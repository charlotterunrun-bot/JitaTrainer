"""简化 SM-2 间隔重复（需求 FR-610）。

训练项 = 音名 × 把位区间。每个训练项维护熟练度与下次到期时间。

间隔序列：``0 / 1 / 3 / 7 / 16 / 35 / 75 / 160`` 天（索引 0 = 当天回炉）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

#: 间隔档位（天）
INTERVALS_DAYS: tuple[int, ...] = (0, 1, 3, 7, 16, 35, 75, 160)

#: 复习事件的种类
EVENT_CORRECT_FIRST = "correct_first"  # 首次尝试即弹对
EVENT_CORRECT_RETRY = "correct_retry"  # 答错后改正
EVENT_WRONG = "wrong"
EVENT_SKIP = "skip"  # 跳过或超时（记为"不会"）
EVENT_HINTED = "hinted"  # 带音名提示答对（不计入熟练度）

EVENT_IDS = (
    EVENT_CORRECT_FIRST,
    EVENT_CORRECT_RETRY,
    EVENT_WRONG,
    EVENT_SKIP,
    EVENT_HINTED,
)

#: 错误率的指数滑动平均系数
ERROR_EMA_ALPHA = 0.3
#: 反应时间的指数滑动平均系数
RT_EMA_ALPHA = 0.3


@dataclass(frozen=True, slots=True)
class ItemState:
    """一个训练项的熟练度状态。"""

    item_key: str
    pitch_class: int
    level_id: str
    ease: float = 2.5
    interval_index: int = 0
    reps: int = 0
    lapses: float = 0.0
    seen_count: int = 0
    correct_count: int = 0
    error_rate: float = 0.0
    avg_rt_ms: int | None = None
    due_at: datetime | None = None
    last_seen_at: datetime | None = None

    @property
    def is_new(self) -> bool:
        return self.seen_count == 0

    @property
    def interval_days(self) -> int:
        return INTERVALS_DAYS[min(self.interval_index, len(INTERVALS_DAYS) - 1)]

    def is_due(self, now: datetime) -> bool:
        return self.due_at is not None and self.due_at <= now

    def overdue_days(self, now: datetime) -> float:
        if self.due_at is None:
            return 0.0
        return max(0.0, (now - self.due_at).total_seconds() / 86400.0)


def review(
    state: ItemState,
    event: str,
    *,
    now: datetime,
    rt_ms: int | None = None,
) -> ItemState:
    """按一次答题结果更新训练项状态。

    规则（需求 FR-610）：

    ==================== =========================== ==========================
    事件                  间隔索引                    下次到期
    ==================== =========================== ==========================
    首次即弹对             +1（封顶）                  now + 间隔[新索引]
    答错后改正 / 答错 / 跳过  归零                       当天（now）
    带提示答对             不变                        不变
    ==================== ============================ ==========================
    """
    if event not in EVENT_IDS:
        raise ValueError(f"未知复习事件：{event}")

    seen = state.seen_count + 1
    last_seen = now

    if event == EVENT_HINTED:
        # 不计入熟练度提升，仅记录
        return replace(state, seen_count=seen, last_seen_at=last_seen)

    if event == EVENT_CORRECT_FIRST:
        index = min(state.interval_index + 1, len(INTERVALS_DAYS) - 1)
        error_rate = _ema(state.error_rate, 0.0, state.seen_count)
        avg_rt = (
            rt_ms
            if rt_ms is not None and state.avg_rt_ms is None
            else int(_ema(state.avg_rt_ms or 0, rt_ms, state.seen_count))
            if rt_ms is not None
            else state.avg_rt_ms
        )
        return replace(
            state,
            interval_index=index,
            reps=state.reps + 1,
            seen_count=seen,
            correct_count=state.correct_count + 1,
            error_rate=error_rate,
            avg_rt_ms=avg_rt,
            due_at=now + timedelta(days=INTERVALS_DAYS[index]),
            last_seen_at=last_seen,
        )

    # 答错 / 改正 / 跳过：回到当天
    error_rate = _ema(state.error_rate, 1.0, state.seen_count)
    lapses = state.lapses + (0.5 if event == EVENT_SKIP else 1.0)
    correct_count = state.correct_count + (1 if event == EVENT_CORRECT_RETRY else 0)
    return replace(
        state,
        interval_index=0,
        reps=state.reps,
        lapses=lapses,
        seen_count=seen,
        correct_count=correct_count,
        error_rate=error_rate,
        due_at=now + timedelta(days=INTERVALS_DAYS[0]),
        last_seen_at=last_seen,
    )


def _ema(previous: float, value: float, seen: int) -> float:
    """指数滑动平均；第一次观测直接取观测值。"""
    if seen <= 0:
        return float(value)
    return float(ERROR_EMA_ALPHA * value + (1 - ERROR_EMA_ALPHA) * previous)


def initial_state(item_key: str, pitch_class: int, level_id: str) -> ItemState:
    return ItemState(item_key=item_key, pitch_class=pitch_class, level_id=level_id)


__all__ = [
    "INTERVALS_DAYS",
    "EVENT_CORRECT_FIRST",
    "EVENT_CORRECT_RETRY",
    "EVENT_WRONG",
    "EVENT_SKIP",
    "EVENT_HINTED",
    "EVENT_IDS",
    "ItemState",
    "review",
    "initial_state",
]
