"""会话控制器：把出题、判定、统计与三种会话模式串起来。

刻意**不依赖 Qt**，并且时钟可注入，因此整个会话流程可以用合成事件完整单元测试
（练习屏只负责把 ``PitchEvent`` 喂进来、把 ``SessionEvent`` 画出来）。
"""

from __future__ import annotations

import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from ..core.audio.events import PitchEvent
from ..core.judge.base import (
    RESULT_CORRECT,
    RESULT_TIMEOUT,
    RESULT_WRONG,
    JudgeOutcome,
)
from ..core.judge.pitch_class_judge import JudgeConfig, PitchClassJudge
from ..core.theory.levels import DEFAULT_LEVEL_ID
from .base import SOURCE_NEW, SOURCE_REQUEUE, Question, QuestionContext

#: 会话模式（需求 FR-560）
MODE_DURATION = "duration"
MODE_COUNT = "count"
MODE_FREE = "free"
MODE_IDS = (MODE_DURATION, MODE_COUNT, MODE_FREE)

DURATION_PRESETS = (5, 10, 15, 30, 45)
COUNT_PRESETS = (10, 20, 30, 50, 100)
DEFAULT_DURATION_MINUTES = 15
DEFAULT_COUNT = 30

#: 难度系数（需求 FR-830）
LEVEL_DIFFICULTY = {"L1": 1.0, "L2": 1.2, "L3": 1.4, "L4": 1.6}

#: 得分公式的时间奖励分档（毫秒）
TIME_BONUS_TIERS = ((1500, 1.5), (3000, 1.2), (5000, 1.0))
TIME_BONUS_DEFAULT = 0.8
COMBO_STEP = 0.05
COMBO_CAP = 1.5

# 会话事件类型
EVENT_STARTED = "started"
EVENT_QUESTION = "question"
EVENT_CORRECT = "correct"
EVENT_WRONG = "wrong"
EVENT_TIMEOUT = "timeout"
EVENT_SKIPPED = "skipped"
EVENT_FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """一次会话的配置。"""

    module_id: str = "pitch_find"
    mode: str = MODE_COUNT
    target_minutes: int = DEFAULT_DURATION_MINUTES
    target_count: int = DEFAULT_COUNT
    level_id: str = DEFAULT_LEVEL_ID
    include_accidentals: bool = False
    scoring_enabled: bool = True
    show_note_name: bool = False

    def validate(self) -> None:
        if self.mode not in MODE_IDS:
            raise ValueError(f"未知会话模式：{self.mode}")
        if self.mode == MODE_DURATION and self.target_minutes <= 0:
            raise ValueError("固定时长模式的时长必须为正")
        if self.mode == MODE_COUNT and self.target_count <= 0:
            raise ValueError("固定数量模式的题量必须为正")

    @property
    def target_seconds(self) -> float:
        return self.target_minutes * 60.0

    def describe(self, language: str = "zh_CN") -> str:
        if self.mode == MODE_DURATION:
            return f"{self.target_minutes} 分钟" if language == "zh_CN" else f"{self.target_minutes} min"
        if self.mode == MODE_COUNT:
            return f"{self.target_count} 题" if language == "zh_CN" else f"{self.target_count} notes"
        return "自由练习" if language == "zh_CN" else "Free practice"


