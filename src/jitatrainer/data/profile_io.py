"""单档案导出 / 导入（需求 FR-105、FR-1000）。

导出为一个 JSON 文件，含：档案信息、档案设置、训练项、会话、答题明细。

设计要点：

- **显式列出字段**（不用 ``SELECT *``），导出格式才是稳定的，
  以后加字段也不会无意间改变文件格式。
- 会话与答题用 ``session_index`` 关联，导入时重新分配 id，
  这样同一个文件可以导入到任意档案、也不会撞 id。
- 导入前校验格式与版本，不支持的版本给出明确错误而不是写坏数据库。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import __version__
from .db import Database, utc_now_iso

EXPORT_FORMAT = "jitatrainer-profile"
EXPORT_VERSION = 1

PROFILE_COLUMNS = ("name", "color", "created_at")
ITEM_COLUMNS = (
    "module_id",
    "item_key",
    "pitch_class",
    "level_id",
    "ease",
    "interval_index",
    "due_at",
    "reps",
    "lapses",
    "seen_count",
    "correct_count",
    "error_rate",
    "avg_rt_ms",
    "last_seen_at",
)
SESSION_COLUMNS = (
    "module_id",
    "mode",
    "target_value",
    "started_at",
    "ended_at",
    "total",
    "correct_first",
    "correct_final",
    "wrong",
    "skipped",
    "timeout",
    "avg_rt_ms",
    "best_combo",
    "score",
)
ATTEMPT_COLUMNS = (
    "item_id",
    "question_json",
    "source_tag",
    "answered_at",
    "result",
    "attempt_index",
    "rt_ms",
    "detected_pitch_class",
    "detected_hz",
    "cents_offset",
    "confidence",
)


class ProfileFormatError(ValueError):
    """导入文件格式或版本不受支持。"""


@dataclass(frozen=True, slots=True)
class ExportSummary:
    path: Path
    profile_name: str
    items: int
    sessions: int
    attempts: int

    def describe(self) -> str:
        return f"{self.profile_name}：训练项 {self.items}、会话 {self.sessions}、答题 {self.attempts}"


@dataclass(frozen=True, slots=True)
class ImportSummary:
    profile_id: int
    profile_name: str
    items: int
    sessions: int
    attempts: int
    mode: str


def export_profile(db: Database, profile_id: int, path: Path | str) -> ExportSummary:
    """把单个档案导出为 JSON。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    with db.connect() as conn:
        profile = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if profile is None:
            raise ProfileFormatError(f"档案不存在：{profile_id}")

        settings_rows = conn.execute(
            "SELECT key, value FROM profile_settings WHERE profile_id = ?", (profile_id,)
        ).fetchall()
        item_rows = conn.execute(
            f"SELECT {', '.join(ITEM_COLUMNS)} FROM items WHERE profile_id = ? ORDER BY id",
            (profile_id,),
        ).fetchall()
        session_rows = conn.execute(
            f"SELECT id, {', '.join(SESSION_COLUMNS)} FROM sessions WHERE profile_id = ? ORDER BY id",
            (profile_id,),
        ).fetchall()

        index_of = {row["id"]: index for index, row in enumerate(session_rows)}
        attempt_rows = []
        for row in conn.execute(
            "SELECT session_id, "
            + ", ".join(ATTEMPT_COLUMNS)
            + " FROM attempts WHERE session_id IN (SELECT id FROM sessions WHERE profile_id = ?) "
            "ORDER BY id",
            (profile_id,),
        ):
            entry = {column: row[column] for column in ATTEMPT_COLUMNS}
            entry["session_index"] = index_of.get(row["session_id"], 0)
            attempt_rows.append(entry)

    payload = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "app_version": __version__,
        "exported_at": utc_now_iso(),
        "profile": {column: profile[column] for column in PROFILE_COLUMNS},
        "settings": {row["key"]: row["value"] for row in settings_rows},
        "items": [{column: row[column] for column in ITEM_COLUMNS} for row in item_rows],
        "sessions": [{column: row[column] for column in SESSION_COLUMNS} for row in session_rows],
        "attempts": attempt_rows,
    }

    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return ExportSummary(
        path=target,
        profile_name=str(profile["name"]),
        items=len(payload["items"]),
        sessions=len(payload["sessions"]),
        attempts=len(payload["attempts"]),
    )


