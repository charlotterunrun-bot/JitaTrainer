"""乐理层测试：音名/MIDI/频率换算、调弦、指板映射、把位。"""

from __future__ import annotations

import math

import pytest

from jitatrainer.core.theory.fretboard import (
    all_positions,
    level_pitch_classes,
    position_midi,
    positions_for_pitch_class,
    positions_in_level,
)
from jitatrainer.core.theory.levels import LEVELS, get_level, next_level
from jitatrainer.core.theory.notes import (
    NOTE_NAMES,
    cents_between,
    hz_to_midi,
    is_natural,
    midi_name,
    midi_to_hz,
    nearest_cents_offset,
    note_set,
    pitch_class_name,
    pitch_class_of_hz,
)
from jitatrainer.core.theory.tuning import STANDARD, STRING_COUNT, get_tuning


class TestNotes:
    def test_standard_frequencies(self) -> None:
        """六根空弦频率必须与标准值一致（技术方案附录 A）。"""
        expected = {40: 82.41, 45: 110.00, 50: 146.83, 55: 196.00, 59: 246.94, 64: 329.63}
        for midi, hz in expected.items():
            assert midi_to_hz(midi) == pytest.approx(hz, abs=0.01)

    def test_roundtrip(self) -> None:
        for midi in range(24, 100):
            assert hz_to_midi(midi_to_hz(midi)) == pytest.approx(midi, abs=1e-9)

    @pytest.mark.parametrize(
        ("midi", "name"),
        [(40, "E2"), (45, "A2"), (50, "D3"), (55, "G3"), (59, "B3"), (64, "E4"), (60, "C4"), (69, "A4")],
    )
    def test_midi_name(self, midi: int, name: str) -> None:
        assert midi_name(midi) == name

    def test_octave_error_does_not_change_pitch_class(self) -> None:
        """判定只认音名：E2 与 E4 必须是同一个 pitch class。"""
        assert pitch_class_of_hz(midi_to_hz(40)) == pitch_class_of_hz(midi_to_hz(64)) == 4

    def test_cents_between(self) -> None:
        assert cents_between(midi_to_hz(41), midi_to_hz(40)) == pytest.approx(100.0, abs=1e-6)
        assert cents_between(midi_to_hz(40), midi_to_hz(40)) == pytest.approx(0.0, abs=1e-9)

    def test_nearest_cents_offset_uses_same_note_name(self) -> None:
        """偏差必须相对同名音计算，不能跨八度比较。"""
        e2 = midi_to_hz(40)
        assert nearest_cents_offset(e2 * 2 ** (10 / 1200), 4) == pytest.approx(10.0, abs=1e-6)
        # 低一个八度但音名相同，偏差应为 0 而不是 -1200
        assert nearest_cents_offset(e2 / 2, 4) == pytest.approx(0.0, abs=1e-9)

    def test_note_set(self) -> None:
        assert note_set(include_accidentals=True) == set(range(12))
        assert note_set(include_accidentals=False) == {0, 2, 4, 5, 7, 9, 11}
        assert is_natural(4) and not is_natural(6)
        assert pitch_class_name(4) == "E"
        assert NOTE_NAMES[4] == "E"


class TestTuning:
    def test_standard_open_strings(self) -> None:
        assert STANDARD.open_strings == (40, 45, 50, 55, 59, 64)
        assert STANDARD.open_midi(6) == 40  # 最粗弦 E2
        assert STANDARD.open_midi(1) == 64  # 最细弦 E4

    def test_capo_offsets_pitch(self) -> None:
        from jitatrainer.core.theory.tuning import Tuning

        capo = Tuning("capo2", "变调夹2品", "Capo 2", (40, 45, 50, 55, 59, 64), capo_fret=2)
        assert capo.midi_at(6, 0) == 42
        assert capo.midi_at(6, 3) == 45

    def test_invalid_string_and_length(self) -> None:
        with pytest.raises(ValueError):
            STANDARD.open_midi(0)
        with pytest.raises(ValueError):
            STANDARD.open_midi(7)
        with pytest.raises(ValueError):
            from jitatrainer.core.theory.tuning import Tuning

            Tuning("bad", "坏", "bad", (40, 45))
        with pytest.raises(KeyError):
            get_tuning("drop-d")


class TestFretboard:
    def test_position_midi(self) -> None:
        assert position_midi(STANDARD, 6, 0) == 40
        assert position_midi(STANDARD, 6, 3) == 43  # G2
        assert position_midi(STANDARD, 1, 12) == 76  # E5
        assert position_midi(STANDARD, 5, 2) == 47  # B2

    @pytest.mark.parametrize(
        ("level_id", "max_fret", "expected_count"),
        [("L1", 3, 24), ("L2", 5, 36), ("L3", 7, 48), ("L4", 12, 78)],
    )
    def test_level_position_counts(self, level_id: str, max_fret: int, expected_count: int) -> None:
        """技术方案声明的位置数必须成立。"""
        level = get_level(level_id)
        assert level.max_fret == max_fret
        assert len(positions_in_level(level)) == expected_count
        assert len(all_positions("standard", max_fret)) == STRING_COUNT * (max_fret + 1)

    def test_every_level_covers_all_twelve_pitch_classes(self) -> None:
        """L1 已经覆盖全部 12 个音名 —— 所以"默认只出自然音"这一设置项是必要的。"""
        for level in LEVELS:
            classes = level_pitch_classes(level.id, include_accidentals=True)
            assert len(classes) == 12, f"{level.id} 只覆盖 {len(classes)} 个音名"

    def test_natural_only_filter(self) -> None:
        for level in LEVELS:
            classes = level_pitch_classes(level.id, include_accidentals=False)
            assert set(classes) == {0, 2, 4, 5, 7, 9, 11}

    def test_positions_for_pitch_class(self) -> None:
        positions = positions_for_pitch_class(4, "L1")  # E
        assert positions, "L1 中应存在 E"
        assert all(p.pitch_class == 4 for p in positions)
        assert {p.midi for p in positions} >= {40}  # 至少包含第 6 弦空弦

    def test_position_labels(self) -> None:
        pos = positions_for_pitch_class(11, "L1")[0]  # B
        assert "弦" in pos.label_zh() and "String" in pos.label_en()
        assert pos.note_name.startswith("B")


class TestLevels:
    def test_ordering_and_next(self) -> None:
        assert [lv.id for lv in LEVELS] == ["L1", "L2", "L3", "L4"]
        assert next_level("L1").id == "L2"  # type: ignore[union-attr]
        assert next_level("L4") is None

    def test_all_levels_include_open_strings(self) -> None:
        for level in LEVELS:
            assert level.frets.start == 0

    def test_unknown_level(self) -> None:
        with pytest.raises(KeyError):
            get_level("L9")