@dataclass
class SessionStats:
    """会话统计。"""

    asked: int = 0
    correct_first: int = 0
    correct_final: int = 0
    wrong_attempts: int = 0
    skipped: int = 0
    timeouts: int = 0
    combo: int = 0
    best_combo: int = 0
    score: int = 0
    #: 反应时间用"累加 + 计数"而不是列表：列表会随题量无限增长，
    #: 而需求 NFR-06 明确要求长时间练习内存不得持续增长（平均值用这两个数就能算）。
    rt_sum_ms: int = 0
    rt_count: int = 0
    wrong_by_item: Counter = field(default_factory=Counter)

    @property
    def answered(self) -> int:
        """已完成（有最终结果）的题数。"""
        return self.correct_final + self.timeouts + self.skipped

    @property
    def accuracy_first(self) -> float:
        return self.correct_first / self.asked if self.asked else 0.0

    @property
    def accuracy_final(self) -> float:
        return self.correct_final / self.asked if self.asked else 0.0

    @property
    def avg_rt_ms(self) -> int | None:
        if not self.rt_count:
            return None
        return int(self.rt_sum_ms / self.rt_count)

    def add_reaction_time(self, rt_ms: int) -> None:
        self.rt_sum_ms += int(rt_ms)
        self.rt_count += 1

    def weakest_items(self, limit: int = 3) -> list[tuple[str, int]]:
        return self.wrong_by_item.most_common(limit)


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """会话小结（需求 FR-820）。"""

    module_id: str
    mode: str
    level_id: str
    duration_s: float
    asked: int
    correct_first: int
    correct_final: int
    wrong_attempts: int
    skipped: int
    timeouts: int
    avg_rt_ms: int | None
    best_combo: int
    score: int
    accuracy_first: float
    accuracy_final: float
    weakest: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """会话向 UI 发出的事件。"""

    kind: str
    question: Question | None = None
    outcome: JudgeOutcome | None = None
    summary: SessionSummary | None = None


