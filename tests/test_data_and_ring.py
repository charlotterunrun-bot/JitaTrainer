"""环形缓冲与数据层测试。"""

from __future__ import annotations

import numpy as np
import pytest

from jitatrainer.core.audio.ring import RingBuffer
from jitatrainer.data.db import Database, backup_database, list_backups, utc_now_iso
from jitatrainer.data.migrations import LATEST_VERSION


class TestRingBuffer:
    def test_insufficient_data_returns_none(self) -> None:
        ring = RingBuffer(capacity=1024)
        ring.write(np.ones(100, dtype=np.float32))
        assert ring.read_latest(200) is None
        assert ring.read_latest(100) is not None

    def test_latest_window_is_most_recent(self) -> None:
        ring = RingBuffer(capacity=16)
        ring.write(np.arange(10, dtype=np.float32))
        window = ring.read_latest(4)
        assert window is not None
        assert list(window[:, 0]) == [6, 7, 8, 9]

    def test_wraparound(self) -> None:
        ring = RingBuffer(capacity=8)
        ring.write(np.arange(12, dtype=np.float32))
        window = ring.read_latest(8)
        assert window is not None
        assert list(window[:, 0]) == [4, 5, 6, 7, 8, 9, 10, 11]

    def test_block_larger_than_capacity_keeps_tail(self) -> None:
        ring = RingBuffer(capacity=4)
        ring.write(np.arange(10, dtype=np.float32))
        assert ring.overrun_count >= 1
        window = ring.read_latest(4)
        assert window is not None
        assert list(window[:, 0]) == [6, 7, 8, 9]

    def test_stereo_supported(self) -> None:
        ring = RingBuffer(capacity=16, channels=2)
        ring.write(np.ones((10, 2), dtype=np.float32))
        window = ring.read_latest(5)
        assert window is not None and window.shape == (5, 2)

    def test_invalid_arguments(self) -> None:
        with pytest.raises(ValueError):
            RingBuffer(capacity=0)
        ring = RingBuffer(capacity=8)
        with pytest.raises(ValueError):
            ring.read_latest(0)
        with pytest.raises(ValueError):
            ring.read_latest(9)


