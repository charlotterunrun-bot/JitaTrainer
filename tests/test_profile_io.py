"""档案导入导出测试（FR-105 / FR-1000）。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from jitatrainer.data.db import Database
from jitatrainer.data.profile_io import (
    EXPORT_FORMAT,
    EXPORT_VERSION,
    ProfileFormatError,
    export_profile,
    import_profile,
    read_profile_file,
    suggest_export_name,
)

UTC = timezone.utc


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@pytest.fixture
def database(tmp_path) -> Database:
    db = Database(tmp_path / "profiles.db")
    db.initialize()
    return db


@pytest.fixture
def populated(database: Database) -> int:
    """造一个有训练项、会话、答题、设置的档案。"""
    with database.connect() as conn:
        profile_id = int(database.list_profiles(conn)[0]["id"])
        database.rename_profile(conn, profile_id, "小明")
        database.set_profile_setting(conn, profile_id, "level_id", "L2")
        database.upsert_item(
            conn,
            profile_id=profile_id,
            module_id="pitch_find",
            item_key="note=E|level=L2",
            pitch_class=4,
            level_id="L2",
        )
        # 训练项状态由调度层更新，这里直接写完整个状态
        conn.execute(
            "UPDATE items SET interval_index = 3, due_at = '2026-10-09T00:00:00Z', seen_count = 5, "
            "correct_count = 4, error_rate = 0.2, avg_rt_ms = 1350 "
            "WHERE profile_id = ? AND item_key = 'note=E|level=L2'",
            (profile_id,),
        )
        cursor = conn.execute(
            "INSERT INTO sessions (profile_id, module_id, mode, target_value, started_at, ended_at, "
            "total, correct_first, correct_final, wrong, skipped, timeout, avg_rt_ms, best_combo, score) "
            "VALUES (?, 'pitch_find', 'count', 20, ?, ?, 20, 17, 19, 3, 1, 0, 1400, 6, 812)",
            (profile_id, iso(datetime(2026, 10, 7, 10, 0, tzinfo=UTC)), iso(datetime(2026, 10, 7, 10, 15, tzinfo=UTC))),
        )
        session_id = int(cursor.lastrowid)
        for index, (result, rt) in enumerate((("correct", 1200), ("wrong", None), ("correct", 1500))):
            conn.execute(
                "INSERT INTO attempts (session_id, question_json, source_tag, answered_at, result, "
                "attempt_index, rt_ms, detected_pitch_class, detected_hz, cents_offset) "
                "VALUES (?, ?, 'new', ?, ?, 1, ?, 4, 82.41, -12.5)",
                (
                    session_id,
                    json.dumps({"item_key": "note=E|level=L2", "target_pc": 4, "level_id": "L2"}),
                    iso(datetime(2026, 10, 7, 10, index, tzinfo=UTC)),
                    result,
                    rt,
                ),
            )
        conn.commit()
    return profile_id


class TestExport:
    def test_file_has_expected_structure(self, database: Database, populated: int, tmp_path) -> None:
        target = tmp_path / "profile.json"
        summary = export_profile(database, populated, target)

        assert summary.items == 1
        assert summary.sessions == 1
        assert summary.attempts == 3
        assert summary.profile_name == "小明"
        assert target.exists()

        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["format"] == EXPORT_FORMAT
        assert payload["version"] == EXPORT_VERSION
        assert payload["profile"]["name"] == "小明"
        assert payload["settings"]["level_id"] == "L2"
        assert payload["attempts"][0]["session_index"] == 0

    def test_explicit_columns_only(self, database: Database, populated: int, tmp_path) -> None:
        """导出不该带 id 之类的内部字段（否则导入会撞 id）。"""
        target = tmp_path / "profile.json"
        export_profile(database, populated, target)
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert "id" not in payload["sessions"][0]
        assert "id" not in payload["items"][0]
        assert "session_id" not in payload["attempts"][0]

    def test_missing_profile_raises(self, database: Database, tmp_path) -> None:
        with pytest.raises(ProfileFormatError):
            export_profile(database, 9999, tmp_path / "x.json")

    def test_creates_parent_directory(self, database: Database, populated: int, tmp_path) -> None:
        target = tmp_path / "deep" / "nested" / "p.json"
        export_profile(database, populated, target)
        assert target.exists()


class TestImport:
    def test_round_trip_into_new_profile(self, database: Database, populated: int, tmp_path) -> None:
        target = tmp_path / "profile.json"
        export_profile(database, populated, target)
        summary = import_profile(database, target, mode="new")

        assert summary.mode == "new"
        assert summary.items == 1
        assert summary.sessions == 1
        assert summary.attempts == 3
        assert summary.profile_id != populated

        with database.connect() as conn:
            assert len(database.list_profiles(conn)) == 2
            row = conn.execute(
                "SELECT * FROM items WHERE profile_id = ?", (summary.profile_id,)
            ).fetchone()
            assert row["item_key"] == "note=E|level=L2"
            assert row["interval_index"] == 3
            assert row["due_at"] == "2026-10-09T00:00:00Z"
            assert row["avg_rt_ms"] == 1350

            session = conn.execute(
                "SELECT * FROM sessions WHERE profile_id = ?", (summary.profile_id,)
            ).fetchone()
            assert session["score"] == 812
            assert session["best_combo"] == 6

            attempts = conn.execute(
                "SELECT * FROM attempts WHERE session_id = ? ORDER BY id", (session["id"],)
            ).fetchall()
            assert len(attempts) == 3
            assert [a["result"] for a in attempts] == ["correct", "wrong", "correct"]
            assert attempts[0]["cents_offset"] == -12.5

    def test_settings_are_restored(self, database: Database, populated: int, tmp_path) -> None:
        target = tmp_path / "p.json"
        export_profile(database, populated, target)
        summary = import_profile(database, target, mode="new")
        with database.connect() as conn:
            value = database.get_profile_setting(conn, summary.profile_id, "level_id")
        assert value == "L2"

    def test_name_collision_gets_suffix(self, database: Database, populated: int, tmp_path) -> None:
        target = tmp_path / "p.json"
        export_profile(database, populated, target)
        first = import_profile(database, target, mode="new")
        second = import_profile(database, target, mode="new")
        assert first.profile_name == "小明 (2)"
        assert second.profile_name == "小明 (3)"

    def test_original_profile_untouched(self, database: Database, populated: int, tmp_path) -> None:
        target = tmp_path / "p.json"
        export_profile(database, populated, target)
        import_profile(database, target, mode="new")
        with database.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM attempts WHERE session_id IN "
                "(SELECT id FROM sessions WHERE profile_id = ?)",
                (populated,),
            ).fetchone()["c"]
        assert count == 3

    def test_overwrite_replaces_target_data(self, database: Database, populated: int, tmp_path) -> None:
        target = tmp_path / "p.json"
        export_profile(database, populated, target)

        # 再往原档案里加一条会话，然后覆盖导入，应回到导出时的状态
        with database.connect() as conn:
            conn.execute(
                "INSERT INTO sessions (profile_id, module_id, mode, started_at) "
                "VALUES (?, 'pitch_find', 'count', '2026-10-08T10:00:00Z')",
                (populated,),
            )
            conn.commit()
            assert conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"] == 2

        summary = import_profile(database, target, mode="overwrite", target_profile_id=populated)
        assert summary.profile_id == populated
        with database.connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"] == 1
            assert len(database.list_profiles(conn)) == 1, "覆盖导入不应新建档案"

    def test_overwrite_requires_target(self, database: Database, populated: int, tmp_path) -> None:
        target = tmp_path / "p.json"
        export_profile(database, populated, target)
        with pytest.raises(ValueError):
            import_profile(database, target, mode="overwrite")

    def test_unknown_mode_rejected(self, database: Database, populated: int, tmp_path) -> None:
        target = tmp_path / "p.json"
        export_profile(database, populated, target)
        with pytest.raises(ValueError):
            import_profile(database, target, mode="merge")


class TestFormatValidation:
    def test_rejects_non_json(self, database: Database, tmp_path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("这不是 JSON", encoding="utf-8")
        with pytest.raises(ProfileFormatError):
            read_profile_file(bad)

    def test_rejects_other_json(self, database: Database, tmp_path) -> None:
        bad = tmp_path / "other.json"
        bad.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
        with pytest.raises(ProfileFormatError):
            read_profile_file(bad)

    def test_rejects_future_version(self, database: Database, tmp_path) -> None:
        bad = tmp_path / "future.json"
        bad.write_text(
            json.dumps({"format": EXPORT_FORMAT, "version": EXPORT_VERSION + 1}), encoding="utf-8"
        )
        with pytest.raises(ProfileFormatError) as excinfo:
            read_profile_file(bad)
        assert "版本" in str(excinfo.value)

    def test_rejects_missing_file(self, database: Database, tmp_path) -> None:
        with pytest.raises(ProfileFormatError):
            read_profile_file(tmp_path / "nope.json")

    def test_import_failure_rolls_back_everything(self, database: Database, tmp_path) -> None:
        """导入中途失败必须整体回滚：不能留下半个档案。"""
        payload = {
            "format": EXPORT_FORMAT,
            "version": EXPORT_VERSION,
            "profile": {"name": "坏档案", "color": "#fff", "created_at": "2026-10-07T00:00:00Z"},
            "settings": {"level_id": "L1"},
            "items": [],
            # started_at 是 NOT NULL，导入到一半必然失败
            "sessions": [{"module_id": "pitch_find", "mode": "count", "started_at": None}],
            "attempts": [],
        }
        path = tmp_path / "broken.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        before = _profile_names(database)
        with pytest.raises(ProfileFormatError) as excinfo:
            import_profile(database, path, mode="new")
        assert "缺少必要字段" in str(excinfo.value)

        after = _profile_names(database)
        assert after == before, f"失败后残留了半个档案：{after}"
        with database.connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"] == 0
            assert (
                conn.execute("SELECT COUNT(*) AS c FROM profile_settings").fetchone()["c"] == 0
            ), "设置也不该残留"

    def test_attempt_with_unknown_session_is_skipped(self, database: Database, tmp_path) -> None:
        """答题记录指向不存在的会话时跳过该条，而不是写坏数据。"""
        complete_session = {
            "module_id": "pitch_find",
            "mode": "count",
            "target_value": 10,
            "started_at": "2026-10-07T10:00:00Z",
            "ended_at": "2026-10-07T10:10:00Z",
            "total": 2,
            "correct_first": 1,
            "correct_final": 1,
            "wrong": 1,
            "skipped": 0,
            "timeout": 0,
            "avg_rt_ms": 1200,
            "best_combo": 1,
            "score": 100,
        }
        payload = {
            "format": EXPORT_FORMAT,
            "version": EXPORT_VERSION,
            "profile": {"name": "含孤儿答题", "color": "#fff", "created_at": "2026-10-07T00:00:00Z"},
            "settings": {},
            "items": [],
            "sessions": [complete_session],
            "attempts": [
                {
                    "session_index": 0,
                    "result": "correct",
                    "source_tag": "new",
                    "question_json": '{"target_pc": 4}',
                    "answered_at": "2026-10-07T10:01:00Z",
                },
                {
                    "session_index": 42,
                    "result": "wrong",
                    "source_tag": "new",
                    "question_json": '{"target_pc": 7}',
                    "answered_at": "2026-10-07T10:02:00Z",
                },
            ],
        }
        path = tmp_path / "orphan.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        summary = import_profile(database, path, mode="new")
        assert summary.attempts == 1


def _profile_names(database: Database) -> list[str]:
    with database.connect() as conn:
        return [row["name"] for row in database.list_profiles(conn)]


class TestExportName:
    def test_strips_illegal_characters(self) -> None:
        name = suggest_export_name('a/b\\c:d*e?f"g<h>i|j')
        assert all(ch not in name for ch in '\\/:*?"<>|')
        assert name.startswith("JitaTrainer-")
        assert name.endswith(".json")

    def test_empty_name_falls_back(self) -> None:
        assert "profile" in suggest_export_name("   ")
