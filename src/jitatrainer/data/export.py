"""CSV 导出（需求 FR-810「导出 CSV」）。

用 ``utf-8-sig``（带 BOM）编码，这样 Excel 双击打开中文不会乱码。
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..core.theory.notes import NOTE_NAMES, midi_name
from .analytics import AnalyticsRepository, local_date_of, parse_time
from .db import Database

ATTEMPT_HEADERS = (
    "时间(本地)",
    "训练项",
    "目标音名",
    "弦",
    "品",
    "结果",
    "尝试序号",
    "反应时间(ms)",
    "检测到",
    "检测频率(Hz)",
    "音分偏差",
    "来源",
)

DAILY_HEADERS = ("日期", "题量", "首次正确", "错误次数", "正确率", "平均反应(ms)", "练习分钟")


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    rows: int


def _local_text(iso: str | None) -> str:
    moment = parse_time(iso)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S") if moment else ""


def export_attempts_csv(
    db: Database,
    profile_id: int,
    path: Path | str,
    *,
    module_id: str = "pitch_find",
) -> ExportResult:
    """导出答题明细。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT a.answered_at, a.result, a.attempt_index, a.rt_ms, a.question_json, "
            "a.detected_pitch_class, a.detected_hz, a.cents_offset, a.source_tag "
            "FROM attempts a JOIN sessions s ON s.id = a.session_id "
            "WHERE s.profile_id = ? AND s.module_id = ? ORDER BY a.id",
            (profile_id, module_id),
        ).fetchall()

    written = 0
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(ATTEMPT_HEADERS)
        for row in rows:
            meta = _meta(row["question_json"])
            detected = (
                NOTE_NAMES[row["detected_pitch_class"] % 12]
                if row["detected_pitch_class"] is not None
                else ""
            )
            writer.writerow(
                (
                    _local_text(row["answered_at"]),
                    meta.get("item_key", ""),
                    NOTE_NAMES[meta.get("target_pc", 0) % 12] if "target_pc" in meta else "",
                    meta.get("string", ""),
                    meta.get("fret", ""),
                    row["result"],
                    row["attempt_index"],
                    row["rt_ms"] if row["rt_ms"] is not None else "",
                    detected,
                    f"{row['detected_hz']:.2f}" if row["detected_hz"] is not None else "",
                    f"{row['cents_offset']:+.1f}" if row["cents_offset"] is not None else "",
                    row["source_tag"],
                )
            )
            written += 1
    return ExportResult(path=target, rows=written)


def export_daily_csv(
    db: Database,
    profile_id: int,
    path: Path | str,
    *,
    module_id: str = "pitch_find",
    days: int = 90,
    today: date | None = None,
) -> ExportResult:
    """导出逐日汇总。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    with db.connect() as conn:
        series = AnalyticsRepository(db, profile_id, module_id).daily_series(
            conn, days=days, today=today
        )

    written = 0
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(DAILY_HEADERS)
        for point in series:
            writer.writerow(
                (
                    point.day.isoformat(),
                    point.answered,
                    point.correct_first,
                    point.wrong,
                    f"{point.accuracy * 100:.1f}%",
                    point.avg_rt_ms if point.avg_rt_ms is not None else "",
                    point.minutes,
                )
            )
            written += 1
    return ExportResult(path=target, rows=written)


def _meta(question_json: str) -> dict:
    try:
        return json.loads(question_json)
    except (TypeError, json.JSONDecodeError):
        return {}


__all__ = [
    "ATTEMPT_HEADERS",
    "DAILY_HEADERS",
    "ExportResult",
    "export_attempts_csv",
    "export_daily_csv",
]
