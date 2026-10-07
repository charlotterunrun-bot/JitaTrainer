"""数据库访问层。

职责：
  - 连接管理（WAL、外键、Row 工厂）
  - 版本化迁移（单事务，失败回滚且不破坏原库）
  - 档案、设置、训练项的基本读写
  - 备份（启动时与会话结束后调用）

本模块不依赖 PySide6。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..paths import backups_dir as default_backups_dir
from ..paths import db_path as default_db_path
from .migrations import LATEST_VERSION, MIGRATIONS

SCHEMA_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
"""

DEFAULT_PROFILE_NAME = "默认档案"


def utc_now_iso() -> str:
    """统一的时间戳格式（UTC，秒精度，带 Z）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Database:
    """SQLite 封装。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()

    # ------------------------------------------------------------------ 连接
    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ------------------------------------------------------------------ 迁移
    @staticmethod
    def current_version(conn: sqlite3.Connection) -> int:
        conn.execute(SCHEMA_VERSION_TABLE)
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return int(row["v"]) if row and row["v"] is not None else 0

    @staticmethod
    def apply_migrations(conn: sqlite3.Connection) -> int:
        """应用所有未执行的迁移，返回最终版本号。"""
        conn.execute(SCHEMA_VERSION_TABLE)
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = int(row["v"]) if row and row["v"] is not None else 0

        for migration in MIGRATIONS:
            if migration.version <= current:
                continue
            try:
                with conn:  # 单事务
                    conn.executescript(migration.sql)
                    conn.execute(
                        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                        (migration.version, utc_now_iso()),
                    )
            except Exception:
                conn.rollback()
                raise
            current = migration.version
        return current

    def initialize(self) -> int:
        """建库 + 迁移 + 保证存在一个默认档案。返回 schema 版本。"""
        with self.connect() as conn:
            version = self.apply_migrations(conn)
            count = conn.execute("SELECT COUNT(*) AS c FROM profiles").fetchone()["c"]
            if count == 0:
                conn.execute(
                    "INSERT INTO profiles (name, color, created_at, last_used_at) VALUES (?, ?, ?, ?)",
                    (DEFAULT_PROFILE_NAME, "#4A90D9", utc_now_iso(), utc_now_iso()),
                )
            conn.commit()
        return version

    # ------------------------------------------------------------------ 档案
    @staticmethod
    def list_profiles(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return list(conn.execute("SELECT * FROM profiles ORDER BY id"))

    @staticmethod
    def create_profile(conn: sqlite3.Connection, name: str, color: str = "#4A90D9") -> int:
        cursor = conn.execute(
            "INSERT INTO profiles (name, color, created_at, last_used_at) VALUES (?, ?, ?, ?)",
            (name, color, utc_now_iso(), utc_now_iso()),
        )
        conn.commit()
        return int(cursor.lastrowid)

    @staticmethod
    def rename_profile(conn: sqlite3.Connection, profile_id: int, name: str) -> None:
        conn.execute("UPDATE profiles SET name = ? WHERE id = ?", (name, profile_id))
        conn.commit()

    @staticmethod
    def delete_profile(conn: sqlite3.Connection, profile_id: int) -> None:
        conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        conn.commit()

    # ------------------------------------------------------------------ 设置
    @staticmethod
    def get_app_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    @staticmethod
    def set_app_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()

    @staticmethod
    def get_profile_setting(
        conn: sqlite3.Connection, profile_id: int, key: str, default: str | None = None
    ) -> str | None:
        row = conn.execute(
            "SELECT value FROM profile_settings WHERE profile_id = ? AND key = ?",
            (profile_id, key),
        ).fetchone()
        return row["value"] if row else default

    @staticmethod
    def set_profile_setting(conn: sqlite3.Connection, profile_id: int, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO profile_settings (profile_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(profile_id, key) DO UPDATE SET value = excluded.value",
            (profile_id, key, value),
        )
        conn.commit()

    # ------------------------------------------------------------------ 训练项
    @staticmethod
    def upsert_item(
        conn: sqlite3.Connection,
        *,
        profile_id: int,
        module_id: str,
        item_key: str,
        pitch_class: int,
        level_id: str,
    ) -> int:
        """确保训练项存在，返回其 id。"""
        conn.execute(
            "INSERT INTO items (profile_id, module_id, item_key, pitch_class, level_id) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(profile_id, module_id, item_key) DO NOTHING",
            (profile_id, module_id, item_key, pitch_class, level_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM items WHERE profile_id = ? AND module_id = ? AND item_key = ?",
            (profile_id, module_id, item_key),
        ).fetchone()
        return int(row["id"])

    @staticmethod
    def get_item(conn: sqlite3.Connection, profile_id: int, module_id: str, item_key: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM items WHERE profile_id = ? AND module_id = ? AND item_key = ?",
            (profile_id, module_id, item_key),
        ).fetchone()

    @staticmethod
    def due_items(conn: sqlite3.Connection, profile_id: int, module_id: str, now_iso: str | None = None):
        now_iso = now_iso or utc_now_iso()
        return list(
            conn.execute(
                "SELECT * FROM items WHERE profile_id = ? AND module_id = ? "
                "AND due_at IS NOT NULL AND due_at <= ? ORDER BY due_at",
                (profile_id, module_id, now_iso),
            )
        )

    # ------------------------------------------------------------------ 事件日志
    @staticmethod
    def log_event(
        conn: sqlite3.Connection, level: str, message: str, context: dict | None = None
    ) -> None:
        conn.execute(
            "INSERT INTO app_events (ts, level, message, context_json) VALUES (?, ?, ?, ?)",
            (utc_now_iso(), level, message, json.dumps(context, ensure_ascii=False) if context else None),
        )
        conn.commit()


def backup_database(
    db_file: Path | str | None = None,
    backup_dir: Path | str | None = None,
    keep: int = 10,
) -> Path | None:
    """备份数据库，保留最近 ``keep`` 份。数据库不存在时返回 None。

    文件名带毫秒并用序号兜底，避免同一秒内多次备份互相覆盖。
    """
    source = Path(db_file) if db_file is not None else default_db_path()
    if not source.exists():
        return None

    target_dir = Path(backup_dir) if backup_dir is not None else default_backups_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    stamp = f"{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}"
    target = target_dir / f"jitatrainer-{stamp}.db"
    counter = 1
    while target.exists():
        target = target_dir / f"jitatrainer-{stamp}-{counter}.db"
        counter += 1

    shutil.copy2(source, target)

    existing = sorted(target_dir.glob("jitatrainer-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in existing[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return target


__all__ = [
    "Database",
    "backup_database",
    "utc_now_iso",
    "LATEST_VERSION",
]
