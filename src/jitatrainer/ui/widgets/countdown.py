"""倒计时条：显示"距离自动换下一个音还有多久"。

自绘的原因与其它图表控件一致（不引入额外依赖），同时避免用 QLabel +
QProgressBar 拼出来时文字被挤到与其它元素重叠（实测过的界面问题）。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..theme import COLORS

#: 剩余时间低于该比例时转为警示色
WARN_RATIO = 0.35
#: 剩余时间低于该比例时转为危险色
DANGER_RATIO = 0.15


class CountdownBar(QWidget):
    """一条细进度条 + 剩余秒数。

    - ``show_countdown(total_ms)``：进入倒计时显示
    - ``show_manual()``：手动推进模式，不显示倒计时，只提示按键
    - ``clear()``：隐藏
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(26)
        self.setMinimumWidth(150)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._mode = "hidden"  # hidden | countdown | manual
        self._remaining_ms = 0.0
        self._total_ms = 1.0
        self._manual_hint = "按 S 或回车进入下一个音"

    # ------------------------------------------------------------------ 数据
    def show_countdown(self, remaining_ms: float, total_ms: float) -> None:
        self._mode = "countdown"
        self._remaining_ms = max(0.0, float(remaining_ms))
        self._total_ms = max(1.0, float(total_ms))
        if not self.isVisible():
            self.setVisible(True)
        self.update()

    def show_manual(self, hint: str = "") -> None:
        self._mode = "manual"
        if hint:
            self._manual_hint = hint
        if not self.isVisible():
            self.setVisible(True)
        self.update()

    def clear(self) -> None:
        self._mode = "hidden"
        self.update()

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def remaining_ms(self) -> float:
        return self._remaining_ms

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        if self._mode == "hidden":
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 3, -1, -3)

        font = QFont(self.font())
        font.setPointSizeF(max(9.0, self.font().pointSizeF()))
        painter.setFont(font)

        if self._mode == "manual":
            painter.setPen(QColor(COLORS["text_dim"]))
            painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._manual_hint)
            return

        ratio = max(0.0, min(1.0, self._remaining_ms / self._total_ms))
        if ratio <= DANGER_RATIO:
            color = QColor(COLORS["bad"])
        elif ratio <= WARN_RATIO:
            color = QColor(COLORS["warn"])
        else:
            color = QColor(COLORS["good"])

        seconds = self._remaining_ms / 1000.0
        label = f"{seconds:.1f}s"
        metrics = painter.fontMetrics()
        text_width = metrics.horizontalAdvance(label) + 10

        # 文字放右侧，进度条占左侧剩余空间，两者不会重叠
        bar = QRectF(rect.left(), rect.center().y() - 3, max(20.0, rect.width() - text_width), 6)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["panel_alt"]))
        painter.drawRoundedRect(bar, 3, 3)

        filled = QRectF(bar)
        filled.setWidth(bar.width() * ratio)
        painter.setBrush(color)
        painter.drawRoundedRect(filled, 3, 3)

        painter.setPen(color)
        painter.drawText(
            QRectF(rect.right() - text_width, rect.top(), text_width, rect.height()),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            label,
        )


__all__ = ["CountdownBar"]
