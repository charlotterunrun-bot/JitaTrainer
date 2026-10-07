"""YIN 音高检测测试。

这里固化 M0 基准测试中最重要的三条结论：
  1. 吉他音域内的识别精度（<= 1 音分）
  2. 分析窗口不足时**必须报错**而不是给出错误结果
  3. 谐波校验不得把正确基频改错（回归测试，防止再次引入激进规则）
  4. 低置信度结果必须被拒绝（避免冤判）
"""

from __future__ import annotations

import numpy as np
import pytest

from jitatrainer.core.audio.pitch_yin import (
    PitchDetector,
    YinConfig,
    harmonic_correct,
)
from jitatrainer.core.theory.notes import cents_between, midi_to_hz, pitch_class_of_hz

SAMPLERATE = 48000
OPEN_STRINGS = {"E2": 40, "A2": 45, "D3": 50, "G3": 55, "B3": 59, "E4": 64}


def synth_guitar_note(
    f0: float,
    *,
    samplerate: int = SAMPLERATE,
    duration: float = 1.0,
    harmonics: int = 12,
    decay: float = 4.0,
    noise_db: float | None = None,
    seed: int = 0,
) -> np.ndarray:
    """合成一个拨弦音（与 tools/pitch_bench.py 相同的合成模型）。"""
    t = np.arange(int(samplerate * duration)) / samplerate
    signal = np.zeros_like(t)
    for n in range(1, harmonics + 1):
        freq = f0 * n
        if freq > samplerate / 2 * 0.95:
            break
        amplitude = 1.0 / (n**1.2)
        signal += amplitude * np.sin(2 * np.pi * freq * t + 0.3 * n) * np.exp(
            -decay * t * (1 + 0.25 * (n - 1))
        )
    signal /= np.max(np.abs(signal)) + 1e-12
    if noise_db is not None:
        rng = np.random.default_rng(seed)
        signal = signal + rng.normal(0, 10 ** (-noise_db / 20.0), size=signal.shape)
    return signal


def frame_of(f0: float, window: int = 2048, **kwargs) -> np.ndarray:
    signal = synth_guitar_note(f0, **kwargs)
    start = int(0.15 * SAMPLERATE)
    return signal[start : start + window].astype(np.float64)


