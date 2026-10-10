"""练习屏：模块一「看谱找音」的主界面。

结构（需求 FR-570）：
    顶栏（进度/正确率/连击/剩余/得分）
    主区：六线谱（≥60% 高度）
    底栏：实时音高表 + 反馈文字 + 快捷键提示

练习屏只负责"把 PitchEvent 喂给会话、把 SessionEvent 画出来"，
所有判定与统计逻辑都在 ``practice.session`` 里（可单元测试）。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.theory.notes import pitch_class_name
from ...data.settings import Settings
from ...practice.base import SOURCE_NEW, SOURCE_REQUEUE, SOURCE_REVIEW, SOURCE_WEAK, Question
from ...practice.session import (
    EVENT_CORRECT,
    EVENT_FINISHED,
    EVENT_QUESTION,
    EVENT_SKIPPED,
    EVENT_TIMEOUT,
    EVENT_WRONG,
    PracticeSession,
    SessionConfig,
    SessionEvent,
    SessionSummary,
)
from ..audio_bridge import build_session
from ..widgets.countdown import CountdownBar
from ..widgets.notation_tab import NotationTab
from ..widgets.pitch_meter import PitchMeter

POLL_MS = 30
CORRECT_FLASH_S = 0.5


class PracticePage(QWidget):
    """练习屏。"""

    finished = Signal(object)  # SessionSummary
    exit_requested = Signal()

    def __init__(
        self,
        settings: Settings,
        tr,
        config: SessionConfig,
        module,
        *,
        profile_id: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.tr = tr
        self.config = config
        self.module = module
        self.profile_id = profile_id

        self.session = PracticeSession(
            module,
            config,
            lambda question: module.make_judge_config(question, settings),
            profile_id=profile_id,
        )
        self.audio = None
        self.recorder = None
        self.item_store = None
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._correct_flash_until = 0.0
        self._last_question: Question | None = None
        #: 连续"无法用本琴解释"的检测次数。用于发现麦克风/环境问题并提示用户，
        #: 而不是默默按某个猜测处理（需求：声音不符预期时提示用户）。
        self._unexplained_run = 0
        self._env_warned = False
        #: PitchEvent.t 与 time.monotonic() 是两个时间基。倒计时需要换算：
        #: 记下最近一次事件的时间戳与当时的挂钟，用它外推"现在是事件时间基上的几秒"。
        self._last_event_t: float | None = None
        self._last_event_wall = 0.0

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._build_ui()

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(22)
        self.progress_label = self._top_item(top, self.tr("practice.progress_label"))
        self.accuracy_label = self._top_item(top, self.tr("practice.accuracy"))
        self.combo_label = self._top_item(top, self.tr("practice.combo"))
        self.remaining_label = self._top_item(top, self.tr("practice.remaining_label"))
        self.score_label = self._top_item(top, self.tr("practice.score"))
        if not self.config.scoring_enabled:
            self.score_label.hide()
        top.addStretch(1)

        self.pause_button = QPushButton(self.tr("practice.pause"))
        self.pause_button.clicked.connect(self.toggle_pause)
        # 手动推进模式下这个按钮就是主要操作，改名更直观
        self.skip_button = QPushButton(
            self.tr("practice.next_note" if not self.config.countdown_enabled else "practice.skip")
        )
        self.skip_button.clicked.connect(
            self.advance_question if not self.config.countdown_enabled else self.skip_question
        )
        self.stop_button = QPushButton(self.tr("practice.stop"))
        self.stop_button.clicked.connect(self.request_exit)
        for button in (self.pause_button, self.skip_button, self.stop_button):
            top.addWidget(button)
        layout.addLayout(top)

        self.tab = NotationTab()
        layout.addWidget(self.tab, 1)

        self.meter = PitchMeter()
        self.meter.setMinimumHeight(130)
        self.meter.setMaximumHeight(170)
        layout.addWidget(self.meter)

        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        self.feedback = QLabel(self.tr("practice.waiting"))
        self.feedback.setObjectName("sectionTitle")
        self.feedback.setWordWrap(True)
        bottom.addWidget(self.feedback, 1)
        # 倒计时条：自绘控件，固定高度，不会与反馈文字挤在一起
        self.countdown = CountdownBar()
        bottom.addWidget(self.countdown)
        self.hint = QLabel(
            self.tr(
                "practice.shortcuts" if self.config.countdown_enabled else "practice.shortcuts_manual"
            )
        )
        self.hint.setObjectName("faint")
        bottom.addWidget(self.hint)
        layout.addLayout(bottom)

    def _top_item(self, layout: QHBoxLayout, caption: str) -> QLabel:
        box = QVBoxLayout()
        caption_label = QLabel(caption)
        caption_label.setObjectName("faint")
        value_label = QLabel("—")
        value_label.setStyleSheet("font-size: 17px; font-weight: 600;")
        box.addWidget(caption_label)
        box.addWidget(value_label)
        layout.addLayout(box)
        return value_label

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self._attach_backends()
        try:
            self.audio = build_session(self.settings)
            self.audio.start()
        except Exception as exc:  # noqa: BLE001 - 设备问题要提示而不是崩溃
            self.audio = None
            self.feedback.setText(self.tr("error.audio_unavailable", reason=str(exc)))
            self.feedback.setStyleSheet("color: #e05c5c;")
            return

        self._apply(self.session.start())
        self._timer.start()
        self.setFocus()

    def _attach_backends(self) -> None:
        """接上持久化（答题记录）与记忆曲线调度。任何失败都不影响练习。"""
        if self.profile_id is None:
            return
        self._attach_recorder()
        self._attach_scheduler()

    def _attach_recorder(self) -> None:
        """把会话过程写入数据库（失败不影响练习）。"""
        if self.recorder is not None:
            return
        try:
            from ...data.repository import PracticeRepository, SessionRecorder

            repository = PracticeRepository(self.settings.db, self.profile_id)
            self.recorder = SessionRecorder(
                repository,
                module_id=self.config.module_id,
                mode=self.config.mode,
                target_value=(
                    self.config.target_count
                    if self.config.mode == "count"
                    else self.config.target_minutes
                    if self.config.mode == "duration"
                    else None
                ),
            )
            self.session.observer = self.recorder
        except Exception:  # noqa: BLE001
            self.recorder = None

    def _attach_scheduler(self) -> None:
        """接上 M3 记忆曲线调度：到期复习 / 薄弱强化 / 新题 / 错题回炉。"""
        if self.session.scheduler is not None:
            return
        try:
            from ...core.theory.fretboard import level_pitch_classes
            from ...data.repository import DbItemStore
            from ...practice.base import default_item_key
            from ...scheduling.scheduler import QuestionScheduler

            store = DbItemStore(self.settings.db, self.profile_id, self.config.module_id)
            pitch_classes = level_pitch_classes(self.config.level_id, self.config.include_accidentals)
            scheduler = QuestionScheduler(
                level_id=self.config.level_id,
                pitch_classes=pitch_classes,
                item_key_of=lambda pc: default_item_key(pc, self.config.level_id),
                states=store.load(self.config.level_id),
                on_state_change=store.save,
            )
            self.item_store = store
            self.session.scheduler = scheduler
        except Exception:  # noqa: BLE001
            self.item_store = None

    def stop_audio(self) -> None:
        self._timer.stop()
        if self.audio is not None:
            self.audio.stop()
            self.audio = None
        if self.recorder is not None:
            self.recorder.close()
            self.recorder = None
        if self.item_store is not None:
            self.item_store.close()
            self.item_store = None

    def leave(self) -> None:
        self.stop_audio()

    def toggle_pause(self) -> None:
        paused = self.session.toggle_pause()
        if paused:
            # 需求 FR-560：暂停时麦克风检测也暂停
            if self.audio is not None:
                self.audio.stop()
            self.pause_button.setText(self.tr("practice.resume"))
            self.feedback.setText(self.tr("practice.paused"))
        else:
            if self.audio is not None:
                self.audio.start()
            self.pause_button.setText(self.tr("practice.pause"))
            self.feedback.setText(self.tr("practice.waiting"))
            self.setFocus()

    def skip_question(self) -> None:
        if self.session.is_finished:
            return
        self._apply(self.session.skip())

    def advance_question(self) -> None:
        """手动推进到下一个音（手动超时模式的主要操作）。"""
        if self.session.is_finished:
            return
        self._apply(self.session.advance())

    def request_exit(self) -> None:
        if not self.session.is_finished:
            self._apply(self.session.finish(reason="user"))
        self.exit_requested.emit()

    # ------------------------------------------------------------------ 主循环
    def _poll(self) -> None:
        if self.session.is_finished:
            return

        if self.audio is not None:
            for pitch in self.audio.poll():
                self._last_event_t = pitch.t
                self._last_event_wall = time.monotonic()
                self._apply(self.session.feed(pitch))

        self._apply(self.session.tick())
        self._refresh_top()
        self._refresh_countdown()

    # ------------------------------------------------------------------ 倒计时
    def _now_in_event_clock(self) -> float | None:
        """把挂钟时间换算到 ``PitchEvent.t`` 的时间基上。"""
        if self._last_event_t is None:
            return None
        return self._last_event_t + (time.monotonic() - self._last_event_wall)

    def _refresh_countdown(self) -> None:
        """更新倒计时条。

        自动模式显示剩余秒数；手动推进模式不显示倒计时，改为提示按键，
        因为此时程序不会自己换题。
        """
        if self.session.is_finished:
            self.countdown.clear()
            return
        if not self.config.countdown_enabled:
            self.countdown.show_manual(self.tr("practice.manual_hint"))
            return

        now = self._now_in_event_clock()

        # 宽容期优先显示："听到了，但暂时不计入判定"
        grace = self.session.grace_remaining_ms(now) if now is not None else None
        if grace is not None and self.config.grace_ms > 0:
            self.countdown.show_grace(grace, self.config.grace_ms, self.tr("practice.in_grace"))
            return

        remaining = self.session.remaining_ms(now) if now is not None else None
        if remaining is None:
            # 还没收到音频（例如刚开始或设备异常）：显示满格，避免闪动
            remaining = float(self.config.timeout_ms)
        # 倒计时从宽容期结束后开始算，可能略大于超时秒数，作为总量取较大者
        total = max(float(self.config.timeout_ms), remaining)
        self.countdown.show_countdown(remaining, total)

    def _apply(self, events: list[SessionEvent]) -> None:
        for event in events:
            if event.kind == EVENT_QUESTION:
                self._on_question(event.question)
            elif event.kind == EVENT_CORRECT:
                self._on_correct(event)
            elif event.kind == EVENT_WRONG:
                self._on_wrong(event)
            elif event.kind == EVENT_TIMEOUT:
                self._on_timeout()
            elif event.kind == EVENT_SKIPPED:
                self._on_skipped(event)
            elif event.kind == EVENT_FINISHED:
                self._on_finished(event.summary)
        self._refresh_top()

    def _on_question(self, question: Question | None) -> None:
        if question is None:
            return
        self._last_question = question
        spec = self.module.render_spec(question, show_note_name=self.config.show_note_name)
        self.tab.set_question(
            spec.position,
            note_name=spec.note_name,
            show_note_name=spec.show_note_name,
            source_label=self._source_label(question.source),
        )
        self.tab.set_highlight(False)
        self.meter.set_target(question.position.midi, spec.label_zh, self.settings.get_float("tolerance_cents", 25.0))

        if time.monotonic() < self._correct_flash_until:
            return  # 保留"正确！"提示一小会儿
        self.feedback.setStyleSheet("")
        self.feedback.setText(self.tr("practice.waiting"))

    def _on_correct(self, event: SessionEvent) -> None:
        self.tab.set_state("correct")
        self.feedback.setStyleSheet("color: #3ecf8e;")
        self.feedback.setText(
            self.tr("practice.correct")
            + (f"　{event.outcome.elapsed_ms:.0f} ms" if event.outcome and event.outcome.elapsed_ms else "")
        )
        self._correct_flash_until = time.monotonic() + CORRECT_FLASH_S

    def _on_wrong(self, event: SessionEvent) -> None:
        question = event.question
        detected = event.outcome.detected_pc if event.outcome else None
        played = pitch_class_name(detected) if detected is not None else "—"
        target = pitch_class_name(question.target_pc) if question else "—"
        self.tab.set_state("wrong")
        self.tab.set_highlight(True)  # 高亮标准位置帮用户找到答案
        self.feedback.setStyleSheet("color: #e05c5c;")
        self.feedback.setText(self.tr("practice.wrong", played=played, target=target))
        self._check_environment(event)

    def _check_environment(self, event: SessionEvent) -> None:
        """检测到的音高若无法用本琴解释，连续几次就提示用户检查设备。

        答错本身很正常（练习就是要弹错）；这里针对的是"这个音**不属于这把琴
        能弹出的音**"——那通常意味着弹了别的弦/别的乐器、麦克风摆位或增益有问题。
        这正是"发现声音不符合预期时提示用户处理"的实时路径。
        """
        hz = event.outcome.detected_hz if event.outcome else None
        profile = getattr(self.audio, "profile", None) if self.audio is not None else None
        if profile is None or not hz or hz <= 0:
            return
        try:
            from ...core.instrument import resolve_hz

            resolution = resolve_hz(hz, profile)
        except Exception:  # noqa: BLE001 - 提示功能不该影响练习
            return

        if resolution.status == "unexplained":
            self._unexplained_run += 1
        else:
            self._unexplained_run = 0

        if self._unexplained_run >= 3 and not self._env_warned:
            self._env_warned = True
            self.feedback.setStyleSheet("color: #e8b339;")
            self.feedback.setText(self.tr("practice.env_warning"))

    def _on_timeout(self) -> None:
        self.tab.set_highlight(True)
        self.feedback.setStyleSheet("color: #e8b339;")
        self.feedback.setText(self.tr("practice.timeout"))

    def _on_skipped(self, event: SessionEvent | None = None) -> None:
        self.feedback.setStyleSheet("color: #e8b339;")
        reason = event.reason if event is not None else ""
        key = "practice.advanced" if reason == "manual" else "practice.skipped"
        self.feedback.setText(self.tr(key))

    def _on_finished(self, summary: SessionSummary | None) -> None:
        self.stop_audio()
        self._backup_after_session()
        if summary is not None:
            self.finished.emit(summary)

    def _backup_after_session(self) -> None:
        """需求 FR-1000：每次会话结束后做一次轻量备份。失败不影响流程。"""
        try:
            from ...data.db import backup_database

            backup_database(reason="session")
        except Exception:  # noqa: BLE001
            pass

    def _refresh_top(self) -> None:
        stats = self.session.stats
        total = self.session.stats.asked
        if self.config.mode == "count":
            self.progress_label.setText(f"{min(total + 1, self.config.target_count)}/{self.config.target_count}")
        else:
            self.progress_label.setText(str(total + (0 if self.session.is_finished else 1)))

        self.accuracy_label.setText(f"{stats.accuracy_first * 100:.0f}%")
        self.combo_label.setText(str(stats.combo))
        remaining = self.session.remaining_s
        if remaining is None:
            self.remaining_label.setText("—")
        else:
            minutes, seconds = divmod(int(remaining), 60)
            self.remaining_label.setText(f"{minutes:02d}:{seconds:02d}")
        self.score_label.setText(str(stats.score))

    def _source_label(self, source: str) -> str:
        return {
            SOURCE_REVIEW: self.tr("practice.source.review"),
            SOURCE_WEAK: self.tr("practice.source.weak"),
            SOURCE_REQUEUE: self.tr("practice.source.requeue"),
            SOURCE_NEW: "",
        }.get(source, "")

    # ------------------------------------------------------------------ 键盘
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Space:
            self.toggle_pause()
        elif key == Qt.Key.Key_S:
            self.skip_question()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_N):
            # 手动推进模式的主要操作；自动模式下等价于跳过
            self.advance_question()
        elif key == Qt.Key.Key_Escape:
            self.request_exit()
        else:
            super().keyPressEvent(event)


__all__ = ["PracticePage"]
