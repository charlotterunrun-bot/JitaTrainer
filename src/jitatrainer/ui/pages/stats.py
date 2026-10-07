"""统计报告页（M4）。

数据来自 ``data/analytics.py``（只读查询），图表是自绘控件，导出走 ``data/export.py``。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ... import paths
from ...core.theory.notes import NOTE_NAMES, pitch_class_name
from ...data.analytics import AnalyticsRepository
from ...data.export import export_attempts_csv, export_daily_csv
from ...data.settings import Settings
from ...scheduling.advice import AdviceConfig
from ..widgets.charts import BarChart, HeatmapChart, LineChart

PERIOD_OPTIONS = ((7, "最近 7 天"), (30, "最近 30 天"), (90, "最近 90 天"))


class StatsPage(QWidget):
    """历史统计页。"""

    back_requested = Signal()

    def __init__(
        self,
        settings: Settings,
        tr,
        *,
        profile_id: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.tr = tr
        self.profile_id = profile_id or 1
        self.repository = AnalyticsRepository(settings.db, self.profile_id)
        self._days = 30
        self._build_ui()

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel(self.tr("stats.title"))
        title.setObjectName("sectionTitle")
        header.addWidget(title)
        header.addStretch(1)

        header.addWidget(QLabel(self.tr("stats.period")))
        self.period_combo = QComboBox()
        for days, label in PERIOD_OPTIONS:
            self.period_combo.addItem(
                label if self.tr.language == "zh_CN" else f"Last {days} days", days
            )
        self.period_combo.setCurrentIndex(1)
        self.period_combo.currentIndexChanged.connect(self._on_period_changed)
        header.addWidget(self.period_combo)

        self.export_button = QPushButton(self.tr("stats.export_csv"))
        self.export_button.clicked.connect(self.export_csv)
        header.addWidget(self.export_button)

        back = QPushButton(self.tr("common.back"))
        back.clicked.connect(self.back_requested.emit)
        header.addWidget(back)
        layout.addLayout(header)

        self.kpi_grid = QGridLayout()
        self.kpi_grid.setHorizontalSpacing(26)
        layout.addLayout(self.kpi_grid)

        charts = QHBoxLayout()
        charts.setSpacing(14)
        self.accuracy_chart = LineChart()
        self.minutes_chart = BarChart()
        charts.addWidget(self.accuracy_chart, 1)
        charts.addWidget(self.minutes_chart, 1)
        layout.addLayout(charts)

        bottom = QHBoxLayout()
        bottom.setSpacing(14)
        self.heatmap = HeatmapChart()
        bottom.addWidget(self.heatmap, 3)

        weak_box = QFrame()
        weak_box.setStyleSheet(
            "QFrame { background-color: #1c2229; border: 1px solid #2c343d; border-radius: 10px; }"
        )
        weak_layout = QVBoxLayout(weak_box)
        weak_layout.setContentsMargins(14, 12, 14, 12)
        weak_title = QLabel(self.tr("stats.weakest"))
        weak_title.setObjectName("sectionTitle")
        weak_layout.addWidget(weak_title)
        self.weak_label = QLabel("—")
        self.weak_label.setWordWrap(True)
        self.weak_label.setObjectName("dim")
        weak_layout.addWidget(self.weak_label)
        weak_layout.addStretch(1)
        bottom.addWidget(weak_box, 2)
        layout.addLayout(bottom, 1)

        self.status = QLabel("")
        self.status.setObjectName("faint")
        layout.addWidget(self.status)

    # ------------------------------------------------------------------ 数据
    def refresh(self) -> None:
        days = self._days
        try:
            with self.settings.db.connect() as conn:
                series = self.repository.daily_series(conn, days=days)
                cells = self.repository.note_cells(conn)
                comparison = self.repository.compare(conn, days=days)
                overall = self.repository.overall(conn)
                weak = self.repository.weak_notes(conn, limit=5)
        except Exception as exc:  # noqa: BLE001 - 统计失败不影响其他功能
            self.status.setText(f"{self.tr('stats.title')}：{type(exc).__name__}: {exc}")
            return

        self._render_kpi(overall, comparison)
        self._render_charts(series)
        self._render_heatmap(cells)
        self._render_weak(weak)
        self.status.setText("")

    def _render_kpi(self, overall, comparison) -> None:  # noqa: ANN001
        while self.kpi_grid.count():
            item = self.kpi_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        current, previous = comparison.current, comparison.previous
        kpis = [
            (self.tr("stats.total_answered"), str(overall.answered), ""),
            (self.tr("stats.accuracy"), f"{overall.accuracy * 100:.0f}%", ""),
            (
                self.tr("stats.avg_rt"),
                f"{overall.avg_rt_ms} ms" if overall.avg_rt_ms else "—",
                "",
            ),
            (self.tr("stats.total_minutes"), f"{overall.minutes:.0f} min", ""),
            (
                self.tr("stats.vs_previous"),
                f"{current.answered} vs {previous.answered}",
                self._delta_text(current.answered - previous.answered),
            ),
            (
                self.tr("stats.accuracy_delta"),
                f"{current.accuracy * 100:.0f}% vs {previous.accuracy * 100:.0f}%",
                self._delta_text(
                    (current.accuracy - previous.accuracy) * 100, suffix="pt"
                ),
            ),
        ]
        for column, (caption, value, delta) in enumerate(kpis):
            box = QVBoxLayout()
            caption_label = QLabel(caption)
            caption_label.setObjectName("faint")
            value_label = QLabel(value)
            value_label.setStyleSheet("font-size: 17px; font-weight: 600;")
            box.addWidget(caption_label)
            box.addWidget(value_label)
            if delta:
                delta_label = QLabel(delta)
                delta_label.setStyleSheet(
                    f"font-size: 13px; color: {'#3ecf8e' if delta.startswith('+') else '#e05c5c'};"
                )
                box.addWidget(delta_label)
            self.kpi_grid.addLayout(box, 0, column)

    @staticmethod
    def _delta_text(delta: float, *, suffix: str = "") -> str:
        if abs(delta) < 1e-9:
            return ""
        sign = "+" if delta > 0 else ""
        return f"{sign}{delta:.0f}{suffix}" if suffix else f"{sign}{delta:.0f}"

    def _render_charts(self, series) -> None:  # noqa: ANN001
        labels = [point.day.strftime("%m-%d") for point in series]
        answers = [point.answered for point in series]
        accuracy = [(point.accuracy * 100 if point.answered else None) for point in series]
        minutes = [point.minutes for point in series]

        self.accuracy_chart.set_title(self.tr("stats.accuracy_curve"))
        self.accuracy_chart.set_series(
            list(zip(labels, accuracy)), y_min=0.0, y_max=100.0, suffix="%"
        )
        self.minutes_chart.set_title(self.tr("stats.daily_minutes"))
        self.minutes_chart.set_bars(list(zip(labels, minutes)), suffix=" min")
        if sum(answers) == 0:
            self.accuracy_chart.set_title(self.tr("stats.no_data"))

    def _render_heatmap(self, cells) -> None:  # noqa: ANN001
        levels: list[str] = []
        for cell in cells:
            if cell.level_id not in levels:
                levels.append(cell.level_id)
        levels.sort()

        # 没有数据时也画一个空网格，让用户知道这块是干什么的
        if not levels:
            levels = ["L1"]
        levels = levels[:4]

        rows = [pitch_class_name(pc) for pc in range(12)]
        values: list[list[float | None]] = [[None for _ in levels] for _ in range(12)]
        labels: list[list[str]] = [["" for _ in levels] for _ in range(12)]

        for cell in cells:
            if cell.level_id not in levels:
                continue
            row = cell.pitch_class % 12
            column = levels.index(cell.level_id)
            values[row][column] = cell.error_rate
            labels[row][column] = str(cell.wrong)

        self.heatmap.set_title(self.tr("stats.heatmap"))
        self.heatmap.set_grid(rows, levels, values, labels)

    def _render_weak(self, weak) -> None:  # noqa: ANN001
        if not weak:
            self.weak_label.setText(self.tr("stats.no_weak"))
            return
        lines = [
            f"• {pitch_class_name(cell.pitch_class)}（{cell.level_id}）　"
            f"{self.tr('stats.wrong_times', count=cell.wrong)}　"
            f"正确率 {(1 - cell.error_rate) * 100:.0f}%"
            + (f"　{cell.avg_rt_ms} ms" if cell.avg_rt_ms else "")
            for cell in weak
        ]
        self.weak_label.setText("\n".join(lines))

    # ------------------------------------------------------------------ 交互
    def _on_period_changed(self) -> None:
        self._days = int(self.period_combo.currentData() or 30)
        self.refresh()

    def export_csv(self) -> None:
        export_dir = paths.exports_dir()
        export_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        target, _selected = QFileDialog.getSaveFileName(
            self,
            self.tr("stats.export_csv"),
            str(export_dir / f"JitaTrainer-{stamp}.csv"),
            "CSV (*.csv)",
        )
        if not target:
            return

        try:
            detail = export_attempts_csv(
                self.settings.db, self.profile_id, target, module_id=self.repository.module_id
            )
            daily_target = target.replace(".csv", "-daily.csv")
            daily = export_daily_csv(
                self.settings.db,
                self.profile_id,
                daily_target,
                module_id=self.repository.module_id,
                days=max(90, self._days),
            )
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"{type(exc).__name__}: {exc}")
            return

        self.status.setText(
            self.tr(
                "stats.export_done",
                detail=detail.rows,
                daily=daily.rows,
                path=detail.path.name,
            )
        )


__all__ = ["StatsPage", "NOTE_NAMES", "AdviceConfig"]
