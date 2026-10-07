"""出题调度（需求 FR-710 / FR-720）。

每 10 题为一个配额窗口：

    ① 到期复习 ≤ 4 题（逾期越久越优先）
    ② 薄弱项强化 ≤ 3 题（加权随机）
    ③ 新题 ≥ 3 题

错题回炉：本次答错/跳过/超时的题，隔 5~8 题重现；连续 2 次答对移出；
回炉题不占配额，但总量不超过会话题量的 30%。

调度器**不依赖数据库、不依赖 Qt**，因此可以用固定随机种子完整测试。
"""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..practice.base import SOURCE_NEW, SOURCE_REQUEUE, SOURCE_REVIEW, SOURCE_WEAK
from .srs import (
    EVENT_CORRECT_FIRST,
    EVENT_CORRECT_RETRY,
    EVENT_HINTED,
    EVENT_SKIP,
    EVENT_WRONG,
    ItemState,
    review,
)
from .weights import DEFAULT_TARGET_RT_MS, is_weak, weak_weight


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    """调度参数（默认值即需求文档中的取值）。"""

    window_size: int = 10
    due_quota: int = 4
    weak_quota: int = 3
    new_min: int = 3
    requeue_gap_min: int = 5
    requeue_gap_max: int = 8
    requeue_max_ratio: float = 0.30
    requeue_pass_count: int = 2
    target_rt_ms: int = DEFAULT_TARGET_RT_MS


@dataclass(frozen=True, slots=True)
class Decision:
    """一次出题决定。"""

    item_key: str
    pitch_class: int
    source: str


@dataclass
class _RequeueEntry:
    item_key: str
    pitch_class: int
    due_at_index: int
    consecutive_correct: int = 0
    #: 是否"已排期等待重现"。呈现后置为 False，但仍保留跟踪，
    #: 以便实现"连续 2 次答对才移出回炉队列"。
    waiting: bool = True


@dataclass
class SchedulerStats:
    asked: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    requeued: int = 0

    def count(self, source: str) -> int:
        return self.by_source.get(source, 0)

    def add(self, source: str) -> None:
        self.by_source[source] = self.by_source.get(source, 0) + 1


