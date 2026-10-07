"""统计查询（M4）。

设计取舍：

- **时间统一存 UTC，按本地日期聚合**。SQLite 里做时区转换很别扭，
  而一年的答题明细只有几千行，所以取原始行、在 Python 里分桶——
  这样也更容易测试（可注入"今天"是哪天）。
- 只读，不做任何写入；统计失败不应影响练习。
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .db import Database


def parse_time(text: str | None) -> datetime | None:
    """解析库里存的时间戳（UTC，带 Z 或不带时区）。"""
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def local_date_of(text: str | None) -> date | None:
    """把 UTC 时间戳转成**本地日期**（用户看到的"今天"）。"""
    moment = parse_time(text)
    return moment.astimezone().date() if moment else None


def _round4(value: float) -> float:
    return round(value, 4)


@dataclass(frozen=True, slots=True)
class DailyPoint:
    day: date
    answered: int
    correct_first: int
    wrong: int
    accuracy: float
    avg_rt_ms: int | None
    minutes: float


@dataclass(frozen=True, slots=True)
class NoteCell:
    """训练项维度的统计（错音热力图的数据源）。"""

    item_key: str
    pitch_class: int
    level_id: str
    attempts: int
    wrong: int
    error_rate: float
    avg_rt_ms: int | None


@dataclass(frozen=True, slots=True)
class PeriodStats:
    label: str
    start: date | None
    end: date | None
    answered: int
    correct_first: int
    accuracy: float
    avg_rt_ms: int | None
    minutes: float


@dataclass(frozen=True, slots=True)
class Comparison:
    """两个时段的对比（需求 FR-810「时段对比」）。"""

    current: PeriodStats
    previous: PeriodStats

    @property
    def answer_delta(self) -> int:
        return self.current.answered - self.previous.answered

    @property
    def accuracy_delta(self) -> float:
        return _round4(self.current.accuracy - self.previous.accuracy)

    @property
    def rt_delta_ms(self) -> int | None:
        if self.current.avg_rt_ms is None or self.previous.avg_rt_ms is None:
            return None
        return self.current.avg_rt_ms - self.previous.avg_rt_ms


class AnalyticsRepository:
    """练习统计查询。"""

    def __init__(self, db: Database, profile_id: int, module_id: str = "pitch_find") -> None:
        self.db = db
        self.profile_id = profile_id
        self.module_id = module_id

    # ------------------------------------------------------------------ 原始数据
    def _attempt_rows(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT a.answered_at, a.result, a.rt_ms, a.question_json "
                "FROM attempts a JOIN sessions s ON s.id = a.session_id "
                "WHERE s.profile_id = ? AND s.module_id = ? "
                "ORDER BY a.id",
                (self.profile_id, self.module_id),
            )
        )

    def _session_rows(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT started_at, ended_at, total, correct_first, avg_rt_ms FROM sessions "
                "WHERE profile_id = ? AND module_id = ? AND ended_at IS NOT NULL",
                (self.profile_id, self.module_id),
            )
        )

    @staticmethod
    def _question_meta(question_json: str) -> tuple[int | None, str | None, str | None]:
        try:
            data = json.loads(question_json)
        except (TypeError, json.JSONDecodeError):
            return None, None, None
        return data.get("target_pc"), data.get("level_id"), data.get("item_key")

    # ------------------------------------------------------------------ 逐日曲线
    def daily_series(
        self, conn: sqlite3.Connection, *, days: int = 30, today: date | None = None
    ) -> list[DailyPoint]:
        """最近 ``days`` 天的逐日统计（含没有练习的空白天，便于画曲线）。"""
        end = today or datetime.now().date()
        start = end - timedelta(days=days - 1)

        buckets: dict[date, dict] = defaultdict(
            lambda: {"answered": 0, "correct_first": 0, "wrong": 0, "rts": []}
        )
        for row in self._attempt_rows(conn):
            day = local_date_of(row["answered_at"])
            if day is None or not (start <= day <= end):
                continue
            bucket = buckets[day]
            bucket["answered"] += 1
            if row["result"] == "correct":
                bucket["correct_first"] += 1
                if row["rt_ms"]:
                    bucket["rts"].append(int(row["rt_ms"]))
            elif row["result"] == "wrong":
                bucket["wrong"] += 1

        minutes: dict[date, float] = defaultdict(float)
        for row in self._session_rows(conn):
            started = parse_time(row["started_at"])
            ended = parse_time(row["ended_at"])
            if started is None or ended is None:
                continue
            day = started.astimezone().date()
            if not (start <= day <= end):
                continue
            minutes[day] += max(0.0, (ended - started).total_seconds() / 60.0)

        series: list[DailyPoint] = []
        for offset in range(days):
            day = start + timedelta(days=offset)
            bucket = buckets.get(day)
            answered = bucket["answered"] if bucket else 0
            correct = bucket["correct_first"] if bucket else 0
            rts = bucket["rts"] if bucket else []
            series.append(
                DailyPoint(
                    day=day,
                    answered=answered,
                    correct_first=correct,
                    wrong=bucket["wrong"] if bucket else 0,
                    accuracy=_round4(correct / answered) if answered else 0.0,
                    avg_rt_ms=int(sum(rts) / len(rts)) if rts else None,
                    minutes=round(minutes.get(day, 0.0), 1),
                )
            )
        return series

    # ------------------------------------------------------------------ 训练项
    def note_cells(self, conn: sqlite3.Connection, *, level_id: str | None = None) -> list[NoteCell]:
        """按训练项聚合（错音热力图用）。"""
        aggregate: dict[str, dict] = defaultdict(
            lambda: {"attempts": 0, "wrong": 0, "rts": [], "pc": None, "level": None}
        )
        for row in self._attempt_rows(conn):
            pitch_class, row_level, item_key = self._question_meta(row["question_json"])
            if pitch_class is None or item_key is None:
                continue
            if level_id is not None and row_level != level_id:
                continue
            bucket = aggregate[item_key]
            bucket["attempts"] += 1
            bucket["pc"] = pitch_class
            bucket["level"] = row_level
            if row["result"] == "wrong":
                bucket["wrong"] += 1
            elif row["result"] == "correct" and row["rt_ms"]:
                bucket["rts"].append(int(row["rt_ms"]))

        cells = [
            NoteCell(
                item_key=key,
                pitch_class=int(bucket["pc"]),
                level_id=str(bucket["level"]),
                attempts=bucket["attempts"],
                wrong=bucket["wrong"],
                error_rate=_round4(bucket["wrong"] / bucket["attempts"]) if bucket["attempts"] else 0.0,
                avg_rt_ms=int(sum(bucket["rts"]) / len(bucket["rts"])) if bucket["rts"] else None,
            )
            for key, bucket in aggregate.items()
        ]
        cells.sort(key=lambda cell: (-cell.error_rate, -cell.wrong, cell.pitch_class))
        return cells

    def weak_notes(self, conn: sqlite3.Connection, *, limit: int = 5) -> list[NoteCell]:
        """最需要加强的训练项（错误率高、错得多）。"""
        cells = [cell for cell in self.note_cells(conn) if cell.wrong > 0]
        cells.sort(key=lambda cell: (-cell.error_rate, -cell.wrong))
        return cells[:limit]

    def wrong_note_ranking(
        self, conn: sqlite3.Connection, *, level_id: str | None = None
    ) -> list[tuple[int, int, float]]:
        """错音排行：``(音名序号, 错误次数, 错误率)``。"""
        counts: dict[int, dict] = defaultdict(lambda: {"attempts": 0, "wrong": 0})
        for row in self._attempt_rows(conn):
            pitch_class, row_level, _ = self._question_meta(row["question_json"])
            if pitch_class is None:
                continue
            if level_id is not None and row_level != level_id:
                continue
            counts[pitch_class]["attempts"] += 1
            if row["result"] == "wrong":
                counts[pitch_class]["wrong"] += 1
        ranking = [
            (pc, data["wrong"], _round4(data["wrong"] / data["attempts"]) if data["attempts"] else 0.0)
            for pc, data in counts.items()
        ]
        ranking.sort(key=lambda item: (-item[1], -item[2], item[0]))
        return ranking

    # ------------------------------------------------------------------ 时段
    def _period(
        self, attempts: list[sqlite3.Row], sessions: list[sqlite3.Row], start: date, end: date, label: str
    ) -> PeriodStats:
        answered = correct = 0
        rts: list[int] = []
        minutes = 0.0

        for row in attempts:
            day = local_date_of(row["answered_at"])
            if day is None or not (start <= day <= end):
                continue
            answered += 1
            if row["result"] == "correct":
                correct += 1
                if row["rt_ms"]:
                    rts.append(int(row["rt_ms"]))
        for row in sessions:
            started = parse_time(row["started_at"])
            ended = parse_time(row["ended_at"])
            if started is None or ended is None:
                continue
            day = started.astimezone().date()
            if start <= day <= end:
                minutes += max(0.0, (ended - started).total_seconds() / 60.0)

        return PeriodStats(
            label=label,
            start=start,
            end=end,
            answered=answered,
            correct_first=correct,
            accuracy=_round4(correct / answered) if answered else 0.0,
            avg_rt_ms=int(sum(rts) / len(rts)) if rts else None,
            minutes=round(minutes, 1),
        )

    def period(
        self, conn: sqlite3.Connection, *, days: int = 7, offset: int = 0, today: date | None = None
    ) -> PeriodStats:
        """取一个时段：``offset=0`` 是最近 N 天，``offset=1`` 是再往前 N 天。"""
        end = (today or datetime.now().date()) - timedelta(days=offset * days)
        start = end - timedelta(days=days - 1)
        label = f"{start.isoformat()}~{end.isoformat()}"
        return self._period(self._attempt_rows(conn), self._session_rows(conn), start, end, label)

    def compare(
        self, conn: sqlite3.Connection, *, days: int = 7, today: date | None = None
    ) -> Comparison:
        """最近 N 天 vs 前 N 天（需求 FR-810「时段对比」）。"""
        attempts = self._attempt_rows(conn)
        sessions = self._session_rows(conn)
        end = today or datetime.now().date()

        current_end = end
        current_start = end - timedelta(days=days - 1)
        previous_end = current_start - timedelta(days=1)
        previous_start = previous_end - timedelta(days=days - 1)

        return Comparison(
            current=self._period(
                attempts, sessions, current_start, current_end, f"最近 {days} 天"
            ),
            previous=self._period(
                attempts, sessions, previous_start, previous_end, f"前 {days} 天"
            ),
        )

    def overall(self, conn: sqlite3.Connection) -> PeriodStats:
        """全部历史。"""
        return self._period(
            self._attempt_rows(conn), self._session_rows(conn), date(1970, 1, 1), date(2999, 1, 1), "全部"
        )


__all__ = [
    "AnalyticsRepository",
    "Comparison",
    "DailyPoint",
    "NoteCell",
    "PeriodStats",
    "local_date_of",
    "parse_time",
]
