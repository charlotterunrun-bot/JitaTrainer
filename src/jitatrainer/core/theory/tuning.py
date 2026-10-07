"""调弦定义。

本期只实例化标准调弦，但结构上已为降半音、变调夹、开放调弦预留：
把"弦 → 空弦音高"的映射与"变调夹抬高多少"分离，未来扩展不需要改动指板层。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 标准调弦空弦音（第 6 弦到第 1 弦，单位 MIDI）：E2 A2 D3 G3 B3 E4
STANDARD_OPEN_STRINGS = (40, 45, 50, 55, 59, 64)

STRING_COUNT = 6


@dataclass(frozen=True, slots=True)
class Tuning:
    """一种调弦方式。

    Attributes:
        id: 稳定标识（用于存档与设置）
        name_zh / name_en: 显示名
        open_strings: 从第 6 弦（最粗）到第 1 弦（最细）的空弦 MIDI 音高
        capo_fret: 变调夹品位（本期恒为 0）
    """

    id: str
    name_zh: str
    name_en: str
    open_strings: tuple[int, ...]
    capo_fret: int = 0

    def __post_init__(self) -> None:
        if len(self.open_strings) != STRING_COUNT:
            raise ValueError(f"调弦必须定义 {STRING_COUNT} 根弦，收到 {len(self.open_strings)}")
        if self.capo_fret < 0:
            raise ValueError("变调夹品位不能为负")

    def open_midi(self, string_no: int) -> int:
        """第 ``string_no`` 弦的空弦音高（1 = 最细弦）。"""
        if not 1 <= string_no <= STRING_COUNT:
            raise ValueError(f"弦号必须在 1..{STRING_COUNT} 之间，收到 {string_no}")
        return self.open_strings[STRING_COUNT - string_no]

    def midi_at(self, string_no: int, fret: int) -> int:
        """某弦某品的实际发声音高（已计入变调夹）。"""
        if fret < 0:
            raise ValueError("品位不能为负")
        return self.open_midi(string_no) + self.capo_fret + fret


STANDARD = Tuning(
    id="standard",
    name_zh="标准调弦",
    name_en="Standard",
    open_strings=STANDARD_OPEN_STRINGS,
)

#: 已实现的调弦（本期只有标准调弦）
TUNINGS: dict[str, Tuning] = {STANDARD.id: STANDARD}


def get_tuning(tuning_id: str = "standard") -> Tuning:
    try:
        return TUNINGS[tuning_id]
    except KeyError as exc:
        raise KeyError(f"未知调弦：{tuning_id}") from exc
