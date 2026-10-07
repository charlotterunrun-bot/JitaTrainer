"""六线谱（TAB）控件：把 ``RenderSpec`` 画出来。

需求 FR-520：6 条线 + 品格数字；默认隐藏音名字母；全屏大字模式下 1 米外可读。
控件只认 ``RenderSpec``，未来加五线谱/和弦图是新增控件，不改练习模块。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ...core.theory.fretboard import Position
from ..theme import COLORS

STRING_COUNT = 6
#: 线条占控件高度的比例
LINES_SPAN = 0.62


class NotationTab(QWidget):
    """六线谱控件。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(420, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._position: Position | None = None
        self._note_name: str = ""
        self._show_note_name = False
        self._highlight = False
        self._source_label: str = ""
        self._state: str = "idle"  # idle | correct | wrong
        self._show_string_numbers = True

    # ------------------------------------------------------------------ 数据
    def set_question(
        self,
        position: Position,
        *,
        note_name: str = "",
        show_note_name: bool = False,
        source_label: str = "",
    ) -> None:
        self._position = position
        self._note_name = note_name
        self._show_note_name = show_note_name
        self._source_label = source_label
        self._state = "idle"
        self.update()

    def set_state(self, state: str) -> None:
        """``idle`` / ``correct`` / ``wrong``。"""
        self._state = state
        self.update()

    def set_highlight(self, enabled: bool) -> None:
        """高亮标准位置（答错时用来提示答案）。"""
        self._highlight = enabled
        self.update()

    def clear(self) -> None:
        self._position = None
        self._note_name = ""
        self._source_label = ""
        self._state = "idle"
        self.update()

    def set_show_string_numbers(self, enabled: bool) -> None:
        self._show_string_numbers = enabled
        self.update()

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()

        panel = QRectF(rect).adjusted(18, 18, -18, -18)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["panel"]))
        painter.drawRoundedRect(panel, 12, 12)

        if self._position is None:
            painter.setPen(QColor(COLORS["text_faint"]))
            font = QFont(self.font())
            font.setPointSizeF(max(12.0, panel.height() * 0.06))
            painter.setFont(font)
            painter.drawText(panel, Qt.AlignmentFlag.AlignCenter, "—")
            return

        left = panel.left() + panel.width() * 0.16
        right = panel.right() - panel.width() * 0.12
        top = panel.top() + panel.height() * (1 - LINES_SPAN) / 2
        span = panel.height() * LINES_SPAN
        spacing = span / (STRING_COUNT - 1)

        # 六条线：最上面是第 1 弦（最细）
        for index in range(STRING_COUNT):
            y = top + index * spacing
            string_no = index + 1
            width = 1.0 + (string_no - 1) * 0.45  # 粗弦画粗一点
            painter.setPen(QPen(QColor(COLORS["border"]), width))
            painter.drawLine(int(left), int(y), int(right), int(y))

        # 弦号
        if self._show_string_numbers:
            label_font = QFont(self.font())
            label_font.setPointSizeF(max(9.0, spacing * 0.44))
            painter.setFont(label_font)
            painter.setPen(QColor(COLORS["text_faint"]))
            for index in range(STRING_COUNT):
                y = top + index * spacing
                painter.drawText(
                    QRectF(panel.left(), y - spacing / 2, left - panel.left() - 6, spacing),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    str(index + 1),
                )

        # 音符位置
        index = self._position.string - 1
        y = top + index * spacing
        x = left + (right - left) * 0.5

        note_font = QFont(self.font())
        note_font.setBold(True)
        note_font.setPointSizeF(max(18.0, spacing * 1.15))
        painter.setFont(note_font)

        text = str(self._position.fret)
        metrics = painter.fontMetrics()
        text_width = max(metrics.horizontalAdvance(text) + spacing * 0.7, spacing * 1.5)
        text_height = spacing * 1.35
        box = QRectF(x - text_width / 2, y - text_height / 2, text_width, text_height)

        if self._state == "correct":
            fill = QColor(COLORS["good"])
        elif self._state == "wrong":
            fill = QColor(COLORS["bad"])
        elif self._highlight:
            fill = QColor(COLORS["accent"])
        else:
            fill = QColor(COLORS["panel_alt"])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(box, 8, 8)

        painter.setPen(QColor("#ffffff") if self._state != "idle" or self._highlight else QColor(COLORS["text"]))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

        # 音名提示（默认关闭，开启后本题不计入熟练度）
        if self._show_note_name and self._note_name:
            hint_font = QFont(self.font())
            hint_font.setPointSizeF(max(11.0, spacing * 0.5))
            painter.setFont(hint_font)
            painter.setPen(QColor(COLORS["warn"]))
            painter.drawText(
                QRectF(panel.left(), panel.bottom() - spacing * 1.6, panel.width(), spacing * 1.2),
                Qt.AlignmentFlag.AlignCenter,
                f"音名提示：{self._note_name}",
            )

        # 来源标签（复习/回炉）
        if self._source_label:
            tag_font = QFont(self.font())
            tag_font.setPointSizeF(max(9.0, spacing * 0.4))
            painter.setFont(tag_font)
            painter.setPen(QColor(COLORS["text_dim"]))
            painter.drawText(
                QRectF(panel.right() - panel.width() * 0.3, panel.top() + 6, panel.width() * 0.28, spacing),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                self._source_label,
            )


__all__ = ["NotationTab"]
