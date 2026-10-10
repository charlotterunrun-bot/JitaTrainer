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

    # ------------------------------------------------------------------ 布局
    def layout_geometry(self) -> dict | None:
        """算出绘制用的几何，供绘制与**几何测试**共用。

        把布局算法独立出来，测试才能断言"音符框不与页眉重叠""不超出面板"，
        而不是靠肉眼看截图（这类重叠只在特定弦/特定开关下出现，很容易漏）。
        """
        if self._position is None:
            return None

        panel = QRectF(self.rect()).adjusted(18, 18, -18, -18)
        left = panel.left() + panel.width() * 0.16
        right = panel.right() - panel.width() * 0.12

        # 顶部预留一条"页眉"，放音名提示与来源标签。
        #
        # 原来音名提示画在谱面**底部**，而第 6 弦的音符框正好在底部——两者会重叠
        # （渲染成图片后看得一清二楚）。现在把提示放到谱面上方的空白区，
        # 与来源标签分列左右。
        has_hint = bool(self._show_note_name and self._note_name)
        has_source = bool(self._source_label)
        needs_header = has_hint or has_source
        header_height = panel.height() * (0.15 if needs_header else 0.04)

        # 线距要同时给上下留出音符框的高度，否则第 1 弦的框会顶进页眉、
        # 第 6 弦的框会超出面板底边被裁掉（两种情况都实测过）。
        # 音符框半高 ≈ 0.675 × 线距，上下各留 0.70 × 线距：
        #   5 × spacing + 0.70 × spacing + 0.70 × spacing = 可用高度
        usable = max(60.0, panel.height() - header_height)
        spacing = usable / 6.4
        top = panel.top() + header_height + spacing * 0.70
        span = spacing * (STRING_COUNT - 1)

        index = self._position.string - 1
        y = top + index * spacing
        x = left + (right - left) * 0.5

        # 音符框尺寸按字号估算（与绘制一致：用同一个字体度量）
        font = QFont(self.font())
        font.setBold(True)
        font.setPointSizeF(max(18.0, spacing * 1.15))
        from PySide6.QtGui import QFontMetricsF

        metrics = QFontMetricsF(font)
        text = str(self._position.fret)
        text_width = max(metrics.horizontalAdvance(text) + spacing * 0.7, spacing * 1.5)
        text_height = spacing * 1.35
        box = QRectF(x - text_width / 2, y - text_height / 2, text_width, text_height)

        header = QRectF(panel.left(), panel.top(), panel.width(), header_height)
        hint_rect = (
            QRectF(panel.left() + 14, panel.top(), panel.width() * 0.48, header_height)
            if has_hint
            else None
        )
        source_rect = (
            QRectF(
                panel.left() + panel.width() * 0.5,
                panel.top(),
                panel.width() * 0.5 - 14,
                header_height,
            )
            if has_source
            else None
        )

        return {
            "panel": panel,
            "header": header,
            "header_height": header_height,
            "lines_left": left,
            "lines_right": right,
            "spacing": spacing,
            "first_line_y": top,
            "last_line_y": top + span,
            "note_box": box,
            "note_index": index,
            "hint_rect": hint_rect,
            "source_rect": source_rect,
        }

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()

        panel = QRectF(rect).adjusted(18, 18, -18, -18)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["panel"]))
        painter.drawRoundedRect(panel, 12, 12)

        geometry = self.layout_geometry()
        if geometry is None:
            painter.setPen(QColor(COLORS["text_faint"]))
            font = QFont(self.font())
            font.setPointSizeF(max(12.0, panel.height() * 0.06))
            painter.setFont(font)
            painter.drawText(panel, Qt.AlignmentFlag.AlignCenter, "—")
            return

        left = geometry["lines_left"]
        right = geometry["lines_right"]
        top = geometry["first_line_y"]
        spacing = geometry["spacing"]
        header_height = geometry["header_height"]

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

        # 音符位置（几何已在 layout_geometry 里算好，绘制与测试共用同一份）
        index = geometry["note_index"]
        y = top + index * spacing
        box = geometry["note_box"]

        note_font = QFont(self.font())
        note_font.setBold(True)
        note_font.setPointSizeF(max(18.0, spacing * 1.15))
        painter.setFont(note_font)

        text = str(self._position.fret)

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

        # 音名提示：放在页眉左侧（默认关闭；开启后本题不计入熟练度）
        if self._show_note_name and self._note_name:
            hint_font = QFont(self.font())
            hint_font.setPointSizeF(max(11.0, spacing * 0.5))
            painter.setFont(hint_font)
            painter.setPen(QColor(COLORS["warn"]))
            painter.drawText(
                QRectF(panel.left() + 14, panel.top(), panel.width() * 0.48, header_height),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"音名提示：{self._note_name}",
            )

        # 来源标签（复习/回炉）：页眉右侧，与音名提示分列，不会互相压字
        if self._source_label:
            tag_font = QFont(self.font())
            tag_font.setPointSizeF(max(9.0, spacing * 0.4))
            painter.setFont(tag_font)
            painter.setPen(QColor(COLORS["text_dim"]))
            painter.drawText(
                QRectF(
                    panel.left() + panel.width() * 0.5,
                    panel.top(),
                    panel.width() * 0.5 - 14,
                    header_height,
                ),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                self._source_label,
            )


__all__ = ["NotationTab"]
