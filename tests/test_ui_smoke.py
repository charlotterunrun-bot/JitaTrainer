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
