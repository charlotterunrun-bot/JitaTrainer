"""音频事件定义（音频线程与分析线程之间的数据契约）。"""

from __future__ import annotations

from dataclasses import dataclass

from ..theory.notes import hz_to_midi, nearest_cents_offset, pitch_class as pitch_class_of_midi


@dataclass(frozen=True, slots=True)
class PitchEvent:
    """一帧的检测结果。

    Attributes:
        t: 单调时钟（秒）
        hz: 估计基频；0 表示本帧无有效音高
        confidence: YIN 置信度
        rms_db: 本帧音量（dBFS）
        is_onset: 是否被判定为拨弦起音
        harmonic_corrected: 是否被谐波校验修正过
    """

    t: float
    hz: float = 0.0
    confidence: float = 0.0
    rms_db: float = -float("inf")
    is_onset: bool = False
    harmonic_corrected: bool = False

    @property
    def valid(self) -> bool:
        return self.hz > 0.0

    @property
    def midi(self) -> float:
        """MIDI 音高（小数）；无效时为 nan。"""
        return hz_to_midi(self.hz) if self.valid else float("nan")

    @property
    def pitch_class(self) -> int | None:
        """最近的音名序号；无效时为 None。"""
        return pitch_class_of_midi(round(self.midi)) if self.valid else None

    @property
    def cents(self) -> float:
        """与最近同名音的音分偏差；无效时为 nan。"""
        if not self.valid:
            return float("nan")
        pc = self.pitch_class
        if pc is None:
            return float("nan")
        return nearest_cents_offset(self.hz, pc)
