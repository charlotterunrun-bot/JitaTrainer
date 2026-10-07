"""把位区间（难度级别）。

需求：难度按把位划分 —— 0-3 品 / 0-5 品 / 0-7 品 / 全指板（0-12 品）。
所有级别均包含空弦（0 品）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Level:
    id: str
    name_zh: str
    name_en: str
    max_fret: int
    order: int

    @property
    def frets(self) -> range:
        """该级别包含的品格（含空弦）。"""
        return range(0, self.max_fret + 1)


LEVELS: tuple[Level, ...] = (
    Level("L1", "初学者", "Beginner", 3, 1),
    Level("L2", "入门", "Elementary", 5, 2),
    Level("L3", "进阶", "Intermediate", 7, 3),
    Level("L4", "全指板", "Full fretboard", 12, 4),
)

LEVELS_BY_ID: dict[str, Level] = {level.id: level for level in LEVELS}

DEFAULT_LEVEL_ID = "L1"


def get_level(level_id: str) -> Level:
    try:
        return LEVELS_BY_ID[level_id]
    except KeyError as exc:
        raise KeyError(f"未知把位级别：{level_id}") from exc


def level_ids() -> list[str]:
    return [level.id for level in LEVELS]


def next_level(level_id: str) -> Level | None:
    """返回下一级（已是最高级则返回 None）。"""
    current = get_level(level_id)
    for level in LEVELS:
        if level.order == current.order + 1:
            return level
    return None