class TestDatabase:
    def test_initialize_creates_default_profile(self, tmp_path) -> None:
        db = Database(tmp_path / "test.db")
        version = db.initialize()
        assert version == LATEST_VERSION
        with db.connect() as conn:
            profiles = db.list_profiles(conn)
            assert len(profiles) == 1
            assert profiles[0]["name"] == "默认档案"

    def test_migrations_are_idempotent(self, tmp_path) -> None:
        db = Database(tmp_path / "test.db")
        first = db.initialize()
        second = db.initialize()
        assert first == second == LATEST_VERSION
        with db.connect() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()["c"]
            assert count == LATEST_VERSION

    def test_profiles_crud(self, tmp_path) -> None:
        db = Database(tmp_path / "test.db")
        db.initialize()
        with db.connect() as conn:
            pid = db.create_profile(conn, "小美", "#ff8800")
            assert len(db.list_profiles(conn)) == 2
            db.rename_profile(conn, pid, "小美（新）")
            assert db.list_profiles(conn)[1]["name"] == "小美（新）"
            db.delete_profile(conn, pid)
            assert len(db.list_profiles(conn)) == 1

    def test_profile_name_is_unique(self, tmp_path) -> None:
        import sqlite3

        db = Database(tmp_path / "test.db")
        db.initialize()
        with db.connect() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                db.create_profile(conn, "默认档案")

    def test_settings_roundtrip(self, tmp_path) -> None:
        db = Database(tmp_path / "test.db")
        db.initialize()
        with db.connect() as conn:
            pid = db.list_profiles(conn)[0]["id"]
            db.set_app_setting(conn, "language", "en_US")
            db.set_profile_setting(conn, pid, "stable_preset", "fast")
            db.set_app_setting(conn, "language", "zh_CN")  # 覆盖写
            assert db.get_app_setting(conn, "language") == "zh_CN"
            assert db.get_profile_setting(conn, pid, "stable_preset") == "fast"
            assert db.get_app_setting(conn, "missing", "default") == "default"

    def test_item_upsert_is_idempotent(self, tmp_path) -> None:
        db = Database(tmp_path / "test.db")
        db.initialize()
        with db.connect() as conn:
            pid = db.list_profiles(conn)[0]["id"]
            kwargs = dict(
                profile_id=pid,
                module_id="pitch_find",
                item_key="note=B|level=L2",
                pitch_class=11,
                level_id="L2",
            )
            first = db.upsert_item(conn, **kwargs)
            second = db.upsert_item(conn, **kwargs)
            assert first == second
            item = db.get_item(conn, pid, "pitch_find", "note=B|level=L2")
            assert item is not None and item["pitch_class"] == 11

    def test_due_items_query(self, tmp_path) -> None:
        db = Database(tmp_path / "test.db")
        db.initialize()
        with db.connect() as conn:
            pid = db.list_profiles(conn)[0]["id"]
            item_id = db.upsert_item(
                conn,
                profile_id=pid,
                module_id="pitch_find",
                item_key="note=E|level=L1",
                pitch_class=4,
                level_id="L1",
            )
            assert db.due_items(conn, pid, "pitch_find", now_iso="2100-01-01T00:00:00Z") == []
            conn.execute("UPDATE items SET due_at = ? WHERE id = ?", ("2000-01-01T00:00:00Z", item_id))
            conn.commit()
            due = db.due_items(conn, pid, "pitch_find", now_iso="2026-01-01T00:00:00Z")
            assert len(due) == 1 and due[0]["item_key"] == "note=E|level=L1"

    def test_event_logging(self, tmp_path) -> None:
        db = Database(tmp_path / "test.db")
        db.initialize()
        with db.connect() as conn:
            db.log_event(conn, "info", "会话结束", {"total": 30})
            row = conn.execute("SELECT * FROM app_events").fetchone()
            assert row["message"] == "会话结束"
            assert '"total": 30' in row["context_json"]

    def test_backup_keeps_latest_n(self, tmp_path) -> None:
        """同一秒内连续备份也不能互相覆盖，且只保留最近 N 份。"""
        db_file = tmp_path / "jitatrainer.db"
        db = Database(db_file)
        db.initialize()
        backup_dir = tmp_path / "backups"
        for _ in range(3):
            assert backup_database(db_file, backup_dir, keep=2) is not None
            import time

            time.sleep(0.01)
        remaining = sorted(p.name for p in backup_dir.glob("jitatrainer-*.db"))
        assert len(remaining) == 2, f"应保留 2 份，实际 {remaining}"

    def test_backup_names_are_unique_within_same_second(self, tmp_path) -> None:
        db_file = tmp_path / "jitatrainer.db"
        db = Database(db_file)
        db.initialize()
        backup_dir = tmp_path / "backups"
        names = {backup_database(db_file, backup_dir, keep=99).name for _ in range(5)}
        assert len(names) == 5, f"同一秒内备份文件名重复：{names}"

    def test_backup_missing_db_returns_none(self, tmp_path) -> None:
        assert backup_database(tmp_path / "nope.db", tmp_path / "backups") is None

    def test_backup_captures_wal_data_not_yet_checkpointed(self, tmp_path) -> None:
        """回归：数据库开了 WAL，备份必须包含还在 -wal 里的最新数据。

        早期实现用 shutil.copy2 直接拷主库文件，会丢掉尚未 checkpoint 的
        事务——而"刚练完就自动备份"恰恰是最需要保住这些数据的时刻。
        """
        import sqlite3 as sqlite

        db = Database(tmp_path / "wal.db")
        db.initialize()
        conn = db.connect()
        profile_id = db.list_profiles(conn)[0]["id"]
        conn.execute(
            "INSERT INTO sessions (profile_id, module_id, mode, started_at) "
            "VALUES (?, 'pitch_find', 'count', '2026-10-07T10:00:00Z')",
            (profile_id,),
        )
        conn.commit()
        # 刻意不执行 wal_checkpoint：数据仍在 -wal 里
        wal_path = tmp_path / "wal.db-wal"
        assert wal_path.exists() and wal_path.stat().st_size > 0, "测试前提：数据确实在 WAL 里"
        conn.close()

        target = backup_database(tmp_path / "wal.db", tmp_path / "backups", keep=5)
        assert target is not None

        restored = sqlite.connect(str(target))
        try:
            count = restored.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        finally:
            restored.close()
        assert count == 1, "备份里少了还在 WAL 中的会话记录"

    def test_backup_reason_appears_in_filename(self, tmp_path) -> None:
        db = Database(tmp_path / "r.db")
        db.initialize()
        target = backup_database(db.path, tmp_path / "backups", reason="session")
        assert target is not None
        assert "session" in target.name

    def test_list_backups_newest_first(self, tmp_path) -> None:
        db = Database(tmp_path / "l.db")
        db.initialize()
        backup_dir = tmp_path / "backups"
        for _ in range(3):
            backup_database(db.path, backup_dir, keep=10)
        listed = list_backups(backup_dir)
        assert len(listed) == 3
        mtimes = [p.stat().st_mtime for p in listed]
        assert mtimes == sorted(mtimes, reverse=True)

    def test_list_backups_missing_dir(self, tmp_path) -> None:
        assert list_backups(tmp_path / "none") == []

    def test_utc_now_iso_format(self) -> None:
        stamp = utc_now_iso()
        assert stamp.endswith("Z") and "T" in stamp