class PracticeSession:
    """一次练习会话。

    用法::

        session = PracticeSession(module, config, judge_factory, profile_id=1)
        for event in session.start():
            ...
        for pitch in pitch_events:          # 来自音频层
            for event in session.feed(pitch):
                ...
    """

    def __init__(
        self,
        module,
        config: SessionConfig,
        judge_factory: Callable[[Question], JudgeConfig],
        *,
        profile_id: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        scheduler=None,  # noqa: ANN001 - scheduling.QuestionScheduler，避免循环依赖
    ) -> None:
        config.validate()
        self.module = module
        self.config = config
        self.judge_factory = judge_factory
        self.profile_id = profile_id
        self.clock = clock
        #: 出题调度器（M3）。为 None 时退化为纯随机出题（M2 行为）。
        self.scheduler = scheduler

        self.stats = SessionStats()
        self._context = QuestionContext(
            profile_id=profile_id,
            level_id=config.level_id,
            include_accidentals=config.include_accidentals,
        )
        self._question: Question | None = None
        self._judge: PitchClassJudge | None = None
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._paused = False
        self._paused_total = 0.0
        self._pause_started: float | None = None
        self._first_attempt_used = False
        #: 当前题对应的调度决定（M3）
        self._decision = None
        #: 上一个判定到的音名。吉他的音能响好几秒（比宽容期还长），
        #: 因此新题开始时要告诉判定器"这个音是上一题残留的，别当答案"。
        self._ignore_pc: int | None = None
        #: 会话事件观察者（例如 ``data.repository.SessionRecorder``）。
        #: 观察者抛异常不得影响练习，因此统一包一层。
        self.observer: Callable[[SessionEvent], None] | None = None

    # ------------------------------------------------------------------ 事件分发
    def _emit(self, events: list[SessionEvent]) -> list[SessionEvent]:
        if self.observer is not None:
            for event in events:
                try:
                    self.observer(event)
                except Exception:  # noqa: BLE001 - 记录失败不得中断练习
                    pass
        return events

    # ------------------------------------------------------------------ 状态
    @property
    def current_question(self) -> Question | None:
        return self._question

    @property
    def is_finished(self) -> bool:
        return self._finished_at is not None

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def elapsed_s(self) -> float:
        if self._started_at is None:
            return 0.0
        if self._finished_at is not None:
            end = self._finished_at
        elif self._paused and self._pause_started is not None:
            # 暂停期间时间必须冻住，否则固定时长模式会在暂停时把时间耗光
            end = self._pause_started
        else:
            end = self.clock()
        return max(0.0, end - self._started_at - self._paused_total)

    @property
    def remaining_s(self) -> float | None:
        if self.config.mode != MODE_DURATION:
            return None
        return max(0.0, self.config.target_seconds - self.elapsed_s)

    @property
    def answered(self) -> int:
        """已完成（有最终结果）的题数。"""
        return self.stats.asked

    @property
    def question_index(self) -> int:
        """当前是第几题（从 1 开始）。"""
        return self.stats.asked + (1 if self._question is not None else 0)

    # ------------------------------------------------------------------ 控制
    def start(self) -> list[SessionEvent]:
        if self._started_at is not None:
            return []
        self._started_at = self.clock()
        events = [SessionEvent(kind=EVENT_STARTED)]
        events.extend(self._next_question())
        return self._emit(events)

    def pause(self) -> None:
        if self._paused or self.is_finished:
            return
        self._paused = True
        self._pause_started = self.clock()

    def resume(self) -> None:
        if not self._paused:
            return
        self._paused = False
        if self._pause_started is not None:
            self._paused_total += self.clock() - self._pause_started
            self._pause_started = None

    def toggle_pause(self) -> bool:
        if self._paused:
            self.resume()
        else:
            self.pause()
        return self._paused

    def skip(self) -> list[SessionEvent]:
        """跳过本题：记为"不会"，计入薄弱项（需求 FR-560）。"""
        if self._question is None or self.is_finished:
            return []
        self.stats.asked += 1
        self.stats.skipped += 1
        self.stats.combo = 0
        self.stats.wrong_by_item[self._question.item_key] += 1
        self._ignore_pc = None
        self._record_schedule(correct=False, skipped=True)
        events = [SessionEvent(kind=EVENT_SKIPPED, question=self._question)]
        events.extend(self._maybe_finish_or_next())
        return self._emit(events)

    def tick(self) -> list[SessionEvent]:
        """按时间推进（固定时长模式到点结束）。由 UI 定时器调用。"""
        if self.is_finished or self._started_at is None or self._paused:
            return []
        if self.config.mode == MODE_DURATION and self.elapsed_s >= self.config.target_seconds:
            return self.finish(reason="duration")
        return []

    def finish(self, *, reason: str = "user") -> list[SessionEvent]:
        if self.is_finished:
            return []
        self._finished_at = self.clock()
        summary = self.build_summary()
        self._question = None
        self._judge = None
        return self._emit([SessionEvent(kind=EVENT_FINISHED, summary=summary)])

    def build_summary(self) -> SessionSummary:
        return SessionSummary(
            module_id=self.config.module_id,
            mode=self.config.mode,
            level_id=self.config.level_id,
            duration_s=round(self.elapsed_s, 1),
            asked=self.stats.asked,
            correct_first=self.stats.correct_first,
            correct_final=self.stats.correct_final,
            wrong_attempts=self.stats.wrong_attempts,
            skipped=self.stats.skipped,
            timeouts=self.stats.timeouts,
            avg_rt_ms=self.stats.avg_rt_ms,
            best_combo=self.stats.best_combo,
            score=self.stats.score,
            accuracy_first=round(self.stats.accuracy_first, 4),
            accuracy_final=round(self.stats.accuracy_final, 4),
            weakest=tuple(self.stats.weakest_items()),
        )

    # ------------------------------------------------------------------ 事件
    def feed(self, pitch: PitchEvent) -> list[SessionEvent]:
        """喂入一帧音高事件，返回由此产生的会话事件。"""
        if self.is_finished or self._paused or self._question is None:
            return []

        if self._judge is None:
            config = self.judge_factory(self._question)
            if self._ignore_pc is not None:
                config = replace(config, ignore_pitch_class=self._ignore_pc)
            self._judge = PitchClassJudge(config, started_at=pitch.t)

        outcome = self._judge.feed(pitch)
        if outcome is None:
            return []

        if outcome.result == RESULT_CORRECT:
            return self._emit(self._on_correct(outcome))
        if outcome.result == RESULT_WRONG:
            return self._emit(self._on_wrong(outcome))
        if outcome.result == RESULT_TIMEOUT:
            return self._emit(self._on_timeout(outcome))
        return []

    # ------------------------------------------------------------------ 内部
    def _next_question(self) -> list[SessionEvent]:
        if self.scheduler is not None:
            # M3：由调度器决定出什么（到期复习 / 薄弱强化 / 新题 / 错题回炉）
            decision = self.scheduler.next_decision()
            self._decision = decision
            self._context.requested_pc = decision.pitch_class
            self._context.source = decision.source
        else:
            self._context.requested_pc = None
            self._context.source = SOURCE_NEW

        question = self.module.generate(self._context)
        self._question = question
        self._judge = None
        self._first_attempt_used = False
        return [SessionEvent(kind=EVENT_QUESTION, question=question)]

    def _record_schedule(
        self,
        *,
        correct: bool,
        skipped: bool = False,
        timed_out: bool = False,
        rt_ms: int | None = None,
    ) -> None:
        """把结果交给调度器（更新记忆曲线与回炉队列）。"""
        if self.scheduler is None or self._decision is None or self._question is None:
            return
        self.scheduler.on_result(
            self._decision,
            correct=correct,
            first_attempt=not self._first_attempt_used,
            skipped=skipped,
            timed_out=timed_out,
            hinted=self.config.show_note_name,
            rt_ms=rt_ms,
        )

    def _on_correct(self, outcome: JudgeOutcome) -> list[SessionEvent]:
        assert self._question is not None
        self.stats.asked += 1
        self.stats.correct_final += 1
        if not self._first_attempt_used:
            self.stats.correct_first += 1
            if outcome.elapsed_ms:
                self.stats.add_reaction_time(int(outcome.elapsed_ms))
        self.stats.combo += 1
        self.stats.best_combo = max(self.stats.best_combo, self.stats.combo)
        if self.config.scoring_enabled:
            self.stats.score += self.score_for(outcome, self.stats.combo - 1, self._question)
        self._ignore_pc = outcome.detected_pc
        self._record_schedule(
            correct=True, rt_ms=int(outcome.elapsed_ms) if outcome.elapsed_ms else None
        )

        events = [SessionEvent(kind=EVENT_CORRECT, question=self._question, outcome=outcome)]
        events.extend(self._maybe_finish_or_next())
        return events

    def _on_wrong(self, outcome: JudgeOutcome) -> list[SessionEvent]:
        assert self._question is not None
        self.stats.wrong_attempts += 1
        self.stats.combo = 0
        self._first_attempt_used = True
        self.stats.wrong_by_item[self._question.item_key] += 1
        self._record_schedule(correct=False)
        return [SessionEvent(kind=EVENT_WRONG, question=self._question, outcome=outcome)]

    def _on_timeout(self, outcome: JudgeOutcome) -> list[SessionEvent]:
        assert self._question is not None
        self.stats.asked += 1
        self.stats.timeouts += 1
        self.stats.combo = 0
        self.stats.wrong_by_item[self._question.item_key] += 1
        self._ignore_pc = None
        self._record_schedule(correct=False, timed_out=True)
        events = [SessionEvent(kind=EVENT_TIMEOUT, question=self._question, outcome=outcome)]
        events.extend(self._maybe_finish_or_next())
        return events

    def _maybe_finish_or_next(self) -> list[SessionEvent]:
        if self._reached_target():
            return self.finish(reason=self.config.mode)
        return self._next_question()

    def _reached_target(self) -> bool:
        if self.config.mode == MODE_COUNT:
            return self.stats.asked >= self.config.target_count
        if self.config.mode == MODE_DURATION:
            return self.elapsed_s >= self.config.target_seconds
        return False

    def score_for(self, outcome: JudgeOutcome, combo_before: int, question: Question) -> int:
        """需求 FR-830 的得分公式。"""
        difficulty = LEVEL_DIFFICULTY.get(question.level_id, 1.0)
        time_bonus = TIME_BONUS_DEFAULT
        for threshold, bonus in TIME_BONUS_TIERS:
            if outcome.elapsed_ms <= threshold:
                time_bonus = bonus
                break
        combo_bonus = min(COMBO_CAP, 1.0 + COMBO_STEP * combo_before)
        value = 100.0 * difficulty * time_bonus * combo_bonus
        if question.source == SOURCE_REQUEUE:
            value *= 0.5
        return int(round(value))


__all__ = [
    "MODE_IDS",
    "MODE_COUNT",
    "MODE_DURATION",
    "MODE_FREE",
    "DURATION_PRESETS",
    "COUNT_PRESETS",
    "LEVEL_DIFFICULTY",
    "PracticeSession",
    "SessionConfig",
    "SessionEvent",
    "SessionStats",
    "SessionSummary",
    "EVENT_QUESTION",
    "EVENT_CORRECT",
    "EVENT_WRONG",
    "EVENT_TIMEOUT",
    "EVENT_SKIPPED",
    "EVENT_STARTED",
    "EVENT_FINISHED",
]
