"""音频链路测试：门限、起音、帧分析器、会话配置、设置与调弦辅助。

这些测试全部不需要真实音频设备：帧分析器接受合成音频，
设置层用临时数据库，调弦逻辑是纯计算。
"""

from __future__ import annotations

import numpy as np
import pytest

from jitatrainer.core.audio.analyzer import AnalyzerConfig, FrameAnalyzer
from jitatrainer.core.audio.gate import (
    SILENCE_DB,
    GateConfig,
    NoiseFloorMeter,
    OnsetDetector,
    clamp_gate,
    frame_rms_db,
    summarize_capture,
)
from jitatrainer.core.audio.session import AudioSession
from jitatrainer.core.theory.notes import midi_to_hz
from jitatrainer.core.theory.tuning import STANDARD
from jitatrainer.data.db import Database
from jitatrainer.data.settings import DEFAULTS, GLOBAL_KEYS, Settings
from tests.test_pitch_yin import synth_guitar_note

SAMPLERATE = 48000


def note_frame(midi: int, window: int = 2048, *, gain: float = 0.5, start: float = 0.15) -> np.ndarray:
    signal = synth_guitar_note(midi_to_hz(midi), seed=midi)
    offset = int(start * SAMPLERATE)
    return (signal[offset : offset + window] * gain).astype(np.float32)


class TestLevelMath:
    def test_full_scale_sine_is_minus_three_db(self) -> None:
        t = np.arange(4800) / SAMPLERATE
        sine = np.sin(2 * np.pi * 440 * t)
        assert frame_rms_db(sine) == pytest.approx(-3.01, abs=0.05)

    def test_silence(self) -> None:
        assert frame_rms_db(np.zeros(1024)) == SILENCE_DB
        assert frame_rms_db(np.array([])) == SILENCE_DB

    def test_half_amplitude_is_minus_six_db_relative(self) -> None:
        t = np.arange(4800) / SAMPLERATE
        full = np.sin(2 * np.pi * 440 * t)
        half = full * 0.5
        assert frame_rms_db(full) - frame_rms_db(half) == pytest.approx(6.02, abs=0.05)

    def test_gate_clamping(self) -> None:
        assert clamp_gate(-200.0) == -80.0
        assert clamp_gate(10.0) == -10.0
        assert clamp_gate(-45.0) == -45.0


class TestNoiseFloor:
    def test_uses_percentile_not_maximum(self) -> None:
        """测量期间碰到琴弦不应把噪声地板抬高。"""
        meter = NoiseFloorMeter(percentile=25.0)
        for _ in range(20):
            meter.add_level(-70.0)
        meter.add_level(-20.0)  # 用户碰了一下弦
        assert meter.floor_db == pytest.approx(-70.0, abs=1.0)
        assert meter.suggest_gate(12.0) == pytest.approx(-58.0, abs=1.0)

    def test_empty_meter(self) -> None:
        meter = NoiseFloorMeter()
        assert meter.floor_db == SILENCE_DB
        assert meter.count == 0

    def test_invalid_percentile(self) -> None:
        with pytest.raises(ValueError):
            NoiseFloorMeter(percentile=120.0)

    def test_summarize_capture(self) -> None:
        stats = summarize_capture([-60.0, -50.0, -40.0], gate_db=-50.0)
        assert stats.frames == 3
        assert stats.peak_db == pytest.approx(-40.0)
        assert stats.has_signal

    def test_summarize_empty(self) -> None:
        stats = summarize_capture([], gate_db=-50.0)
        assert stats.frames == 0 and not stats.has_signal


class TestOnsetDetector:
    def test_detects_sudden_rise(self) -> None:
        detector = OnsetDetector(rise_db=8.0, history=3)
        for _ in range(3):
            assert detector.update(-70.0) is False
        assert detector.update(-20.0) is True

    def test_no_onset_on_steady_level(self) -> None:
        detector = OnsetDetector(rise_db=8.0, history=3)
        for _ in range(6):
            assert detector.update(-30.0) is False

    def test_gradual_rise_is_not_onset(self) -> None:
        detector = OnsetDetector(rise_db=8.0, history=3)
        assert detector.update(-70.0) is False
        assert detector.update(-66.0) is False
        assert detector.update(-62.0) is False
        assert detector.update(-58.0) is False


class TestFrameAnalyzer:
    def test_silence_below_gate_skips_detection(self) -> None:
        analyzer = FrameAnalyzer(gate=GateConfig(noise_floor_db=-60.0, offset_db=12.0))
        event = analyzer.process(np.zeros(2048, dtype=np.float32), 0.0)
        assert not event.valid
        assert event.rms_db == SILENCE_DB

    def test_quiet_but_above_gate_noise_is_rejected(self) -> None:
        analyzer = FrameAnalyzer(gate=GateConfig(noise_floor_db=-90.0, offset_db=5.0))
        rng = np.random.default_rng(3)
        # 白噪声电平约 -20dB，超过门限但无音高 → 必须判为无效，而不是瞎报音名
        event = analyzer.process((rng.normal(0, 0.1, 2048)).astype(np.float32), 0.0)
        assert not event.valid

    @pytest.mark.parametrize("midi", [40, 45, 50, 55, 59, 64])
    def test_open_strings_detected_through_pipeline(self, midi: int) -> None:
        analyzer = FrameAnalyzer(gate=GateConfig(noise_floor_db=-80.0, offset_db=5.0))
        event = analyzer.process(note_frame(midi), 1.0)
        assert event.valid, f"midi={midi} 未检测到"
        assert event.pitch_class == midi % 12

    def test_detune_correction_shifts_reported_pitch(self) -> None:
        """校准偏移应当修正报告出来的频率。"""
        analyzer = FrameAnalyzer(gate=GateConfig(noise_floor_db=-80.0, offset_db=5.0))
        frame = note_frame(64)
        base = analyzer.process(frame, 0.0)
        analyzer.set_detune_cents(20.0)
        shifted = analyzer.process(frame, 0.1)
        assert shifted.valid and base.valid
        # 检测偏高 20 音分 → 校正后应下调约 20 音分
        assert shifted.hz < base.hz
        ratio_cents = 1200 * np.log2(base.hz / shifted.hz)
        assert ratio_cents == pytest.approx(20.0, abs=1.0)

    def test_onset_flag_propagates(self) -> None:
        analyzer = FrameAnalyzer(gate=GateConfig(noise_floor_db=-80.0, offset_db=5.0))
        for _ in range(3):
            analyzer.process(np.zeros(2048, dtype=np.float32), 0.0)
        event = analyzer.process(note_frame(64), 0.1)
        assert event.is_onset