def read_profile_file(path: Path | str) -> dict:
    """读取并校验档案文件。"""
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileFormatError(f"不是合法的 JSON 文件：{exc}") from exc
    except OSError as exc:
        raise ProfileFormatError(f"无法读取文件：{exc}") from exc

    if not isinstance(payload, dict) or payload.get("format") != EXPORT_FORMAT:
        raise ProfileFormatError("不是 JitaTrainer 档案文件")
    version = payload.get("version")
    if not isinstance(version, int) or version > EXPORT_VERSION:
        raise ProfileFormatError(
            f"档案版本 {version} 高于本程序支持的 {EXPORT_VERSION}，请升级程序后再导入"
        )
    return payload


def import_profile(
    db: Database,
    path: Path | str,
    *,
    mode: str = "new",
    target_profile_id: int | None = None,
) -> ImportSummary:
    """导入档案。

    ``mode="new"``：新建一个档案（名字冲突时自动加后缀）。
    ``mode="overwrite"``：清空 ``target_profile_id`` 的数据后写入（档案本身保留）。

    整个导入在**一个事务**里完成，任何一步失败都不会留下半个档案。
    """
    payload = read_profile_file(path)
    if mode not in ("new", "overwrite"):
        raise ValueError(f"未知导入模式：{mode}")
    if mode == "overwrite" and target_profile_id is None:
        raise ValueError("覆盖导入必须指定目标档案")

    profile_data = payload.get("profile") or {}
    with db.connect() as conn:
        try:
            conn.execute("BEGIN")
            if mode == "overwrite":
                profile_id = int(target_profile_id)  # type: ignore[arg-type]
                conn.execute(
                    "UPDATE profiles SET color = ?, last_used_at = ? WHERE id = ?",
                    (profile_data.get("color", "#4A90D9"), utc_now_iso(), profile_id),
                )
                conn.execute("DELETE FROM attempts WHERE session_id IN "
                             "(SELECT id FROM sessions WHERE profile_id = ?)", (profile_id,))
                conn.execute("DELETE FROM sessions WHERE profile_id = ?", (profile_id,))
                conn.execute("DELETE FROM items WHERE profile_id = ?", (profile_id,))
                conn.execute("DELETE FROM profile_settings WHERE profile_id = ?", (profile_id,))
            else:
                name = _unique_profile_name(conn, str(profile_data.get("name") or "导入的档案"))
                cursor = conn.execute(
                    "INSERT INTO profiles (name, color, created_at, last_used_at) VALUES (?, ?, ?, ?)",
                    (
                        name,
                        profile_data.get("color", "#4A90D9"),
                        profile_data.get("created_at") or utc_now_iso(),
                        utc_now_iso(),
                    ),
                )
                profile_id = int(cursor.lastrowid)

            items = _insert_items(conn, profile_id, payload.get("items") or [])
            sessions, index_map = _insert_sessions(conn, profile_id, payload.get("sessions") or [])
            attempts = _insert_attempts(conn, payload.get("attempts") or [], index_map)
            settings = _insert_settings(conn, profile_id, payload.get("settings") or {})
            conn.commit()
        except sqlite3.Error as exc:
            # 整体回滚：绝不留半个档案。同时把底层错误换成用户能看懂的说法。
            conn.rollback()
            raise ProfileFormatError(f"档案数据不完整或已损坏，导入已取消：{exc}") from exc
        except Exception:
            conn.rollback()
            raise

        name_row = conn.execute("SELECT name FROM profiles WHERE id = ?", (profile_id,)).fetchone()

    return ImportSummary(
        profile_id=profile_id,
        profile_name=str(name_row["name"]) if name_row else "",
        items=items,
        sessions=sessions,
        attempts=attempts,
        mode=mode,
    )


def _unique_profile_name(conn: sqlite3.Connection, wanted: str) -> str:
    existing = {row["name"] for row in conn.execute("SELECT name FROM profiles")}
    if wanted not in existing:
        return wanted
    suffix = 2
    while f"{wanted} ({suffix})" in existing:
        suffix += 1
    return f"{wanted} ({suffix})"


