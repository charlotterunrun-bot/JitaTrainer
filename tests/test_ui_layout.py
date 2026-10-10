"""界面几何不重叠测试。

背景：用户反馈"界面有时候会出现显示的内容字符有重叠"。渲染成图片逐一核对后，
确认了两处真实重叠：

1. 六线谱的**音名提示画在底部**，而第 6 弦的音符框正好在底部 → 压在音符上；
2. 统计页**一行塞 6 个 KPI**，长标题在 1000px 窗口下被挤到一起。

这些是"只在特定内容/尺寸下才出现"的问题，靠人工看容易漏，因此把几何关系固化成断言。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QRectF  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jitatrainer.core.theory.fretboard import Position  # noqa: E402
from jitatrainer.ui.widgets.charts import BarChart, HeatmapChart, LineChart  # noqa: E402
from jitatrainer.ui.widgets.notation_tab import NotationTab  # noqa: E402


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


class TestNotationTabGeometry:
    """六线谱：音符框必须完全在面板内，且不与页眉（提示/来源标签）相交。"""

    @pytest.mark.parametrize("string,fret,midi", [(6, 0, 40), (6, 3, 43), (1, 12, 76), (3, 5, 60)])
    @pytest.mark.parametrize("show_hint", [False, True])
    def test_note_box_inside_panel_and_clear_of_header(
        self, app, string: int, fret: int, midi: int, show_hint: bool  # noqa: ARG002
    ) -> None:
        widget = NotationTab()
        widget.resize(760, 420)
        widget.set_question(
            Position(string=string, fret=fret, midi=midi),
            note_name="E2",
            show_note_name=show_hint,
            source_label="回炉",
        )
        geometry = widget.layout_geometry()
        assert geometry is not None

        panel = geometry["panel"]
        box = geometry["note_box"]
        header = geometry["header"]

        assert panel.contains(box), f"音符框超出面板：{box} 不在 {panel} 内"
        assert not box.intersects(header), f"音符框与页眉重叠：{box} ∩ {header}"
        # 弦号列在音符左侧，不能压到音符框
        assert box.left() > geometry["lines_left"]

    def test_hint_and_source_label_do_not_overlap(self, app) -> None:  # noqa: ARG002
        widget = NotationTab()
        widget.resize(760, 420)
        widget.set_question(
            Position(string=4, fret=2, midi=52), note_name="E3", show_note_name=True, source_label="复习"
        )
        geometry = widget.layout_geometry()
        assert geometry is not None
        hint = geometry["hint_rect"]
        source = geometry["source_rect"]
        assert hint is not None and source is not None
        assert not hint.intersects(source), f"音名提示与来源标签重叠：{hint} ∩ {source}"

    def test_lines_fit_inside_panel(self, app) -> None:  # noqa: ARG002
        widget = NotationTab()
        widget.resize(600, 300)
        widget.set_question(Position(string=6, fret=12, midi=52))
        geometry = widget.layout_geometry()
        assert geometry is not None
        assert geometry["first_line_y"] > geometry["panel"].top()
        assert geometry["last_line_y"] < geometry["panel"].bottom()

    def test_small_widget_does_not_collapse(self, app) -> None:  # noqa: ARG002
        """极小的控件也不能算出负数或塌陷的几何。"""
        widget = NotationTab()
        widget.resize(320, 200)
        widget.set_question(Position(string=1, fret=9, midi=64), note_name="E4", show_note_name=True)
        geometry = widget.layout_geometry()
        assert geometry is not None
        assert geometry["spacing"] > 5
        assert geometry["panel"].contains(geometry["note_box"])

    def test_no_question_returns_no_geometry(self, app) -> None:  # noqa: ARG002
        widget = NotationTab()
        widget.resize(400, 300)
        assert widget.layout_geometry() is None


class TestChartGeometry:
    """图表：标题与最上面的 Y 轴刻度不能叠在一起。"""

    def test_line_chart_title_clear_of_top_tick(self, app) -> None:  # noqa: ARG002
        chart = LineChart()
        chart.resize(500, 200)
        chart.set_title("正确率曲线")
        chart.set_series([("01-01", 50.0), ("01-02", 80.0)])
        plot = chart._plot_rect()  # noqa: SLF001
        title_bottom = 4 + 20  # 标题矩形 y 4..24
        top_tick = QRectF(0, plot.top() - 9, plot.left() - 6, 18)
        assert top_tick.top() >= title_bottom, (
            f"标题与顶部刻度重叠：标题到 {title_bottom}，刻度从 {top_tick.top()} 开始"
        )

    def test_bar_chart_has_room_for_labels(self, app) -> None:  # noqa: ARG002
        chart = BarChart()
        chart.resize(500, 200)
        chart.set_bars([("01-01", 12.0), ("01-02", 0.0)])
        plot = chart._plot_rect()  # noqa: SLF001
        assert plot.height() > 60, "柱状图绘图区太矮，标签会挤在一起"

    def test_heatmap_grid_inside_widget(self, app) -> None:  # noqa: ARG002
        chart = HeatmapChart()
        chart.resize(600, 300)
        chart.set_grid(
            ["C", "D"], ["L1", "L2"], [[0.5, None], [0.1, 0.9]], [["3", ""], ["1", "2"]]
        )
        rect = QRectF(chart.rect())
        assert rect.width() > 100 and rect.height() > 100
        # 行标签在左侧 46px 内，格子从 46 开始
        assert chart._plot_rect().left() >= 40  # noqa: SLF001
