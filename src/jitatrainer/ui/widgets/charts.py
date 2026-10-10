"""自绘图表控件（不依赖 QtCharts，体积更小）。

三个控件：折线图、柱状图、热力图。都只接受"数据 + 标签"，绘制逻辑与业务无关。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..theme import COLORS

AXIS_COLOR = QColor(COLORS["border"])
TEXT_COLOR = QColor(COLORS["text_dim"])
FAINT_COLOR = QColor(COLORS["text_faint"])
GRID_COLOR = QColor(COLORS["border"])
GRID_COLOR.setAlpha(90)


class _ChartBase(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.title = ""

    def set_title(self, title: str) -> None:
        self.title = title
        self.update()

    def _plot_rect(self) -> QRectF:
        """绘图区。

        上边距留 40px：标题占 y 4~24，最上面的 Y 轴刻度占 (top-9)~(top+9)。
        原来上边距只有 30，两者会在 y 21~24 处叠一小条（渲染成图片能看到）。
        """
        rect = QRectF(self.rect()).adjusted(48, 40, -14, -26)
        return rect

    def _draw_title(self, painter: QPainter) -> None:
        if not self.title:
            return
        font = QFont(self.font())
        font.setPointSizeF(max(9.5, self.font().pointSizeF()))
        painter.setFont(font)
        painter.setPen(TEXT_COLOR)
        painter.drawText(QRectF(8, 4, self.width() - 16, 20), Qt.AlignmentFlag.AlignLeft, self.title)

    def _draw_empty(self, painter: QPainter, message: str = "暂无数据") -> None:
        painter.setPen(FAINT_COLOR)
        painter.drawText(self._plot_rect(), Qt.AlignmentFlag.AlignCenter, message)


class LineChart(_ChartBase):
    """折线图（正确率曲线、反应时间曲线）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._points: list[tuple[str, float | None]] = []
        self._color = QColor(COLORS["accent"])
        self._y_min = 0.0
        self._y_max = 100.0
        self._suffix = ""

    def set_series(
        self,
        points: list[tuple[str, float | None]],
        *,
        y_min: float | None = None,
        y_max: float | None = None,
        color: str | None = None,
        suffix: str = "",
    ) -> None:
        self._points = points
        values = [value for _label, value in points if value is not None]
        if y_min is None:
            y_min = 0.0 if not values else min(values) * 0.9
        if y_max is None:
            y_max = 100.0 if not values else max(values) * 1.1
        if y_max <= y_min:
            y_max = y_min + 1.0
        self._y_min, self._y_max = y_min, y_max
        if color:
            self._color = QColor(color)
        self._suffix = suffix
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw_title(painter)
        rect = self._plot_rect()
        if not self._points:
            self._draw_empty(painter)
            return

        painter.setPen(QPen(AXIS_COLOR, 1))
        painter.drawLine(int(rect.left()), int(rect.bottom()), int(rect.right()), int(rect.bottom()))
        painter.drawLine(int(rect.left()), int(rect.top()), int(rect.left()), int(rect.bottom()))

        # 横向网格与 Y 轴刻度（3 条）
        font = QFont(self.font())
        font.setPointSizeF(max(8.0, self.font().pointSizeF() - 1))
        painter.setFont(font)
        for index in range(3):
            ratio = index / 2
            y = rect.bottom() - rect.height() * ratio
            painter.setPen(QPen(GRID_COLOR, 1, Qt.PenStyle.DashLine))
            painter.drawLine(int(rect.left()), int(y), int(rect.right()), int(y))
            painter.setPen(FAINT_COLOR)
            value = self._y_min + (self._y_max - self._y_min) * ratio
            painter.drawText(
                QRectF(0, y - 9, rect.left() - 6, 18),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{value:.0f}{self._suffix}",
            )

        count = len(self._points)
        step = rect.width() / max(1, count - 1)

        def position(index: int, value: float) -> QPointF:
            ratio = (value - self._y_min) / (self._y_max - self._y_min)
            ratio = max(0.0, min(1.0, ratio))
            return QPointF(rect.left() + index * step, rect.bottom() - rect.height() * ratio)

        # 折线（跳过缺失点）
        polygon: list[QPointF] = []
        for index, (_label, value) in enumerate(self._points):
            if value is None:
                if len(polygon) > 1:
                    painter.setPen(QPen(self._color, 2))
                    painter.drawPolyline(QPolygonF(polygon))
                polygon = []
                continue
            polygon.append(position(index, value))
        if len(polygon) > 1:
            painter.setPen(QPen(self._color, 2))
            painter.drawPolyline(QPolygonF(polygon))

        # 数据点
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        for index, (_label, value) in enumerate(self._points):
            if value is None:
                continue
            painter.drawEllipse(position(index, value), 2.6, 2.6)

        # X 轴标签：只画首尾与中间，避免拥挤
        painter.setPen(FAINT_COLOR)
        for index in {0, count // 2, count - 1}:
            if 0 <= index < count:
                label = self._points[index][0]
                x = rect.left() + index * step
                painter.drawText(
                    QRectF(x - 40, rect.bottom() + 4, 80, 18),
                    Qt.AlignmentFlag.AlignCenter,
                    label,
                )


class BarChart(_ChartBase):
    """柱状图（逐日练习时长）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bars: list[tuple[str, float]] = []
        self._color = QColor(COLORS["good"])
        self._suffix = ""

    def set_bars(self, bars: list[tuple[str, float]], *, color: str | None = None, suffix: str = "") -> None:
        self._bars = bars
        if color:
            self._color = QColor(color)
        self._suffix = suffix
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw_title(painter)
        rect = self._plot_rect()

        values = [value for _label, value in self._bars]
        if not self._bars or max(values or [0]) <= 0:
            self._draw_empty(painter, "这段时间还没有练习记录")
            return

        top = max(values) * 1.15
        painter.setPen(QPen(AXIS_COLOR, 1))
        painter.drawLine(int(rect.left()), int(rect.bottom()), int(rect.right()), int(rect.bottom()))

        count = len(self._bars)
        slot = rect.width() / count
        bar_width = max(2.0, slot * 0.62)

        font = QFont(self.font())
        font.setPointSizeF(max(8.0, self.font().pointSizeF() - 1))
        painter.setFont(font)

        for index, (label, value) in enumerate(self._bars):
            height = rect.height() * (value / top) if top else 0.0
            x = rect.left() + index * slot + (slot - bar_width) / 2
            bar = QRectF(x, rect.bottom() - height, bar_width, height)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._color if value > 0 else QColor(COLORS["panel_alt"]))
            painter.drawRoundedRect(bar, 3, 3)

        painter.setPen(FAINT_COLOR)
        painter.drawText(
            QRectF(0, 8, rect.left() - 6, 18),
            Qt.AlignmentFlag.AlignRight,
            f"{top:.0f}{self._suffix}",
        )
        for index in {0, count // 2, count - 1}:
            if 0 <= index < count:
                x = rect.left() + index * slot + slot / 2
                painter.drawText(
                    QRectF(x - 40, rect.bottom() + 4, 80, 18),
                    Qt.AlignmentFlag.AlignCenter,
                    self._bars[index][0],
                )


class HeatmapChart(_ChartBase):
    """热力图（错音分布：音名 × 把位）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(220)
        self._rows: list[str] = []
        self._cols: list[str] = []
        self._values: list[list[float | None]] = []
        self._labels: list[list[str]] = []

    def set_grid(
        self,
        rows: list[str],
        cols: list[str],
        values: list[list[float | None]],
        labels: list[list[str]] | None = None,
    ) -> None:
        self._rows, self._cols, self._values = rows, cols, values
        self._labels = labels or [["" for _ in cols] for _ in rows]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw_title(painter)

        if not self._rows or not self._cols:
            self._draw_empty(painter, "还没有错音数据")
            return

        rect = QRectF(self.rect()).adjusted(46, 34, -12, -22)
        cell_w = rect.width() / len(self._cols)
        cell_h = rect.height() / len(self._rows)

        font = QFont(self.font())
        font.setPointSizeF(max(8.0, self.font().pointSizeF() - 1))
        painter.setFont(font)

        for row_index, row_label in enumerate(self._rows):
            painter.setPen(TEXT_COLOR)
            painter.drawText(
                QRectF(0, rect.top() + row_index * cell_h, rect.left() - 6, cell_h),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                row_label,
            )
            for col_index, _col_label in enumerate(self._cols):
                value = self._values[row_index][col_index]
                cell = QRectF(
                    rect.left() + col_index * cell_w + 1,
                    rect.top() + row_index * cell_h + 1,
                    cell_w - 2,
                    cell_h - 2,
                )
                if value is None:
                    color = QColor(COLORS["panel"])
                else:
                    # 绿 → 黄 → 红
                    ratio = max(0.0, min(1.0, value))
                    if ratio < 0.5:
                        blend = ratio / 0.5
                        color = QColor(
                            int(62 + (232 - 62) * blend),
                            int(207 + (179 - 207) * blend),
                            int(142 + (57 - 142) * blend),
                        )
                    else:
                        blend = (ratio - 0.5) / 0.5
                        color = QColor(
                            int(232 + (224 - 232) * blend),
                            int(179 + (92 - 179) * blend),
                            int(57 + (92 - 57) * blend),
                        )
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                painter.drawRoundedRect(cell, 3, 3)

                text = self._labels[row_index][col_index]
                if text:
                    painter.setPen(QColor("#0d1114") if value is not None and value > 0.45 else QColor(COLORS["text"]))
                    painter.drawText(cell, Qt.AlignmentFlag.AlignCenter, text)

        painter.setPen(TEXT_COLOR)
        for col_index, col_label in enumerate(self._cols):
            x = rect.left() + col_index * cell_w
            painter.drawText(
                QRectF(x, rect.top() - 20, cell_w, 18), Qt.AlignmentFlag.AlignCenter, col_label
            )


__all__ = ["BarChart", "HeatmapChart", "LineChart"]
