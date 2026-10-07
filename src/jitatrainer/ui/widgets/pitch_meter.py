"""实时音高表：显示当前音名与音分偏差。

需求 FR-551 要求"弹奏时始终显示当前音名 + 音分偏差"，调音器与练习屏共用此控件。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ...core.theory.notes import NOTE_NAMES, hz_to_midi, midi_name, nearest_cents_offset
from ..theme import COLORS

#: 指针满量程（音分）
FULL_SCALE_CENTS = 50.0
#: 判定为"准"的范围
IN_TUNE_CENTS = 5.0


class PitchMeter(QWidget):
    """横向音分偏差表。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._hz: float | None = None
        self._confidence: float = 0.0
        self._target_midi: int | None = None
        self._target_label: str = ""
        self._tolerance: float = 25.0

    # ------------------------------------------------------------------ 数据
    def set_pitch(self, hz: float | None, confidence: float = 0.0) -> None:
        self._hz = hz if hz and hz > 0 else None
        self._confidence = confidence
        self.update()

    def clear(self) -> None:
        self._hz = None
        self._confidence = 0.0
        self.update()

    def set_target(self, midi: int | None, label: str = "", tolerance: float = 25.0) -> None:
        self._target_midi = midi
        self._target_label = label
        self._tolerance = tolerance
        self.update()

    # ------------------------------------------------------------------ 状态
    @property
    def detected_midi(self) -> float | None:
        return hz_to_midi(self._hz) if self._hz else None

    @property
    def target_midi(self) -> int | None:
        return self._target_midi

    @property
    def cents(self) -> float | None:
        """相对**目标音**的音分偏差；无目标时相对最近同名音。"""
        if self._hz is None:
            return None
        if self._target_midi is not None:
            return 100.0 * (hz_to_midi(self._hz) - self._target_midi)
        return nearest_cents_offset(self._hz, round(hz_to_midi(self._hz)) % 12)

    @property
    def in_tune(self) -> bool:
        cents = self.cents
        return cents is not None and abs(cents) <= IN_TUNE_CENTS

    def detected_name(self) -> str:
        if self._hz is None:
            return "—"
        return midi_name(round(hz_to_midi(self._hz)))

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        margin = 24
        bar_y = rect.height() - 46
        bar_h = 16
        bar = QRectF(margin, bar_y, rect.width() - margin * 2, bar_h)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["panel"]))
        painter.drawRoundedRect(bar, 6, 6)

        # 容差带
        tol_ratio = min(1.0, self._tolerance / FULL_SCALE_CENTS)
        tol_w = bar.width() * tol_ratio / 2
        center_x = bar.center().x()
        tol_rect = QRectF(center_x - tol_w, bar.top(), tol_w * 2, bar.height())
        painter.setBrush(QColor(COLORS["good"] + "33"))
        painter.drawRoundedRect(tol_rect, 4, 4)

        # 中心刻度
        painter.setPen(QPen(QColor(COLORS["text_faint"]), 1))
        painter.drawLine(int(center_x), int(bar.top() - 8), int(center_x), int(bar.bottom() + 8))

        cents = self.cents
        if cents is not None:
            clamped = max(-FULL_SCALE_CENTS, min(FULL_SCALE_CENTS, cents))
            ratio = clamped / FULL_SCALE_CENTS
            x = center_x + ratio * (bar.width() / 2)
            if abs(cents) <= IN_TUNE_CENTS:
                color = COLORS["good"]
            elif abs(cents) <= self._tolerance:
                color = COLORS["warn"]
            else:
                color = COLORS["bad"]
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(QRectF(x - 5, bar.top() - 8, 10, bar.height() + 16), 4, 4)

        # 音名
        note_font = QFont(self.font())
        note_font.setPointSizeF(max(20.0, rect.height() * 0.22))
        note_font.setBold(True)
        painter.setFont(note_font)
        painter.setPen(QColor(COLORS["text"]))
        painter.drawText(
            QRectF(0, 8, rect.width(), rect.height() * 0.34),
            Qt.AlignmentFlag.AlignCenter,
            self.detected_name(),
        )

        # 目标与偏差文字
        info_font = QFont(self.font())
        info_font.setPointSizeF(max(10.0, rect.height() * 0.075))
        painter.setFont(info_font)
        painter.setPen(QColor(COLORS["text_dim"]))
        parts: list[str] = []
        if self._target_label:
            parts.append(self._target_label)
        if cents is not None:
            parts.append(f"{cents:+.1f} 音分")
            if abs(cents) <= IN_TUNE_CENTS:
                parts.append("准")
            elif cents < 0:
                parts.append("偏低")
            else:
                parts.append("偏高")
        elif self._hz is None:
            parts.append("未检测到音高")
        if self._confidence:
            parts.append(f"置信度 {self._confidence:.2f}")
        painter.drawText(
            QRectF(0, rect.height() - 30, rect.width(), 24),
            Qt.AlignmentFlag.AlignCenter,
            "   ".join(parts),
        )


__all__ = ["PitchMeter", "FULL_SCALE_CENTS", "IN_TUNE_CENTS", "NOTE_NAMES"]
