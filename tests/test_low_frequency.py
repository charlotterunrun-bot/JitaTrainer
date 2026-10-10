"""低频衰减回归测试（固件来自用户实录）。

背景：笔记本/内置麦克风对 60–150Hz 响应很差。用户实录（2026-10-07）显示，
第 6 弦（E2 82Hz）的电平比第 1 弦低约 30dB，且**短分析窗口下基频分辨不出来**，
YIN 会锁到二次谐波，把 E2 报成 E3 附近。

实测（同一段录音、逐帧统计，48kHz）：

    | 分析窗口        | 落在 E2 音区的帧 | 中位频率   |
    | 2048（ 43ms）   | 12 / 36（33%）   | 157.2 Hz  |
    | 8192（171ms）   | 24 / 47（51%）   |  79.1 Hz  |
    | 低频增强路径     | 21 / 38（55%）   |  78.8 Hz  |

因此 `FrameAnalyzer` 增加"低频增强"：快路径结果落在低频段时用长窗口复核一次，
只接受把频率往真实基频方向拉回的结果。

**这里诚实地记录局限**：即使有增强，这段录音里也只有约一半的帧能锁定真实基频——
因为基频本身太弱。真正让判定可靠的是**连续帧 + 多数表决**（判定层已实现），
以及换用低频响应更好的麦克风。断言因此按"帧分布"而不是"单帧必须正确"来写。
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from jitatrainer.core.audio.analyzer import AnalyzerConfig, FrameAnalyzer
from jitatrainer.core.audio.gate import GateConfig, frame_rms_db
from jitatrainer.core.theory.notes import hz_to_midi, midi_to_hz

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "real_low_e_rolloff.wav"
SAMPLERATE = 48000
HOP = 480
E2_MIDI = 40
BASS_LOW_HZ = 60.0
BASS_HIGH_HZ = 120.0

pytestmark = pytest.mark.skipif(not FIXTURE.is_file(), reason="缺少低频衰减固件")


def load_fixture() -> np.ndarray:
    with wave.open(str(FIXTURE), "rb") as handle:
        assert handle.getframerate() == SAMPLERATE
        return np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").astype(np.float64) / 32768.0


def scan(samples: np.ndarray, analyzer: FrameAnalyzer, length: int) -> list[float]:
    """按帧移扫描整段，返回所有有效检测的频率。"""
    values: list[float] = []
    for start in range(0, max(0, samples.size - length), HOP):
        frame = samples[start : start + length]
        if frame_rms_db(frame) < -70.0:
            continue
        event = analyzer.process(frame, start / SAMPLERATE)
        if event.valid:
            values.append(event.hz)
    return values


def bass_fraction(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(1 for hz in values if BASS_LOW_HZ < hz < BASS_HIGH_HZ) / len(values)


class TestLowFrequencyEnhancement:
    def test_fast_path_fails_to_resolve_low_e(self) -> None:
        """记录短窗口的既有局限：中位频率落在二次谐波上，低音区命中率明显偏低。"""
        samples = load_fixture()
        analyzer = FrameAnalyzer(
            AnalyzerConfig(low_enhance=False, harmonic_mode="presence"),
            gate=GateConfig(noise_floor_db=-90.0, offset_db=5.0),
        )
        values = scan(samples, analyzer, 2048)
        assert values, "未检测到任何有效帧"

        assert float(np.median(values)) > 120.0, "短窗口意外读到了正确的基频"
        assert bass_fraction(values) < 0.5, "短窗口的低音区命中率意外偏高"

    def test_enhancement_improves_low_band_resolution(self) -> None:
        """低频增强必须让"落在低音区的帧"显著变多，中位频率落到 E2 音区。"""
        samples = load_fixture()
        gate = GateConfig(noise_floor_db=-90.0, offset_db=5.0)

        fast = scan(samples, FrameAnalyzer(AnalyzerConfig(low_enhance=False, harmonic_mode="presence"), gate=gate), 2048)
        enhanced = scan(samples, FrameAnalyzer(AnalyzerConfig(low_enhance=True, harmonic_mode="presence"), gate=gate), 8192)

        assert fast and enhanced
        assert float(np.median(enhanced)) < 120.0, (
            f"增强后中位频率仍在高次谐波：{np.median(enhanced):.1f}Hz"
        )
        assert bass_fraction(enhanced) > bass_fraction(fast), (
            f"低音区命中率没有改善：{bass_fraction(fast):.0%} → {bass_fraction(enhanced):.0%}"
        )
        assert bass_fraction(enhanced) >= 0.5, (
            f"增强后低音区命中率仍不足一半：{bass_fraction(enhanced):.0%}"
        )

    def test_majority_vote_picks_bass_region(self) -> None:
        """判定层的多数表决：整段的中位结果必须落在 E2 音区（±1.5 半音）。"""
        samples = load_fixture()
        analyzer = FrameAnalyzer(AnalyzerConfig(harmonic_mode="presence"), gate=GateConfig(noise_floor_db=-90.0, offset_db=5.0))
        values = scan(samples, analyzer, 8192)
        assert values
        median_midi = hz_to_midi(float(np.median(values)))
        assert abs(median_midi - E2_MIDI) <= 1.5, (
            f"中位结果偏离 E2 过远：{np.median(values):.1f}Hz（MIDI {median_midi:.1f}）"
        )

    def test_does_not_enhance_high_notes(self) -> None:
        """高音区不得被误拉低。"""
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_pitch_yin import synth_guitar_note

        f0 = midi_to_hz(64)  # E4
        signal = synth_guitar_note(f0, seed=64)
        frame = signal[7200 : 7200 + 8192]
        analyzer = FrameAnalyzer(AnalyzerConfig(harmonic_mode="presence"), gate=GateConfig(noise_floor_db=-90.0, offset_db=5.0))
        event = analyzer.process(frame, 0.0)
        assert event.valid
        assert abs(hz_to_midi(event.hz) - 64) < 0.5
        assert not event.harmonic_corrected

    def test_required_samples_reflects_low_window(self) -> None:
        assert AnalyzerConfig().required_samples == 8192
        assert AnalyzerConfig(low_enhance=False, harmonic_mode="presence").required_samples == 2048

    def test_low_enhance_can_be_disabled(self) -> None:
        analyzer = FrameAnalyzer(AnalyzerConfig(low_enhance=False, harmonic_mode="presence"))
        assert analyzer.low_detector is None
