"""乐器配置档案与程序的接线测试。

要点：配置档案必须**真的驱动检测策略**，否则它只是一份说明文档。
"""

from __future__ import annotations

import json

import pytest

from jitatrainer.core.instrument import (
    DetectionSettings,
    InstrumentProfile,
    save_profile,
    standard_profile,
)
from jitatrainer.data.db import Database
from jitatrainer.data.settings import Settings
from jitatrainer.ui.audio_bridge import active_profile, build_session


@pytest.fixture
def settings(tmp_path) -> Settings:
    db = Database(tmp_path / "wire.db")
    db.initialize()
    conn = db.connect()
    profile_id = db.list_profiles(conn)[0]["id"]
    yield Settings(db, conn, profile_id)
    conn.close()


def write_profile(tmp_path, **detection) -> InstrumentProfile:  # noqa: ANN003
    profile = InstrumentProfile(
        id="test-profile",
        name="测试琴",
        strings=standard_profile().strings,
        detection=DetectionSettings(**detection),
    )
    return profile


class TestActiveProfile:
    def test_defaults_to_builtin_when_unset(self, settings: Settings) -> None:
        profile = active_profile(settings)
        assert profile.id == "standard-6-eadgbe"
        assert profile.detection.harmonic_mode == "auto"

    def test_loads_configured_profile(self, settings: Settings, tmp_path) -> None:
        profile = write_profile(tmp_path, harmonic_mode="presence", low_band_hz=180.0)
        path = save_profile(profile, tmp_path / "guitar.json")
        settings.set("instrument_profile", str(path))

        loaded = active_profile(settings)
        assert loaded.id == "test-profile"
        assert loaded.detection.harmonic_mode == "presence"
        assert loaded.detection.low_band_hz == 180.0

    def test_missing_file_falls_back(self, settings: Settings, tmp_path) -> None:
        settings.set("instrument_profile", str(tmp_path / "nope.json"))
        assert active_profile(settings).id == "standard-6-eadgbe"

    def test_corrupt_file_falls_back(self, settings: Settings, tmp_path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{ 这不是 json", encoding="utf-8")
        settings.set("instrument_profile", str(bad))
        # 配置坏了不能让程序起不来
        assert active_profile(settings).id == "standard-6-eadgbe"


class TestSessionHonoursProfile:
    def test_session_uses_profile_harmonic_mode(self, settings: Settings, tmp_path) -> None:
        profile = write_profile(tmp_path, harmonic_mode="presence")
        settings.set("instrument_profile", str(save_profile(profile, tmp_path / "p.json")))

        session = build_session(settings)
        assert session.analyzer_config.harmonic_mode == "presence"
        assert session.analyzer.detector.config.harmonic_mode == "presence"
        assert session.analyzer.low_detector is not None
        assert session.analyzer.low_detector.config.harmonic_mode == "presence"

    def test_session_defaults_to_conservative(self, settings: Settings) -> None:
        session = build_session(settings)
        assert session.analyzer_config.harmonic_mode == "auto"
        assert session.analyzer.detector.config.harmonic_mode == "auto"

    def test_session_uses_profile_low_enhance(self, settings: Settings, tmp_path) -> None:
        profile = write_profile(tmp_path, low_enhance=False)
        settings.set("instrument_profile", str(save_profile(profile, tmp_path / "p.json")))
        session = build_session(settings)
        assert session.analyzer.low_detector is None

    def test_profile_is_attached_to_session(self, settings: Settings, tmp_path) -> None:
        profile = write_profile(tmp_path, harmonic_mode="off")
        settings.set("instrument_profile", str(save_profile(profile, tmp_path / "p.json")))
        session = build_session(settings)
        assert session.profile.id == "test-profile"

    def test_samplerate_still_from_settings(self, settings: Settings) -> None:
        settings.set("samplerate", "44100")
        session = build_session(settings)
        assert session.samplerate == 44100
        assert session.analyzer_config.samplerate == 44100


class TestMeasuredProfileDrivesDetection:
    """端到端：用实测生成的档案跑一遍合成信号，确认策略真的生效。"""

    def test_presence_profile_corrects_where_auto_does_not(self, settings: Settings, tmp_path) -> None:
        import numpy as np

        from jitatrainer.core.theory.notes import midi_to_hz

        sample_rate = 48000
        f0 = midi_to_hz(40)  # E2
        t = np.arange(sample_rate) / sample_rate
        signal = 0.02 * np.sin(2 * np.pi * f0 * t)
        for n, amp in ((2, 1.0), (4, 0.5), (6, 0.3), (8, 0.15)):
            signal += amp * np.sin(2 * np.pi * f0 * n * t)
        signal /= np.max(np.abs(signal)) + 1e-12
        frame = signal[int(0.15 * sample_rate) : int(0.15 * sample_rate) + 2048]

        # 默认（auto）：不动这个读数
        auto = build_session(settings)
        result_auto = auto.analyzer.detector.detect(frame.astype(np.float64))
        assert not result_auto.harmonic_corrected

        # 换成 presence 档案：按低频存在性规则向下修正
        profile = write_profile(tmp_path, harmonic_mode="presence")
        settings.set("instrument_profile", str(save_profile(profile, tmp_path / "p.json")))
        legacy = build_session(settings)
        result_legacy = legacy.analyzer.detector.detect(frame.astype(np.float64))
        assert result_legacy.harmonic_corrected
        assert result_legacy.hz == pytest.approx(f0, rel=0.03)


class TestProfileFileCompatibility:
    def test_measurement_tool_output_is_loadable(self, tmp_path) -> None:
        """tools/measure_guitar.py 写出的文件必须能被程序读回。"""
        from jitatrainer.core.instrument import (
            StringMeasurement,
            load_profile,
            profile_from_measurements,
        )

        measured = [
            StringMeasurement(number=number, hz=hz, level_db=-30.0, confidence=0.99)
            for number, hz in ((6, 81.71), (5, 109.42), (4, 145.73), (3, 195.98), (2, 246.45), (1, 329.19))
        ]
        profile = profile_from_measurements(measured, name="用户吉他")
        path = save_profile(profile, tmp_path / "measured.json")
        loaded = load_profile(path)

        assert loaded.name == "用户吉他"
        assert loaded.is_measured
        assert loaded.string(6).measured_hz == pytest.approx(81.71, abs=0.01)
        assert loaded.detection.harmonic_mode == "auto"

    def test_profile_json_is_readable_text(self, tmp_path) -> None:
        path = save_profile(standard_profile(), tmp_path / "p.json")
        text = path.read_text(encoding="utf-8")
        assert "标准六弦吉他" in text, "配置文件应是可读的 UTF-8 中文，方便用户自己看"
        data = json.loads(text)
        assert data["strings"][0]["note"] == "E2"
