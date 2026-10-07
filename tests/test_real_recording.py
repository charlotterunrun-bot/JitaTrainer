"""真实录音回归测试。

固件来源：2026-10-07 用内置麦克风阵列（Realtek, 48kHz）实录的六根空弦，
按 6→5→4→3→2→1 依次拨响（`tools/capture_fixture.py` 采集与切段）。

这段录音的价值在于它**真实复现了两个现实问题**：
  1. 内置麦克风对 60–150Hz 响应差，低音弦基频被压到二次谐波的 1/18；
  2. 当时琴整体偏低 16–93 音分。

第 1 条会让 YIN 锁到高八度（把 E2 报成 E3 附近），因此这里的断言是
"每个音必须落在正确音区（±1.5 个半音）"——八度误判会偏离约 12 个半音，必然失败。
"""

from __future__ import annotations

import wave
from functools import lru_cache
from pathlib import Path

import numpy as np
import pytest

from jitatrainer.core.audio.pitch_yin import PitchDetector
from jitatrainer.core.theory.notes import hz_to_midi, midi_to_hz

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "real_open_strings.wav"
TWELFTH_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "real_twelfth_lock.wav"
SAMPLERATE = 48000
SEGMENT_SECONDS = 0.5
GAP_SECONDS = 0.1
WINDOW = 2048
HOP = 480

#: 六根空弦的标准音高（顺序与固件一致：6→5→4→3→2→1）
EXPECTED_MIDI = [40, 45, 50, 55, 59, 64]
STRING_NAMES = ["第6弦 E2", "第5弦 A2", "第4弦 D3", "第3弦 G3", "第2弦 B3", "第1弦 E4"]

#: 允许的音区误差（半音）。琴本身偏低，所以放宽到 1.5；八度误判会偏 ~12 个半音
TOLERANCE_SEMITONES = 1.5

pytestmark = pytest.mark.skipif(not FIXTURE.is_file(), reason="缺少真实录音固件")


def load_fixture() -> np.ndarray:
    with wave.open(str(FIXTURE), "rb") as handle:
        return np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").astype(np.float64) / 32768.0


@lru_cache(maxsize=1)
def _cached_samples() -> np.ndarray:
    return load_fixture()


def segment_frame(samples: np.ndarray, index: int) -> np.ndarray:
    """取第 index 段中**能量最强**的一窗音频。

    不能用"段首 + 固定偏移"：实测第 3、1 弦的拨弦只维持 0.10–0.11 秒就衰减到
    门限以下，固定偏移会正好错过。真实程序也是持续扫描并取最佳窗口。
    """
    block_start = int(index * (SEGMENT_SECONDS + GAP_SECONDS) * SAMPLERATE)
    block_end = block_start + int(SEGMENT_SECONDS * SAMPLERATE)

    best_frame = samples[block_start : block_start + WINDOW]
    best_rms = float(np.sqrt(np.mean(best_frame**2))) if best_frame.size else -1.0
    for start in range(block_start, max(block_start, block_end - WINDOW), HOP):
        frame = samples[start : start + WINDOW]
        if frame.size < WINDOW:
            break
        rms = float(np.sqrt(np.mean(frame**2)))
        if rms > best_rms:
            best_rms, best_frame = rms, frame
    return best_frame


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        return np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").astype(np.float64) / 32768.0


def loudest_frame(samples: np.ndarray, window: int = WINDOW, hop: int = HOP) -> np.ndarray:
    """在整段音频里找能量最强的一窗（真实程序也是持续扫描取最佳窗口）。"""
    best = samples[:window]
    best_rms = float(np.sqrt(np.mean(best**2))) if best.size else -1.0
    for start in range(0, max(0, samples.size - window), hop):
        frame = samples[start : start + window]
        rms = float(np.sqrt(np.mean(frame**2)))
        if rms > best_rms:
            best_rms, best = rms, frame
    return best


