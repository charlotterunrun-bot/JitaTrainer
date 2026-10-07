"""音名、MIDI、频率换算。

本项目的判定原则是"只认音名（pitch class）"，因此这里的换算函数是判定、
统计、谱面显示的共同基础，必须有完整单元测试。
"""

from __future__ import annotations

import math

A4_HZ = 440.0
A4_MIDI = 69

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
NOTE_NAMES_FLAT = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")

#: 自然音（C 大调白键音）的音级
NATURAL_PITCH_CLASSES = frozenset({0, 2, 4, 5, 7, 9, 11})


def midi_to_hz(midi: float) -> float:
    """MIDI 音高转频率（Hz）。"""
    return A4_HZ * 2.0 ** ((midi - A4_MIDI) / 12.0)


def hz_to_midi(hz: float) -> float:
    """频率转 MIDI 音高（可能是小数）。"""
    if hz <= 0:
        raise ValueError("频率必须为正数")
    return A4_MIDI + 12.0 * math.log2(hz / A4_HZ)


def pitch_class(midi: int) -> int:
    """MIDI 音高 → 音名序号（0=C … 11=B）。"""
    return int(midi) % 12


def pitch_class_of_hz(hz: float) -> int:
    """频率 → 最近的音名序号。"""
    return int(round(hz_to_midi(hz))) % 12


def octave_of_midi(midi: int) -> int:
    """MIDI 音高的八度编号（C4 = 中央 C）。"""
    return int(midi) // 12 - 1


def midi_name(midi: int, *, prefer_sharp: bool = True) -> str:
    """MIDI 音高 → 带八度的音名，如 40 → ``E2``。"""
    names = NOTE_NAMES if prefer_sharp else NOTE_NAMES_FLAT
    return f"{names[pitch_class(midi)]}{octave_of_midi(midi)}"


def pitch_class_name(pc: int, *, prefer_sharp: bool = True) -> str:
    """音名序号 → 音名，如 4 → ``E``。"""
    names = NOTE_NAMES if prefer_sharp else NOTE_NAMES_FLAT
    return names[pc % 12]


def cents_between(hz_a: float, hz_b: float) -> float:
    """两个频率之间的音分差（a 相对 b）。"""
    if hz_a <= 0 or hz_b <= 0:
        raise ValueError("频率必须为正数")
    return 1200.0 * math.log2(hz_a / hz_b)


def nearest_cents_offset(hz: float, pc: int) -> float:
    """把频率换算成"与最近的同名音之间的音分偏差"。

    判定只认音名，所以偏差必须相对**同音名**计算：例如弹 82.9Hz（E2 = 82.41Hz）
    得到约 +10 音分，而不是与 E1/E3 比较得出上千音分。
    """
    midi = hz_to_midi(hz)
    reference = 12.0 * round((midi - pc) / 12.0) + pc
    return 100.0 * (midi - reference)


def is_natural(pc: int) -> bool:
    """是否为自然音（不含升降号）。"""
    return (pc % 12) in NATURAL_PITCH_CLASSES


def note_set(*, include_accidentals: bool) -> set[int]:
    """返回音名集合（12 个音名，或仅 7 个自然音）。"""
    if include_accidentals:
        return set(range(12))
    return set(NATURAL_PITCH_CLASSES)
