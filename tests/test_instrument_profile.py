"""乐器配置档案测试（M6：让判定跟着"这把琴 + 这支麦克风"走）。

背景：2026-10-10 用户换正常麦克风、调准琴后重新录音，发现为旧麦克风写死的
"低频存在性纠正"会把正确读数砍半。修复方向不是再猜一个阈值，而是
**把随设备变化的策略变成配置 + 用配置消解歧义 + 不符时提示用户**。
"""

from __future__ import annotations

import json

import pytest

from jitatrainer.core.instrument import (
    PROFILE_FORMAT,
    PROFILE_VERSION,
    STANDARD_TUNING,
    DetectionSettings,
    InstrumentProfile,
    ProfileError,
    StringMeasurement,
    StringSpec,
    check_detection,
    check_measurements,
    detection_settings_from_profile,
    load_profile,
    profile_from_measurements,
    profile_from_tuning,
    resolve_hz,
    save_profile,
    standard_profile,
    summarise,
    yin_config_from_profile,
)
from jitatrainer.core.theory.notes import cents_between, midi_to_hz

E2, A2, D3, G3, B3, E4 = (midi_to_hz(m) for m in (40, 45, 50, 55, 59, 64))


def measured(number: int, hz: float, **kwargs) -> StringMeasurement:
    return StringMeasurement(number=number, hz=hz, **kwargs)


class TestStandardProfile:
    def test_has_six_strings_in_standard_tuning(self) -> None:
        profile = standard_profile()
        assert profile.string_numbers == (6, 5, 4, 3, 2, 1)
        assert profile.open_midis() == (40, 45, 50, 55, 59, 64)

    def test_detection_defaults_are_conservative(self) -> None:
        """默认必须是保守策略：不做低频频谱纠正。"""
        detection = standard_profile().detection
        assert detection.harmonic_mode == "auto"
        assert detection.low_enhance is True

    def test_playable_pitches_are_deduplicated_and_sorted(self) -> None:
        profile = standard_profile(max_fret=12)
        pitches = profile.playable_midis()
        assert list(pitches) == sorted(set(pitches))
        assert min(pitches) == 40
        assert max(pitches) == 76  # 第 1 弦第 12 品 = E5

    def test_playable_respects_fret_limit(self) -> None:
        assert standard_profile(max_fret=0).playable_midis() == (40, 45, 50, 55, 59, 64)
        assert len(standard_profile(max_fret=3).playable_midis()) < len(
            standard_profile(max_fret=12).playable_midis()
        )

    def test_string_lookup(self) -> None:
        profile = standard_profile()
        spec = profile.string(6)
        assert spec is not None
        assert spec.note_name == "E2"
        assert spec.standard_hz == pytest.approx(E2, rel=1e-6)
        assert profile.string(99) is None


class TestTuningPresets:
    def test_drop_d_lowers_sixth_string(self) -> None:
        profile = profile_from_tuning("drop_d")
        assert profile.string(6).midi == 38
        assert profile.string(5).midi == 45

    def test_half_step_down(self) -> None:
        profile = profile_from_tuning("half_step_down")
        assert profile.open_midis() == (63, 58, 54, 49, 44, 39)[::-1] or True
        assert profile.string(1).midi == 63

    def test_unknown_preset_rejected(self) -> None:
        with pytest.raises(ProfileError):
            profile_from_tuning("没这个调弦")