class TestTwelfthLock:
    """第 5 弦的"十二度锁定"：基频 107Hz 只有三次谐波 318Hz 的 1/19。"""

    pytestmark = pytest.mark.skipif(not TWELFTH_FIXTURE.is_file(), reason="缺少十二度锁定固件")

    def test_not_locked_to_third_harmonic(self) -> None:
        frame = loudest_frame(load_wav(TWELFTH_FIXTURE))
        result = PitchDetector().detect(frame)
        assert result.valid, "未检测到音高"

        detected_midi = hz_to_midi(result.hz)
        expected = 45  # A2
        assert abs(detected_midi - expected) <= TOLERANCE_SEMITONES, (
            f"第 5 弦锁定到高次谐波：检测 {result.hz:.1f}Hz（MIDI {detected_midi:.1f}），"
            f"期望 A2（{expected}），偏离 {abs(detected_midi - expected):.1f} 个半音"
        )

    def test_reported_frequency_is_in_bass_region(self) -> None:
        """十二度锁定会报 ~318Hz；修复后应落在 90–130Hz。"""
        frame = loudest_frame(load_wav(TWELFTH_FIXTURE))
        result = PitchDetector().detect(frame)
        assert result.valid
        assert 90.0 < result.hz < 130.0, f"第 5 弦频率异常：{result.hz:.1f}Hz"


class TestRealRecording:
    @pytest.fixture(autouse=True)
    def samples(self) -> np.ndarray:
        return _cached_samples()

    def test_fixture_shape(self) -> None:
        samples = _cached_samples()
        expected = int((SEGMENT_SECONDS + GAP_SECONDS) * len(EXPECTED_MIDI) * SAMPLERATE)
        assert abs(samples.size - expected) < SAMPLERATE // 10

    @pytest.mark.parametrize("index", range(len(EXPECTED_MIDI)))
    def test_no_octave_error_on_any_string(self, samples: np.ndarray, index: int) -> None:
        """核心回归：任何一根弦都不允许落到相差一个八度的音区。"""
        frame = segment_frame(samples, index)
        result = PitchDetector().detect(frame)
        assert result.valid, f"{STRING_NAMES[index]} 未检测到音高（固件窗口能量过低）"

        detected_midi = hz_to_midi(result.hz)
        expected = EXPECTED_MIDI[index]
        semitone_error = abs(detected_midi - expected)

        assert semitone_error <= TOLERANCE_SEMITONES, (
            f"{STRING_NAMES[index]} 音区错误：检测到 "
            f"{result.hz:.1f}Hz（MIDI {detected_midi:.1f}），"
            f"期望 {expected}，偏离 {semitone_error:.1f} 个半音"
        )

    def test_low_e_string_is_not_reported_an_octave_up(self, samples: np.ndarray) -> None:
        """第 6 弦曾经被报成 155Hz（高八度），修复后必须落在 60–110Hz。"""
        result = PitchDetector().detect(segment_frame(samples, 0))
        assert result.valid
        assert 60.0 < result.hz < 110.0, f"第 6 弦频率异常：{result.hz:.1f}Hz"

    def test_detected_pitch_class_matches_flat_string(self, samples: np.ndarray) -> None:
        """琴整体偏低，音名可能落到相邻半音上，但必须与"最接近的标准音"一致。"""
        detected: list[str] = []
        for index in range(len(EXPECTED_MIDI)):
            result = PitchDetector().detect(segment_frame(samples, index))
            assert result.valid, f"{STRING_NAMES[index]} 未检测到音高"
            expected = EXPECTED_MIDI[index]
            nearest_allowed = {(expected + delta) % 12 for delta in (-1, 0, 1)}
            detected.append(f"{STRING_NAMES[index]}→{result.pitch_class}")
            assert result.pitch_class in nearest_allowed, (
                f"{STRING_NAMES[index]} 音名 {result.pitch_class} 不在 "
                f"{sorted(nearest_allowed)} 中（检测 {result.hz:.1f}Hz）"
            )
        print("  六根弦音名序号：" + " ".join(detected))
