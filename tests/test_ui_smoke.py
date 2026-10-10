"""界面冒烟测试（离屏运行，不需要真实显示器与麦克风）。

覆盖 M2 的关键界面：六线谱控件、练习屏、会话设置与小结对话框。
这些测试抓出过真实缺陷（例如练习屏的相对导入层级写错）。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jitatrainer.core.audio.events import PitchEvent  # noqa: E402
from jitatrainer.core.theory.fretboard import Position  # noqa: E402
from jitatrainer.core.theory.notes import midi_to_hz  # noqa: E402
from jitatrainer.data.db import Database  # noqa: E402
from jitatrainer.data.settings import Settings  # noqa: E402
from jitatrainer.i18n import Translator  # noqa: E402
from jitatrainer.practice import registry  # noqa: E402
from jitatrainer.practice.session import MODE_COUNT, MODE_DURATION, MODE_FREE, SessionConfig  # noqa: E402
from jitatrainer.ui.dialogs import SessionSetupDialog, SessionSummaryDialog  # noqa: E402
from jitatrainer.ui.pages.practice import PracticePage  # noqa: E402
from jitatrainer.ui.widgets.notation_tab import NotationTab  # noqa: E402


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


@pytest.fixture
def settings(tmp_path, app):  # noqa: ARG001
    db = Database(tmp_path / "ui.db")
    db.initialize()
    conn = db.connect()
    profile_id = db.list_profiles(conn)[0]["id"]
    yield Settings(db, conn, profile_id)
    conn.close()


@pytest.fixture
def tr():
    return Translator("zh_CN")


class TestNotationTab:
    def test_renders_question_without_error(self, app) -> None:  # noqa: ARG002
        widget = NotationTab()
        widget.resize(800, 420)
        widget.set_question(
            Position(string=5, fret=2, midi=47), note_name="B2", show_note_name=True, source_label="复习"
        )
        pixmap = QPixmap(QSize(800, 420))
        widget.render(pixmap)
        assert not pixmap.isNull()

    def test_states_render(self, app) -> None:  # noqa: ARG002
        widget = NotationTab()
        widget.resize(400, 300)
        widget.set_question(Position(string=1, fret=3, midi=67), note_name="G4")
        for state in ("idle", "correct", "wrong"):
            widget.set_state(state)
            widget.set_highlight(state == "wrong")
            pixmap = QPixmap(QSize(400, 300))
            widget.render(pixmap)
            assert not pixmap.isNull()

    def test_empty_state(self, app) -> None:  # noqa: ARG002
        widget = NotationTab()
        widget.resize(400, 300)
        widget.clear()
        pixmap = QPixmap(QSize(400, 300))
        widget.render(pixmap)
        assert not pixmap.isNull()


def _drive(page: PracticePage, question_count: int, *, frames: int = 340) -> list[str]:
    """用合成事件把一局练习跑完（每题事件流长于宽容期）。"""
    kinds: list[str] = []
    page._apply(page.session.start())  # noqa: SLF001
    for index in range(question_count):
        question = page.session.current_question
        if question is None:
            break
        hz = midi_to_hz(60 + question.target_pc)
        for frame in range(frames):
            events = page.session.feed(
                PitchEvent(t=index * 4.0 + frame * 0.01, hz=hz, confidence=0.95, is_onset=(frame == 0))
            )
            kinds.extend(event.kind for event in events)
            page._apply(events)  # noqa: SLF001
    return kinds


class TestPracticePage:
    def test_full_session_runs_and_renders(self, settings, tr, app) -> None:  # noqa: ARG002
        module = registry.get_module("pitch_find")
        config = SessionConfig(mode=MODE_COUNT, target_count=3, level_id="L1")
        page = PracticePage(settings, tr, config, module, profile_id=1)
        page.resize(960, 720)

        kinds = _drive(page, 3)
        assert kinds[0] == "correct"
        assert kinds[-1] == "finished"

        summary = page.session.build_summary()
        assert summary.asked == 3
        assert summary.correct_first == 3
        assert page.progress_label.text()
        assert page.accuracy_label.text() == "100%"

    def test_summary_signal_emitted(self, settings, tr, app) -> None:  # noqa: ARG002
        module = registry.get_module("pitch_find")
        config = SessionConfig(mode=MODE_COUNT, target_count=1, level_id="L1")
        page = PracticePage(settings, tr, config, module)
        received: list = []
        page.finished.connect(received.append)

        _drive(page, 1)
        assert received and received[0].asked == 1

    def test_wrong_answer_shows_feedback_and_stays(self, settings, tr, app) -> None:  # noqa: ARG002
        module = registry.get_module("pitch_find")
        config = SessionConfig(mode=MODE_COUNT, target_count=2, level_id="L1")
        page = PracticePage(settings, tr, config, module)
        page._apply(page.session.start())  # noqa: SLF001

        question = page.session.current_question
        assert question is not None
        wrong_hz = midi_to_hz(60 + (question.target_pc + 5) % 12)

        # 需要先越过宽容期，然后弹错
        for frame in range(240):
            events = page.session.feed(
                PitchEvent(t=frame * 0.01, hz=wrong_hz, confidence=0.95, is_onset=(frame == 0))
            )
            page._apply(events)  # noqa: SLF001

        assert page.session.current_question is question, "答错后应停留本题"
        assert "需要" in page.feedback.text()

    def test_skip_and_stop_buttons(self, settings, tr, app) -> None:  # noqa: ARG002
        module = registry.get_module("pitch_find")
        config = SessionConfig(mode=MODE_FREE)
        page = PracticePage(settings, tr, config, module)
        page._apply(page.session.start())  # noqa: SLF001
        page.skip_question()
        assert page.session.stats.skipped == 1

    def test_pause_toggle(self, settings, tr, app) -> None:  # noqa: ARG002
        module = registry.get_module("pitch_find")
        config = SessionConfig(mode=MODE_COUNT, target_count=2)
        page = PracticePage(settings, tr, config, module)
        page._apply(page.session.start())  # noqa: SLF001
        page.toggle_pause()
        assert page.session.is_paused
        assert page.pause_button.text() == tr("practice.resume")
        page.toggle_pause()
        assert not page.session.is_paused


class TestLearningIntegration:
    """M3：练习屏接入记忆曲线调度并把训练项落库。"""

    def test_practice_page_wires_scheduler_and_persists_items(self, tmp_path, tr, app) -> None:  # noqa: ARG002
        db = Database(tmp_path / "m3.db")
        db.initialize()
        conn = db.connect()
        profile_id = db.list_profiles(conn)[0]["id"]
        settings = Settings(db, conn, profile_id)

        module = registry.get_module("pitch_find")
        config = SessionConfig(mode=MODE_COUNT, target_count=3, level_id="L1")
        page = PracticePage(settings, tr, config, module, profile_id=profile_id)
        page._attach_backends()  # noqa: SLF001
        assert page.session.scheduler is not None, "练习屏应接入出题调度器"

        _drive(page, 3)

        rows = conn.execute(
            "SELECT * FROM items WHERE profile_id = ?", (profile_id,)
        ).fetchall()
        assert rows, "训练项应写入 items 表"
        assert all(row["due_at"] for row in rows), "答对后应排定下次到期时间"
        assert all(row["seen_count"] >= 1 for row in rows)

        sessions = conn.execute("SELECT * FROM sessions").fetchall()
        assert len(sessions) == 1
        assert sessions[0]["total"] == 3
        page.stop_audio()
        conn.close()

    def test_scheduler_marks_answered_items_not_new(self, tmp_path, tr, app) -> None:  # noqa: ARG002
        db = Database(tmp_path / "m3b.db")
        db.initialize()
        conn = db.connect()
        profile_id = db.list_profiles(conn)[0]["id"]
        settings = Settings(db, conn, profile_id)

        module = registry.get_module("pitch_find")
        config = SessionConfig(mode=MODE_COUNT, target_count=2, level_id="L1")
        page = PracticePage(settings, tr, config, module, profile_id=profile_id)
        page._attach_backends()  # noqa: SLF001

        scheduler = page.session.scheduler
        assert scheduler is not None
        assert all(state.is_new for state in scheduler.states.values())

        _drive(page, 2)
        answered = [s for s in scheduler.states.values() if not s.is_new]
        assert len(answered) == 2, "答过的题不应再算新题"
        page.stop_audio()
        conn.close()


class TestHomePageLearningStatus:
    """回归：首页的「复习计划」与「难度建议」曾经永远是"—"。

    原因是 HomePage 根本没有 profile_id 属性，_learning_status 抛 AttributeError，
    又被 `except Exception: return "—", ""` 静静吞掉 —— 功能等于没生效还看不出来。
    这类"静默失败"必须用断言钉住。
    """

    def _home(self, tmp_path, tr, app, *, with_due: bool):  # noqa: ARG002
        from datetime import datetime, timedelta, timezone

        from jitatrainer.core.theory.notes import NOTE_NAMES
        from jitatrainer.ui.pages.home import HomePage

        db = Database(tmp_path / "home.db")
        db.initialize()
        conn = db.connect()
        profile_id = db.list_profiles(conn)[0]["id"]
        if with_due:
            now = datetime.now(timezone.utc)
            for pc in (4, 9):
                conn.execute(
                    "INSERT INTO items (profile_id, module_id, item_key, pitch_class, level_id, "
                    "interval_index, due_at, seen_count) "
                    "VALUES (?, 'pitch_find', ?, ?, 'L1', 1, ?, 3)",
                    (profile_id, f"note={NOTE_NAMES[pc]}|level=L1", pc,
                     (now - timedelta(hours=1)).isoformat()),
                )
            conn.commit()
        page = HomePage(Settings(db, conn, profile_id), tr, "测试档案")
        page.refresh()
        return page, conn

    def _values(self, page) -> str:  # noqa: ANN001
        return " ".join(
            page.info_grid.itemAt(i).widget().text()
            for i in range(page.info_grid.count())
            if page.info_grid.itemAt(i).widget() is not None
        )

    def test_shows_due_count_instead_of_placeholder(self, tmp_path, tr, app) -> None:  # noqa: ARG002
        page, conn = self._home(tmp_path, tr, app, with_due=True)
        values = self._values(page)
        assert "2" in values, f"应显示到期复习数量：{values}"
        assert "—" not in values, f"不该落到异常分支的占位符：{values}"
        conn.close()

    def test_no_due_shows_clear_message(self, tmp_path, tr, app) -> None:  # noqa: ARG002
        page, conn = self._home(tmp_path, tr, app, with_due=False)
        values = self._values(page)
        assert "无待复习" in values, f"应说明今天没有待复习项：{values}"
        conn.close()

    def test_profile_id_attribute_exists(self, tmp_path, tr, app) -> None:  # noqa: ARG002
        page, conn = self._home(tmp_path, tr, app, with_due=False)
        assert page.profile_id is not None
        assert page.profile_id == page.settings.profile_id
        conn.close()


class TestStatsPage:
    """M4 统计页：渲染与数据绑定（图表是自绘控件，必须真的画一遍）。"""

    def _page(self, tmp_path, tr, app, *, with_data: bool):  # noqa: ARG002
        from datetime import datetime, timedelta, timezone

        from tests.test_analytics import add_session

        db = Database(tmp_path / "stats.db")
        db.initialize()
        conn = db.connect()
        profile_id = db.list_profiles(conn)[0]["id"]
        settings = Settings(db, conn, profile_id)

        if with_data:
            now = datetime.now(timezone.utc) - timedelta(minutes=30)
            add_session(
                db,
                profile_id,
                started=now,
                minutes=14.5,
                attempts=[
                    (4, "correct", 1200),
                    (4, "correct", 1500),
                    (7, "wrong", None),
                    (7, "correct", 2200),
                    (9, "wrong", None),
                    (11, "wrong", None),
                ],
            )
        from jitatrainer.ui.pages.stats import StatsPage

        page = StatsPage(settings, tr, profile_id=profile_id)
        page.resize(1100, 760)
        page.refresh()
        return page, conn

    def test_renders_with_data(self, tmp_path, tr, app) -> None:  # noqa: ARG002
        page, conn = self._page(tmp_path, tr, app, with_data=True)
        pixmap = QPixmap(QSize(1100, 760))
        page.render(pixmap)
        assert not pixmap.isNull()
        # KPI 里应出现累计题量 6
        texts = [
            page.kpi_grid.itemAt(i).layout().itemAt(1).widget().text()
            for i in range(page.kpi_grid.count())
            if page.kpi_grid.itemAt(i).layout() is not None
        ]
        assert "6" in texts
        conn.close()

    def test_renders_without_data(self, tmp_path, tr, app) -> None:  # noqa: ARG002
        page, conn = self._page(tmp_path, tr, app, with_data=False)
        pixmap = QPixmap(QSize(1100, 760))
        page.render(pixmap)
        assert not pixmap.isNull()
        assert page.weak_label.text() == tr("stats.no_weak")
        conn.close()

    def test_period_switch(self, tmp_path, tr, app) -> None:  # noqa: ARG002
        page, conn = self._page(tmp_path, tr, app, with_data=True)
        for index in range(page.period_combo.count()):
            page.period_combo.setCurrentIndex(index)
            assert page._days in (7, 30, 90)  # noqa: SLF001
            pixmap = QPixmap(QSize(600, 400))
            page.render(pixmap)
            assert not pixmap.isNull()
        conn.close()

    def test_heatmap_renders_cells(self, tmp_path, tr, app) -> None:  # noqa: ARG002
        page, conn = self._page(tmp_path, tr, app, with_data=True)
        assert page.heatmap._rows, "热力图应有音名行"  # noqa: SLF001
        assert page.heatmap._cols, "热力图应有把位列"  # noqa: SLF001
        values = [v for row in page.heatmap._values for v in row if v is not None]  # noqa: SLF001
        assert values, "应至少有一个训练项的错音数据"
        conn.close()

    def test_charts_render_line_and_bars(self, tmp_path, tr, app) -> None:  # noqa: ARG002
        page, conn = self._page(tmp_path, tr, app, with_data=True)
        assert page.accuracy_chart._points, "正确率曲线应有数据点"  # noqa: SLF001
        assert any(value > 0 for _label, value in page.minutes_chart._bars), (  # noqa: SLF001
            "练习时长柱状图应有数据"
        )
        conn.close()


class TestDialogs:
    def test_setup_dialog_roundtrip(self, tr, app) -> None:  # noqa: ARG002
        base = SessionConfig(mode=MODE_COUNT, target_count=20, level_id="L2")
        dialog = SessionSetupDialog(base, tr)
        config = dialog.config()
        assert config.level_id == "L2"
        assert config.mode == MODE_COUNT
        assert config.target_count == 20

    def test_setup_dialog_mode_switch_changes_targets(self, tr, app) -> None:  # noqa: ARG002
        dialog = SessionSetupDialog(SessionConfig(), tr)
        index = dialog.mode_combo.findData(MODE_DURATION)
        dialog.mode_combo.setCurrentIndex(index)
        assert dialog.target_combo.isEnabled()
        assert "分钟" in dialog.target_combo.currentText()

        index = dialog.mode_combo.findData(MODE_FREE)
        dialog.mode_combo.setCurrentIndex(index)
        assert not dialog.target_combo.isEnabled()
        assert dialog.config().mode == MODE_FREE

    def test_summary_dialog_shows_values(self, settings, tr, app) -> None:  # noqa: ARG002
        module = registry.get_module("pitch_find")
        config = SessionConfig(mode=MODE_COUNT, target_count=2, level_id="L3")
        page = PracticePage(settings, tr, config, module)
        _drive(page, 2)
        summary = page.session.build_summary()

        dialog = SessionSummaryDialog(summary, tr)
        assert dialog.windowTitle() == tr("stats.session_summary")
        assert not dialog.retry_requested
        dialog._on_again()  # noqa: SLF001
        assert dialog.retry_requested
