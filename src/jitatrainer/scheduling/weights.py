"""易错项加权（技术方案 §9.3）。

    w = (1 + 2.0 × error_rate)                      错误率权重
      × (1 + 1.5 × max(0, (target_rt − avg_rt) / target_rt))   慢反应权重
      × (0.5 + 0.5 × min(1, 距上次练习天数 / 7))        时间衰减权重
"""

from __future__ import annotations

from datetime import datetime

from .srs import ItemState

#: 目标反应时间（毫秒）：慢于它会被加权
DEFAULT_TARGET_RT_MS = 2500
#: 时间衰减的天数上限
TIME_DECAY_DAYS = 7


def error_factor(state: ItemState) -> float:
    return 1.0 + 2.0 * max(0.0, min(1.0, state.error_rate))


def reaction_factor(state: ItemState, target_rt_ms: int = DEFAULT_TARGET_RT_MS) -> float:
    if not state.avg_rt_ms or target_rt_ms <= 0:
        return 1.0
    slow_ratio = max(0.0, (target_rt_ms - state.avg_rt_ms) / target_rt_ms)
    return 1.0 + 1.5 * slow_ratio


def time_decay_factor(
    state: ItemState, now: datetime, max_days: int = TIME_DECAY_DAYS
) -> float:
    if state.last_seen_at is None:
        return 1.0
    days = max(0.0, (now - state.last_seen_at).total_seconds() / 86400.0)
    return 0.5 + 0.5 * min(1.0, days / max_days)


def weak_weight(
    state: ItemState,
    *,
    now: datetime,
    target_rt_ms: int = DEFAULT_TARGET_RT_MS,
) -> float:
    """薄弱项的抽取权重（越大越该练）。"""
    return error_factor(state) * reaction_factor(state, target_rt_ms) * time_decay_factor(state, now)


def is_weak(state: ItemState, *, threshold: float = 1.0) -> bool:
    """是否算"薄弱项"（错误率或反应时间明显落后）。"""
    if state.is_new:
        return False
    return state.error_rate > 0.001 or (state.avg_rt_ms or 0) > DEFAULT_TARGET_RT_MS


__all__ = [
    "DEFAULT_TARGET_RT_MS",
    "TIME_DECAY_DAYS",
    "error_factor",
    "reaction_factor",
    "time_decay_factor",
    "weak_weight",
    "is_weak",
]
