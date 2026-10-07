"""指板映射：弦 × 品 → 音高。

设计要点（技术方案 §4.3）：
  - 位置表按级别**预计算**，不在出题时临时展开，降低错误率；
  - 位置是"标准答案展示"，不参与判定（判定只认音名）。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from .levels import Level, get_level
from .notes import midi_name, pitch_class
from .tuning import STRING_COUNT, Tuning, get_tuning


@dataclass(frozen=True, slots=True)
class Position:
    """指板上的一个位置。"""

    string: int  # 1 = 最细弦（高音 E），6 = 最粗弦
    fret: int
    midi: int

    @property
    def pitch_class(self) -> int:
        return pitch_class(self.midi)

    @property
    def note_name(self) -> str:
        return midi_name(self.midi)

    def label_zh(self) -> str:
        return f"第 {self.string} 弦 · 第 {self.fret} 品"

    def label_en(self) -> str:
        return f"String {self.string} · Fret {self.fret}"


def position_midi(tuning: Tuning, string_no: int, fret: int) -> int:
    return tuning.midi_at(string_no, fret)


@lru_cache(maxsize=64)
def all_positions(tuning_id: str, max_fret: int) -> tuple[Position, ...]:
    """在 0..max_fret 范围内的全部合法位置。"""
    tuning = get_tuning(tuning_id)
    positions: list[Position] = []
    for string_no in range(1, STRING_COUNT + 1):
        for fret in range(0, max_fret + 1):
            positions.append(Position(string_no, fret, tuning.midi_at(string_no, fret)))
    return tuple(positions)


def positions_in_level(level: Level | str, tuning_id: str = "standard") -> tuple[Position, ...]:
    level_obj = get_level(level) if isinstance(level, str) else level
    return all_positions(tuning_id, level_obj.max_fret)


@lru_cache(maxsize=64)
def level_pitch_classes(level_id: str, include_accidentals: bool) -> tuple[int, ...]:
    """某级别下可出的音名集合（按音名升序）。"""
    level = get_level(level_id)
    classes = {p.pitch_class for p in positions_in_level(level)}
    if not include_accidentals:
        classes = {pc for pc in classes if pc in (0, 2, 4, 5, 7, 9, 11)}
    return tuple(sorted(classes))


def positions_for_pitch_class(
    pc: int, level: Level | str, tuning_id: str = "standard"
) -> tuple[Position, ...]:
    """某音名在某级别下的全部位置（用于提示与统计，不用于判定）。"""
    return tuple(p for p in positions_in_level(level, tuning_id) if p.pitch_class == pc % 12)