class TestConfigValidation:
    def test_window_must_cover_lowest_frequency(self) -> None:
        """1024 窗口测不出 E2 —— 必须直接报错，而不是给出错误结果。"""
        with pytest.raises(ValueError, match="分析窗口"):
            YinConfig(window=1024).validate()

    def test_default_window_is_valid(self) -> None:
        cfg = YinConfig()
        cfg.validate()
        assert cfg.window == 2048
        assert cfg.tau_max >= 583  # 必须覆盖 E2 的周期 582.5

    def test_tau_range(self) -> None:
        cfg = YinConfig()
        assert cfg.tau_min == pytest.approx(SAMPLERATE / cfg.fmax, rel=0.01)
        assert cfg.tau_max == min(cfg.window // 2, int(SAMPLERATE / cfg.fmin))


class TestAccuracy:
    @pytest.mark.parametrize("name,midi", list(OPEN_STRINGS.items()))
    def test_open_strings_detected_within_one_cent(self, name: str, midi: int) -> None:
        f0 = midi_to_hz(midi)
        detector = PitchDetector()
        result = detector.detect(frame_of(f0, seed=midi))
        assert result.valid, f"{name} 未检测到音高"
        assert abs(cents_between(result.hz, f0)) <= 1.0, f"{name} 误差过大: {result.cents:.2f} 音分"
        assert result.pitch_class == midi % 12

    def test_high_position_note(self) -> None:
        f0 = midi_to_hz(76)  # E5，1 弦 12 品
        result = PitchDetector().detect(frame_of(f0, seed=76))
        assert result.valid
        assert abs(cents_between(result.hz, f0)) <= 1.0

    def test_octave_error_does_not_affect_pitch_class(self) -> None:
        """即使发生八度误判，音名判定依然正确 —— 这是"只认音名"的红利。"""
        f0 = midi_to_hz(40)
        result = PitchDetector().detect(frame_of(f0))
        assert result.valid
        assert result.pitch_class == pitch_class_of_hz(f0) == 4


class TestHarmonicCorrection:
    def test_does_not_alter_low_notes(self) -> None:
        """回归：第一版激进规则会把 E2/A2/D3 的正确基频错误地降低一个八度。"""
        for midi in (40, 45, 50):
            f0 = midi_to_hz(midi)
            frame = frame_of(f0)
            corrected, changed = harmonic_correct(frame, SAMPLERATE, f0)
            assert not changed, f"midi={midi} 被错误修正"
            assert corrected == pytest.approx(f0)

    def test_full_pipeline_never_shifts_octave_down(self) -> None:
        """整条检测链路：正常合成音不允许被降到低八度。"""
        detector = PitchDetector()
        for name, midi in OPEN_STRINGS.items():
            f0 = midi_to_hz(midi)
            result = detector.detect(frame_of(f0, seed=midi))
            assert result.valid
            assert result.hz > f0 * 0.95, f"{name} 被降到低八度: {result.hz:.2f} vs {f0:.2f}"

    def test_skips_check_for_very_low_frequencies(self) -> None:
        """低于 60Hz 的候选不再下探（已经超出吉他音域）。"""
        frame = frame_of(midi_to_hz(40))
        _, changed = harmonic_correct(frame, SAMPLERATE, 55.0)
        assert not changed

    def test_corrects_octave_up_when_fundamental_is_weak(self) -> None:
        """真实录音暴露的场景：基频很弱、偶次谐波主导时，YIN 会锁到高八度。

        这里直接对校正函数做单元测试：给定"检测值是真实基频的两倍"，
        且 f0/2 处存在（较弱的）谱峰，必须向下修正一个八度。
        """
        f0 = midi_to_hz(40)  # E2 = 82.41Hz
        frame = _synth_weak_fundamental(f0, fundamental_gain=0.02)
        corrected, changed = harmonic_correct(frame, SAMPLERATE, f0 * 2)
        assert changed, "弱基频场景未被修正（低音弦会报高八度）"
        assert corrected == pytest.approx(f0, rel=0.02)

    def test_corrects_twelfth_lock_when_third_harmonic_dominates(self) -> None:
        """真实录音暴露的第二种锁定：基频只有三次谐波的 1/19 时，YIN 报十二度。

        此时 hz/3 处有（较弱的）基频峰、且其二次谐波也有支持，必须向下修正。
        """
        f0 = midi_to_hz(45)  # A2 = 110Hz
        frame = _synth_weak_fundamental_twelfth(f0)
        corrected, changed = harmonic_correct(frame, SAMPLERATE, f0 * 3)
        assert changed, "十二度锁定未被修正（第 5 弦会报成 D#4）"
        assert corrected == pytest.approx(f0, rel=0.03)

    def test_does_not_shift_when_subharmonic_absent(self) -> None:
        """没有低八度成分时不得乱改（防止把正确的高音拉低）。"""
        f0 = midi_to_hz(50)  # D3
        frame = frame_of(f0)
        corrected, changed = harmonic_correct(frame, SAMPLERATE, f0)
        assert not changed
        assert corrected == pytest.approx(f0)

    def test_low_rule_only_applies_to_low_band(self) -> None:
        """高频区仍走保守规则：f0/2 必须显著更强才修正。"""
        f0 = midi_to_hz(76)  # E5
        frame = _synth_weak_fundamental(f0, fundamental_gain=0.02)
        # 高频段（>420Hz）不启用"存在性"判据，因此不应因为弱基频就向下修正
        _, changed = harmonic_correct(frame, SAMPLERATE, f0)
        assert not changed


def _synth_weak_fundamental(f0: float, *, fundamental_gain: float = 0.054, duration: float = 1.0) -> np.ndarray:
    """合成"基频很弱、偶次谐波主导"的音（模拟内置麦克风低频衰减）。

    基频比例 5.4% 取自实测：第 6 弦片段里基频 76.2Hz 的幅度是二次谐波
    155.3Hz 的 1/18。
    """
    t = np.arange(int(SAMPLERATE * duration)) / SAMPLERATE
    signal = fundamental_gain * np.sin(2 * np.pi * f0 * t)
    for n, amplitude in ((2, 1.0), (4, 0.5), (6, 0.3), (8, 0.15)):
        freq = f0 * n
        if freq < SAMPLERATE / 2:
            signal += amplitude * np.sin(2 * np.pi * freq * t)
    signal /= np.max(np.abs(signal)) + 1e-12
    start = int(0.15 * SAMPLERATE)
    return signal[start : start + 2048].astype(np.float64)


def _synth_weak_fundamental_twelfth(f0: float, duration: float = 1.0) -> np.ndarray:
    """合成"基频很弱、三次谐波主导"的音（十二度锁定的成因）。

    谐波比例取自 2026-10-07 的真实测量：第 5 弦片段里基频 106.9Hz 的幅度
    只有三次谐波 317.9Hz 的 **1/19**（≈5.3%），二次谐波约为基频的 1.2 倍。
    刻意用实测比例而不是随便取一个值，避免把阈值卡在临界点上。
    """
    t = np.arange(int(SAMPLERATE * duration)) / SAMPLERATE
    signal = 0.053 * np.sin(2 * np.pi * f0 * t) + 0.064 * np.sin(2 * np.pi * 2 * f0 * t)
    for n, amplitude in ((3, 1.0), (6, 0.5), (9, 0.3), (12, 0.15)):
        freq = f0 * n
        if freq < SAMPLERATE / 2:
            signal += amplitude * np.sin(2 * np.pi * freq * t)
    signal /= np.max(np.abs(signal)) + 1e-12
    start = int(0.15 * SAMPLERATE)
    return signal[start : start + 2048].astype(np.float64)


class TestRejection:
    def test_silence_is_rejected(self) -> None:
        result = PitchDetector().detect(np.zeros(2048))
        assert not result.valid

    def test_noise_is_rejected(self) -> None:
        rng = np.random.default_rng(7)
        result = PitchDetector().detect(rng.normal(0, 0.3, 2048))
        assert not result.valid

    def test_low_confidence_reading_is_not_valid(self) -> None:
        """回归：噪声下 YIN 会给出置信度很低的频率（实测 10dB 噪声把 B3 读成 83Hz），
        这种读数必须被标记为不可用，否则会变成冤判。"""
        cfg = YinConfig(confidence_min=0.9999)
        result = PitchDetector(cfg).detect(frame_of(midi_to_hz(59), noise_db=10.0, seed=99))
        assert not result.valid, f"低置信度读数被当成有效结果: {result}"
        assert result.accepted is False

    def test_short_frame_is_padded_not_crashed(self) -> None:
        result = PitchDetector().detect(frame_of(midi_to_hz(64))[:600])
        assert isinstance(result.valid, bool)
        assert isinstance(result.hz, float)


class TestNoFalsePositives:
    """"宁可慢一点，不可冤判"：噪声下不允许输出错误的音名。"""

    @pytest.mark.parametrize("noise_db", [30.0, 20.0, 15.0, 10.0])
    def test_no_wrong_pitch_class_under_noise(self, noise_db: float) -> None:
        detector = PitchDetector()
        for name, midi in OPEN_STRINGS.items():
            f0 = midi_to_hz(midi)
            frame = frame_of(f0, noise_db=noise_db, seed=midi + int(noise_db))
            result = detector.detect(frame)
            if result.valid:
                # 允许"没测到"，但一旦给出结果，音名必须正确
                assert result.pitch_class == midi % 12, (
                    f"{name} 在 {noise_db}dB 噪声下给出错误音名 "
                    f"{result.pitch_class}（真值 {midi % 12}，置信度 {result.confidence:.3f}）"
                )
            else:
                assert not result.accepted or result.hz <= 0
