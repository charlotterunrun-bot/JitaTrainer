"""难度进阶建议（需求 FR-620）。

触发条件（对当前把位区间，取**最近 3 次会话**）：
  正确率 ≥ 90% 且平均反应时间 ≤ 2.5s 且累计答题数 ≥ 60。

程序**只给建议，不自动切换**——把决定权留给用户。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True, slots=True)
class AdviceConfig:
    recent_sessions: int = 3
    min_accuracy: float = 0.90
    max_avg_rt_ms: int = 2500
    min_answered: int = 60


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """用于评估的会话摘要（由数据库行转换而来）。"""

    asked: int
    accuracy_first: float
    avg_rt_ms: int | None


def should_advance(
    records: Sequence[SessionRecord], config: AdviceConfig | None = None
) -> bool:
    """是否建议进入下一难度。

    ``records`` 应为该难度的历史会话（**由新到旧**）。
    """
    cfg = config or AdviceConfig()
    recent = list(records)[: cfg.recent_sessions]
    if len(recent) < cfg.recent_sessions:
        return False

    total_answered = sum(r.asked for r in recent)
    if total_answered < cfg.min_answered:
        return False

    for record in recent:
        if record.accuracy_first < cfg.min_accuracy:
            return False
        if record.avg_rt_ms is not None and record.avg_rt_ms > cfg.max_avg_rt_ms:
            return False
    return True


def record_from_row(row) -> SessionRecord:  # noqa: ANN001 - sqlite3.Row
    """把 ``sessions`` 表的一行转成评估用的摘要。"""
    asked = int(row["total"] or 0)
    correct = int(row["correct_first"] or 0)
    accuracy = correct / asked if asked else 0.0
    return SessionRecord(asked=asked, accuracy_first=accuracy, avg_rt_ms=row["avg_rt_ms"])


__all__ = ["AdviceConfig", "SessionRecord", "should_advance", "record_from_row"]