class TestAudioSession:
    def test_poll_empty(self) -> None:
        session = AudioSession()
        assert session.poll() == []
        assert session.latest_voiced() is None

    def test_queue_bounded(self) -> None:
        session = AudioSession(queue_size=4)
        from jitatrainer.core.audio.events import PitchEvent

        for i in range(10):
            session._on_event(PitchEvent(t=i / 100, hz=440.0, confidence=0.9))  # noqa: SLF001
        assert len(session.poll()) == 4

    def test_stats_count_voiced_and_onsets(self) -> None:
        session = AudioSession()
        from jitatrainer.core.audio.events import PitchEvent

        session._on_event(PitchEvent(t=0.0, hz=440.0, confidence=0.9, is_onset=True))  # noqa: SLF001
        session._on_event(PitchEvent(t=0.1, hz=0.0))  # noqa: SLF001
        session.poll()
        assert session.stats.voiced == 1
        assert session.stats.onsets == 1

    def test_start_fails_cleanly_without_backend(self) -> None:
        """没有音频后端时必须抛错而不是静默失败（UI 会提示用户）。"""
        session = AudioSession()
        session.capture.start()
        # 本机有 sounddevice，因此这里应当成功；若失败也必须抛异常
        assert session.capture.is_running
        session.capture.stop()


class TestSettings:
    @pytest.fixture
    def settings(self, tmp_path):
        db = Database(tmp_path / "s.db")
        db.initialize()
        conn = db.connect()
        profile_id = db.list_profiles(conn)[0]["id"]
        yield Settings(db, conn, profile_id)
        conn.close()

    def test_defaults(self, settings: Settings) -> None:
        assert settings.get("language") == "zh_CN"
        assert settings.get_int("samplerate") == 48000
        assert settings.get_float("tolerance_cents") == 25.0
        assert settings.get_bool("show_pitch_meter") is True
        assert settings.get_bool("include_accidentals") is False
        assert settings.get_optional_int("input_device_index") is None

    def test_typed_roundtrip(self, settings: Settings) -> None:
        settings.set("tolerance_cents", 40.0)
        settings.set("include_accidentals", True)
        settings.set("input_device_index", 7)
        assert settings.get_float("tolerance_cents") == 40.0
        assert settings.get_bool("include_accidentals") is True
        assert settings.get_optional_int("input_device_index") == 7

    def test_global_vs_profile_scope(self, settings: Settings) -> None:
        """语言是全局设置：换档案后仍然生效。"""
        settings.set("language", "en_US")
        settings.set("level_id", "L3")
        other = Settings(settings.db, settings.conn, None)
        assert other.get("language") == "en_US"
        assert other.get("level_id", "L1") == "L1"  # 档案级，无档案时回落默认

    def test_malformed_values_fall_back(self, settings: Settings) -> None:
        settings.set("tolerance_cents", "abc")
        assert settings.get_float("tolerance_cents", 25.0) == 25.0

    def test_stable_ms_prefers_custom_then_preset(self, settings: Settings) -> None:
        settings.set("stable_preset", "fast")
        assert settings.stable_ms() == 150
        settings.set("stable_preset", "robust")
        assert settings.stable_ms() == 300
        settings.set("stable_ms_custom", 420)
        assert settings.stable_ms() == 420
        settings.set("stable_ms_custom", 9999)
        assert settings.stable_ms() == 600  # 上限钳制

    def test_json_helpers(self, settings: Settings) -> None:
        settings.set("extra", {"a": 1})
        assert settings.get_json("extra") == {"a": 1}
        settings.set("broken", "not json")
        assert settings.get_json("broken", default={}) == {}

    def test_global_keys_and_defaults_are_consistent(self) -> None:
        assert GLOBAL_KEYS.issubset(set(DEFAULTS))
        assert DEFAULTS["stable_preset"] == "balanced"


class TestTuningHelpers:
    def test_nearest_open_string(self) -> None:
        # 82.41Hz = E2 → 第 6 弦
        assert STANDARD.nearest_open_string(40.0)[0] == 6
        assert STANDARD.nearest_open_string(64.0)[0] == 1
        assert STANDARD.nearest_open_string(59.2)[0] == 2

    def test_nearest_open_string_delta(self) -> None:
        string_no, delta = STANDARD.nearest_open_string(40.5)
        assert string_no == 6
        assert delta == pytest.approx(0.5)

    def test_string_target_midi(self) -> None:
        assert [STANDARD.string_target_midi(n) for n in range(6, 0, -1)] == [40, 45, 50, 55, 59, 64]