#: 缺失时可用默认值补齐的字段。手改过的文件、或以后新增的字段都能导入。
ITEM_DEFAULTS: dict[str, object] = {
    "module_id": "pitch_find",
    "ease": 2.5,
    "interval_index": 0,
    "due_at": None,
    "reps": 0,
    "lapses": 0.0,
    "seen_count": 0,
    "correct_count": 0,
    "error_rate": 0.0,
    "avg_rt_ms": None,
    "last_seen_at": None,
}
SESSION_DEFAULTS: dict[str, object] = {
    "module_id": "pitch_find",
    "mode": "free",
    "target_value": None,
    "ended_at": None,
    "total": 0,
    "correct_first": 0,
    "correct_final": 0,
    "wrong": 0,
    "skipped": 0,
    "timeout": 0,
    "avg_rt_ms": None,
    "best_combo": 0,
    "score": 0,
}
ATTEMPT_DEFAULTS: dict[str, object] = {
    "item_id": None,
    "question_json": "{}",
    "source_tag": "new",
    "attempt_index": 1,
    "rt_ms": None,
    "detected_pitch_class": None,
    "detected_hz": None,
    "cents_offset": None,
    "confidence": None,
}

#: 无论如何都必须存在于文件里的字段
ITEM_REQUIRED = ("item_key", "pitch_class", "level_id")
SESSION_REQUIRED = ("started_at",)
ATTEMPT_REQUIRED = ("result",)


def _value(entry: dict, column: str, defaults: dict, required: tuple, what: str) -> object:
    raw = entry.get(column)
    if raw is None:
        if column in required:
            raise ProfileFormatError(f"档案缺少必要字段 {column}（{what}）")
        if column in defaults:
            return defaults[column]
    return raw


def _insert_items(conn: sqlite3.Connection, profile_id: int, items: list[dict]) -> int:
    columns = ["profile_id", *ITEM_COLUMNS]
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT OR REPLACE INTO items ({', '.join(columns)}) VALUES ({placeholders})"
    for item in items:
        if not isinstance(item, dict):
            raise ProfileFormatError("items 里有非法条目")
        values = [profile_id] + [
            _value(item, column, ITEM_DEFAULTS, ITEM_REQUIRED, "训练项") for column in ITEM_COLUMNS
        ]
        conn.execute(sql, values)
    return len(items)


def _insert_sessions(
    conn: sqlite3.Connection, profile_id: int, sessions: list[dict]
) -> tuple[int, dict[int, int]]:
    index_map: dict[int, int] = {}
    columns = ["profile_id", *SESSION_COLUMNS]
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO sessions ({', '.join(columns)}) VALUES ({placeholders})"
    for index, session in enumerate(sessions):
        if not isinstance(session, dict):
            raise ProfileFormatError("sessions 里有非法条目")
        values = [profile_id] + [
            _value(session, column, SESSION_DEFAULTS, SESSION_REQUIRED, "会话")
            for column in SESSION_COLUMNS
        ]
        cursor = conn.execute(sql, values)
        index_map[index] = int(cursor.lastrowid)
    return len(sessions), index_map


def _insert_attempts(
    conn: sqlite3.Connection, attempts: list[dict], index_map: dict[int, int]
) -> int:
    written = 0
    columns = ["session_id", *ATTEMPT_COLUMNS]
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO attempts ({', '.join(columns)}) VALUES ({placeholders})"
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise ProfileFormatError("attempts 里有非法条目")
        session_id = index_map.get(int(attempt.get("session_index", 0)))
        if session_id is None:
            continue  # 找不到会话的答题记录直接跳过，不写坏数据
        values = [session_id] + [
            _value(attempt, column, ATTEMPT_DEFAULTS, ATTEMPT_REQUIRED, "答题")
            for column in ATTEMPT_COLUMNS
        ]
        conn.execute(sql, values)
        written += 1
    return written


def _insert_settings(conn: sqlite3.Connection, profile_id: int, settings: dict) -> int:
    for key, value in settings.items():
        conn.execute(
            "INSERT OR REPLACE INTO profile_settings (profile_id, key, value) VALUES (?, ?, ?)",
            (profile_id, key, str(value)),
        )
    return len(settings)


def suggest_export_name(profile_name: str, *, now: datetime | None = None) -> str:
    """默认文件名：``JitaTrainer-档案名-时间戳.json``（去掉文件名非法字符）。"""
    moment = now or datetime.now()
    safe = "".join(ch for ch in profile_name if ch not in '\\/:*?"<>|').strip() or "profile"
    return f"JitaTrainer-{safe}-{moment:%Y%m%d-%H%M%S}.json"


__all__ = [
    "EXPORT_FORMAT",
    "EXPORT_VERSION",
    "ExportSummary",
    "ImportSummary",
    "ProfileFormatError",
    "export_profile",
    "import_profile",
    "read_profile_file",
    "suggest_export_name",
]