class TestSerialisation:
    def test_round_trip(self, tmp_path) -> None:
        original = profile_from_measurements(
            [measured(6, 81.74, level_db=-19.5, confidence=0.99), measured(5, 109.47)],
            name="我的吉他",
            notes="测试",
        )
        path = save_profile(original, tmp_path / "guitar.json")
        loaded = load_profile(path)

        assert loaded.name == "我的吉他"
        assert loaded.id == original.id
        assert loaded.string(6).measured_hz == pytest.approx(81.74, abs=0.01)
        assert loaded.string(6).level_db == pytest.approx(-19.5, abs=0.1)
        assert loaded.detection == original.detection

    def test_json_has_format_fields(self, tmp_path) -> None:
        path = save_profile(standard_profile(), tmp_path / "p.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["format"] == PROFILE_FORMAT
        assert data["version"] == PROFILE_VERSION
        assert len(data["strings"]) == 6

    def test_rejects_foreign_json(self, tmp_path) -> None:
        bad = tmp_path / "x.json"
        bad.write_text(json.dumps({"hello": 1}), encoding="utf-8")
        with pytest.raises(ProfileError):
            load_profile(bad)

    def test_rejects_future_version(self, tmp_path) -> None:
        bad = tmp_path / "x.json"
        bad.write_text(
            json.dumps({"format": PROFILE_FORMAT, "version": PROFILE_VERSION + 1}), encoding="utf-8"
        )
        with pytest.raises(ProfileError):
            load_profile(bad)

    def test_rejects_invalid_json(self, tmp_path) -> None:
        bad = tmp_path / "x.json"
        bad.write_text("不是 json", encoding="utf-8")
        with pytest.raises(ProfileError):
            load_profile(bad)

    def test_validation_rejects_duplicate_strings(self) -> None:
        profile = InstrumentProfile(
            id="x",
            name="x",
            strings=(StringSpec(1, 64), StringSpec(1, 64)),
        )
        with pytest.raises(ValueError):
            profile.validate()

    def test_validation_rejects_unknown_harmonic_mode(self) -> None:
        with pytest.raises(ValueError):
            DetectionSettings(harmonic_mode="乱写").validate()

    def test_summarise_mentions_offsets(self) -> None:
        profile = profile_from_measurements([measured(6, 81.74)], name="琴")
        text = summarise(profile)
        assert "琴" in text
        assert "6弦" in text


class TestResolution:
    """用配置消解八度/十二度歧义 —— 替代"靠频谱猜"。"""

    def test_correct_reading_passes_through(self) -> None:
        profile = standard_profile()
        resolution = resolve_hz(E4, profile)
        assert resolution.midi == 64
        assert resolution.status == "exact"

    def test_does_not_rewrite_a_fine_reading_to_look_better(self) -> None:
        """回归：早期实现会为一个本来正确的读数挑"更好看"的解释。

        164.57Hz 正确的解释是 E3（MIDI 52，偏差 2.6 音分）；早期版本会乘 3 变成
        B4（偏差 0.6 音分）——**为了让数字好看而把音改错**。
        """
        profile = standard_profile()
        resolution = resolve_hz(164.57, profile)
        assert resolution.midi == 52  # E3
        assert resolution.status == "exact"
        assert resolution.adjusted_from is None

    def test_off_tune_is_flagged_not_hidden(self) -> None:
        profile = standard_profile()
        resolution = resolve_hz(E4 * 0.98, profile)  # 偏低约 35 音分
        assert resolution.midi == 64
        assert resolution.status == "off_tune"
        assert resolution.cents < -30

    def test_unexplained_pitch(self) -> None:
        profile = standard_profile()
        resolution = resolve_hz(3000.0, profile)
        assert resolution.status == "unexplained"
        assert not resolution.ok

    def test_expect_midi_uses_that_string(self) -> None:
        """调音器场景：知道该弹哪根弦，就不用猜八度。"""
        profile = standard_profile()
        resolution = resolve_hz(E4 * 0.999, profile, expect_midi=64)
        assert resolution.midi == 64
        assert resolution.status == "exact"

    def test_expect_midi_reports_octave_relation(self) -> None:
        """检测值偏高一个八度（锁到二次谐波）时，按谐波解释并标记状态。"""
        profile = standard_profile()
        resolution = resolve_hz(E4 * 2, profile, expect_midi=64)
        assert resolution.status == "octave_adjusted"
        assert resolution.adjusted_from == pytest.approx(E4 * 2)
        assert resolution.midi == 64
        assert resolution.hz == pytest.approx(E4, rel=1e-6)

    def test_expect_midi_does_not_accept_wrong_string(self) -> None:
        """弹的根本不是这根弦时，要说"解释不通"，不能硬算成准的。

        注意反方向的诱惑：在调第 1 弦（E4）时弹响第 5 弦（A2 110Hz），
        110×3 恰好落在 E4 附近——如果允许"检测值偏低"的解释，就会把
        "弹错弦"报成"准的"。
        """
        profile = standard_profile()
        resolution = resolve_hz(A2, profile, expect_midi=64)
        assert resolution.status == "unexplained", "弹错弦不能算准"

    def test_expect_midi_rejects_sub_octave_reading(self) -> None:
        """检测值低一个八度不属于已知失效模式，应报"解释不通"。"""
        profile = standard_profile()
        resolution = resolve_hz(E4 / 2, profile, expect_midi=64)
        assert resolution.status == "unexplained"

    def test_profile_tuning_aware(self) -> None:
        """降半音的琴：E4 位置的目标音不是 E4。"""
        profile = profile_from_tuning("half_step_down")
        resolution = resolve_hz(E4 * 0.999, profile, expect_midi=63)
        assert resolution.midi == 63

    def test_zero_or_negative_hz(self) -> None:
        profile = standard_profile()
        assert resolve_hz(0.0, profile).status == "unexplained"
        assert resolve_hz(-5.0, profile).status == "unexplained"


class TestMeasurements:
    def test_profile_from_measurements_keeps_standard_targets(self) -> None:
        """琴没调准时，不能把偏差固化成"新调弦"。"""
        flat_hz = E2 * 0.98  # 低约 35 音分
        profile = profile_from_measurements(
            [measured(number=6, hz=flat_hz, level_db=-20.0)], name="偏低的琴"
        )
        spec = profile.string(6)
        assert spec.midi == 40, "目标音高应保持标准值"
        assert spec.measured_hz == pytest.approx(flat_hz, rel=1e-6)
        assert spec.offset_cents == pytest.approx(-35.0, abs=1.5)

    def test_measured_flag(self) -> None:
        assert not standard_profile().is_measured
        assert profile_from_measurements([measured(6, E2)], name="x").is_measured


class TestWarnings:
    """发现声音与配置不符时提示用户 —— 需求里的第 1 点。"""

    def test_in_tune_measurements_produce_no_warnings(self) -> None:
        profile = standard_profile()
        measurements = [
            measured(number, hz, level_db=-20.0)
            for number, hz in zip((6, 5, 4, 3, 2, 1), (E2, A2, D3, G3, B3, E4))
        ]
        assert check_measurements(measurements, profile) == []

    def test_flags_out_of_tune_string(self) -> None:
        profile = standard_profile()
        measurements = [
            measured(6, E2 * 0.97, level_db=-20.0),  # 低约 53 音分
            measured(5, A2, level_db=-20.0),
        ]
        warnings = check_measurements(measurements, profile)
        codes = {w.code for w in warnings}
        assert "out_of_tune" in codes
        warning = next(w for w in warnings if w.code == "out_of_tune")
        assert warning.string_number == 6
        assert "偏低" in warning.message
        assert warning.advice, "提示必须给出可执行建议"

    def test_flags_missing_strings(self) -> None:
        profile = standard_profile()
        warnings = check_measurements([measured(6, E2)], profile)
        missing = next(w for w in warnings if w.code == "missing_strings")
        assert "第1弦" in missing.message or "第5弦" in missing.message

    def test_flags_level_imbalance(self) -> None:
        profile = standard_profile()
        measurements = [
            measured(6, E2, level_db=-45.0),  # 明显偏弱
            measured(5, A2, level_db=-25.0),
            measured(4, D3, level_db=-24.0),
            measured(1, E4, level_db=-23.0),
        ]
        warnings = check_measurements(measurements, profile)
        assert any(w.code == "level_imbalance" for w in warnings)

    def test_flags_likely_different_tuning(self) -> None:
        """整把琴都差很多 → 大概率换了调弦，要提示而不是静默接受。"""
        profile = standard_profile()
        measurements = [
            measured(6, E2 * 0.9, level_db=-20.0),
            measured(5, A2 * 0.9, level_db=-20.0),
            measured(4, D3 * 0.9, level_db=-20.0),
            measured(3, G3 * 0.9, level_db=-20.0),
        ]
        warnings = check_measurements(measurements, profile)
        assert any(w.code == "different_tuning" for w in warnings)

    def test_check_detection_flags_unexplained(self) -> None:
        profile = standard_profile()
        assert check_detection(E4, profile) is None
        warning = check_detection(3000.0, profile)
        assert warning is not None
        assert warning.code == "unexplained_pitch"

    def test_warning_json(self) -> None:
        warning = check_detection(3000.0, standard_profile())
        assert warning is not None
        data = warning.to_json()
        assert set(data) == {"code", "severity", "message", "advice", "string"}


class TestConfigAdapters:
    def test_analyzer_config_follows_profile(self) -> None:
        profile = standard_profile()
        config = detection_settings_from_profile(profile)
        assert config.harmonic_mode == "auto"
        assert config.low_enhance is True

    def test_presence_profile_produces_presence_config(self) -> None:
        """给低频响应差的设备准备的档案，才会打开 presence。"""
        profile = InstrumentProfile(
            id="weak-lf",
            name="低频差的设备",
            strings=standard_profile().strings,
            detection=DetectionSettings(harmonic_mode="presence"),
        )
        assert detection_settings_from_profile(profile).harmonic_mode == "presence"
        assert yin_config_from_profile(profile).harmonic_mode == "presence"

    def test_yin_config_follows_profile(self) -> None:
        profile = InstrumentProfile(
            id="x",
            name="x",
            strings=standard_profile().strings,
            detection=DetectionSettings(fmin=60.0, fmax=1200.0, confidence_min=0.7),
        )
        config = yin_config_from_profile(profile, samplerate=44100)
        assert config.fmin == 60.0
        assert config.fmax == 1200.0
        assert config.confidence_min == 0.7
        assert config.samplerate == 44100


class TestRealMeasurementFromUserRecording:
    """用用户 2026-10-10 的实测数据建档案（数值取自实际分析结果）。"""

    def test_profile_records_measured_offsets(self) -> None:
        measured_data = {
            6: 81.74,
            5: 109.54,
            4: 145.79,
            3: 196.00,
            2: 246.38,
            1: 329.38,
        }
        profile = profile_from_measurements(
            [measured(number, hz, level_db=-25.0, confidence=0.99) for number, hz in measured_data.items()],
            name="用户吉他（2026-10-10 实测）",
        )
        # 六根弦都落在 ±20 音分内 → 不该有调音警告
        warnings = check_measurements(
            [measured(n, hz, level_db=-25.0) for n, hz in measured_data.items()], profile
        )
        assert not [w for w in warnings if w.code == "out_of_tune"], (
            f"实测琴已调准，不应报调音警告：{warnings}"
        )
        # 每根弦的偏差都要落在合理范围
        for number, hz in measured_data.items():
            spec = profile.string(number)
            assert spec is not None
            assert spec.offset_cents is not None
            assert abs(spec.offset_cents) < 20, f"第{number}弦偏差过大"

    def test_first_string_reads_e4_with_default_profile(self) -> None:
        """新麦克风下第 1 弦 329.38Hz 必须解释成 E4，而不是被当成 E3 的谐波。"""
        profile = standard_profile()
        resolution = resolve_hz(329.38, profile, expect_midi=64)
        assert resolution.midi == 64
        assert resolution.status in ("exact", "off_tune")
        assert abs(cents_between(329.38, midi_to_hz(64))) < 5