class QuestionScheduler:
    """决定下一题出什么（来源 + 音名）。"""

    def __init__(
        self,
        *,
        level_id: str,
        pitch_classes: Iterable[int],
        item_key_of: Callable[[int], str],
        states: dict[str, ItemState] | None = None,
        config: ScheduleConfig | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], datetime] = _utcnow,
        on_state_change: Callable[[ItemState], None] | None = None,
    ) -> None:
        self.level_id = level_id
        self.config = config or ScheduleConfig()
        self.rng = rng or random.Random()
        self.clock = clock
        #: 训练项状态变化时的回调（用于落库）
        self.on_state_change = on_state_change

        self.states: dict[str, ItemState] = dict(states or {})
        for pitch_class in pitch_classes:
            key = item_key_of(pitch_class)
            if key not in self.states:
                self.states[key] = ItemState(item_key=key, pitch_class=pitch_class, level_id=level_id)

        self.stats = SchedulerStats()
        self._window_counts: dict[str, int] = {}
        self._requeue: list[_RequeueEntry] = []
        #: 最近出过的题，避免连续重复（一个音连出几次会让人烦躁且无训练价值）
        self._recent: deque[str] = deque(maxlen=2)

    # ------------------------------------------------------------------ 查询
    @property
    def asked(self) -> int:
        return self.stats.asked

    def state_of(self, item_key: str) -> ItemState | None:
        return self.states.get(item_key)

    def due_items(self, now: datetime | None = None) -> list[ItemState]:
        moment = now or self.clock()
        return [s for s in self.states.values() if not s.is_new and s.is_due(moment)]

    def weak_items(self, now: datetime | None = None) -> list[ItemState]:
        moment = now or self.clock()
        return [s for s in self.states.values() if not s.is_new and not s.is_due(moment) and is_weak(s)]

    def new_items(self) -> list[ItemState]:
        return [s for s in self.states.values() if s.is_new]

    # ------------------------------------------------------------------ 出题
    def next_decision(self) -> Decision:
        """按配额与优先级给出下一题。"""
        index = self.stats.asked
        now = self.clock()

        requeue = self._take_requeue(index)
        if requeue is not None:
            self.stats.asked += 1
            self._bump(SOURCE_REQUEUE)
            self.stats.requeued += 1
            self._recent.append(requeue.item_key)
            return Decision(requeue.item_key, requeue.pitch_class, SOURCE_REQUEUE)

        choice = self._pick_by_quota(now)
        self.stats.asked += 1
        self._bump(choice.source)
        self._recent.append(choice.item_key)
        return choice

    # ------------------------------------------------------------------ 候选筛选
    @property
    def pending_requeue_keys(self) -> set[str]:
        """已排期等待回炉的题：在回炉重现之前不走普通通道。"""
        return {entry.item_key for entry in self._requeue if entry.waiting}

    def _filter(self, candidates: list[ItemState]) -> list[ItemState]:
        """先排除"等待回炉"的题，再尽量排除"刚刚出过"的题。

        - **回炉锁是绝对的**：已排期回炉的题在重现前绝不走普通通道
          （否则它会先被"到期/薄弱"通道抽走，回炉机制就形同虚设）。
        - "防连出"可以回退：宁可连着出，也不能没题可出。
        """
        pending = self.pending_requeue_keys
        unlocked = [s for s in candidates if s.item_key not in pending]
        fresh = [s for s in unlocked if s.item_key not in self._recent]
        return fresh or unlocked

    def _pick_by_quota(self, now: datetime) -> Decision:
        counts = self._window_counts
        due = self._filter(
            sorted(self.due_items(now), key=lambda s: s.overdue_days(now), reverse=True)
        )
        weak = self._filter(self.weak_items(now))
        new = self._filter(self.new_items())

        # ① 到期复习
        if due and counts.get(SOURCE_REVIEW, 0) < self.config.due_quota:
            return self._decision(due[0], SOURCE_REVIEW)
        # ② 薄弱强化
        if weak and counts.get(SOURCE_WEAK, 0) < self.config.weak_quota:
            return self._decision(self._weighted_choice(weak, now), SOURCE_WEAK)
        # ③ 新题
        if new:
            return self._decision(self.rng.choice(new), SOURCE_NEW)

        # 配额用尽或没有新题：按同样优先级回退（不硬性限制）
        if due:
            return self._decision(due[0], SOURCE_REVIEW)
        if weak:
            return self._decision(self._weighted_choice(weak, now), SOURCE_WEAK)

        # 全部训练项都已掌握且未到期：出一个最久没练的
        pending = self.pending_requeue_keys
        available = [s for s in self.states.values() if s.item_key not in pending]
        if not available:
            # 极端情况：所有题都在等回炉（例如连续全部答错）。此时"必须有题可出"
            # 优先于回炉锁——否则会死锁。
            available = list(self.states.values())
        ordered = sorted(
            available,
            key=lambda s: s.last_seen_at or datetime.min.replace(tzinfo=timezone.utc),
        )
        fresh = [s for s in ordered if s.item_key not in self._recent]
        return self._decision((fresh or ordered)[0], SOURCE_WEAK)

    def _decision(self, state: ItemState, source: str) -> Decision:
        return Decision(state.item_key, state.pitch_class, source)

    def _weighted_choice(self, candidates: list[ItemState], now: datetime) -> ItemState:
        weights = [max(1e-6, weak_weight(s, now=now, target_rt_ms=self.config.target_rt_ms)) for s in candidates]
        return self.rng.choices(candidates, weights=weights, k=1)[0]

    def _bump(self, source: str) -> None:
        self.stats.add(source)
        window = self._window_counts
        window[source] = window.get(source, 0) + 1
        if self.stats.asked % self.config.window_size == 0:
            self._window_counts = {}

    # ------------------------------------------------------------------ 回炉
    def _take_requeue(self, index: int) -> _RequeueEntry | None:
        if not self._requeue:
            return None

        ready = [entry for entry in self._requeue if entry.waiting and entry.due_at_index <= index]
        if not ready:
            return None

        # 总量限制：回炉题不超过已出题量的 30%（超了就顺延两题）
        if index > 0 and self.stats.requeued / max(1, index) > self.config.requeue_max_ratio:
            for entry in ready:
                entry.due_at_index = index + 2
            return None

        entry = min(ready, key=lambda e: e.due_at_index)
        entry.waiting = False
        return entry

    def _schedule_requeue(self, decision: Decision, index: int) -> None:
        gap = self.rng.randint(self.config.requeue_gap_min, self.config.requeue_gap_max)
        for entry in self._requeue:
            if entry.item_key == decision.item_key:
                entry.due_at_index = index + gap
                entry.consecutive_correct = 0
                entry.waiting = True
                return
        self._requeue.append(
            _RequeueEntry(
                item_key=decision.item_key,
                pitch_class=decision.pitch_class,
                due_at_index=index + gap,
            )
        )

    def _resolve_requeue(self, decision: Decision, correct: bool) -> None:
        """更新回炉跟踪：连续两次答对才彻底移出。"""
        for entry in list(self._requeue):
            if entry.item_key != decision.item_key:
                continue
            if not correct:
                entry.consecutive_correct = 0
                return
            entry.consecutive_correct += 1
            if entry.consecutive_correct >= self.config.requeue_pass_count:
                self._requeue.remove(entry)
            return

    @property
    def requeue_size(self) -> int:
        return len(self._requeue)

    # ------------------------------------------------------------------ 结果
    def on_result(
        self,
        decision: Decision,
        *,
        correct: bool,
        first_attempt: bool = True,
        skipped: bool = False,
        timed_out: bool = False,
        hinted: bool = False,
        rt_ms: int | None = None,
    ) -> ItemState:
        """记录一次答题结果并更新记忆曲线。"""
        state = self.states.get(decision.item_key)
        if state is None:
            state = ItemState(
                item_key=decision.item_key,
                pitch_class=decision.pitch_class,
                level_id=self.level_id,
            )

        if hinted:
            event = EVENT_HINTED
        elif skipped or timed_out:
            event = EVENT_SKIP
        elif correct and first_attempt:
            event = EVENT_CORRECT_FIRST
        elif correct:
            event = EVENT_CORRECT_RETRY
        else:
            event = EVENT_WRONG

        updated = review(state, event, now=self.clock(), rt_ms=rt_ms)
        self.states[decision.item_key] = updated

        failed = event in (EVENT_WRONG, EVENT_SKIP)
        if failed:
            # 首次失败与回炉再次失败都要重新排期
            self._schedule_requeue(decision, self.stats.asked)
        self._resolve_requeue(decision, correct=event in (EVENT_CORRECT_FIRST, EVENT_CORRECT_RETRY))

        if self.on_state_change is not None:
            try:
                self.on_state_change(updated)
            except Exception:  # noqa: BLE001 - 落库失败不得中断练习
                pass
        return updated


__all__ = ["QuestionScheduler", "ScheduleConfig", "Decision", "SchedulerStats"]
