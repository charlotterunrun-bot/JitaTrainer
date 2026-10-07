"""M4 统计与导出测试。

统计的时间口径是本轮最容易出错的地方：库里统一存 UTC，展示与"按天聚合"
必须按**本地日期**，否则跨时区边界会把练习记到错误的日子上。
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jitatrainer.core.theory.notes import NOTE_NAMES
from jitatrainer.data.analytics import AnalyticsRepository, local_date_of, parse_time
from jitatrainer.data.db import Database
from jitatrainer.data.export import (
    ATTEMPT_HEADERS,
    DAILY_HEADERS,
    export_attempts_csv,
    export_daily_csv,
)
from jitatrainer.data.repository import PracticeRepository, SessionRecorder

UTC = timezone.utc


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def add_session(
    db: Database,
    profile_id: int,
    *,
    started: datetime,
    minutes: float,
    attempts: list[tuple[int, str, int | None]],
    level: str = "L1",
    module_id: str = "pitch_find",
) -> int:
    """插入一次会话及其答题记录。``attempts`` 元素为 (音名序号, 结果, 反应时间)。"""
    ended = started + timedelta(minutes=minutes)
    correct_first = sum(1 for _pc, result, _rt in attempts if result == "correct")
    rts = [rt for _pc, result, rt in attempts if result == "correct" and rt]
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO sessions (profile_id, module_id, mode, target_value, started_at, ended_at, "
            "total, correct_first, correct_final, avg_rt_ms) VALUES (?, ?, 'count', ?, ?, ?, ?, ?, ?, ?)",
            (
                profile_id,
                module_id,
                len(attempts),
                iso(started),
                iso(ended),
                len(attempts),
                correct_first,
                correct_first,
                int(sum(rts) / len(rts)) if rts else None,
            ),
        )
        session_id = int(cursor.lastrowid)
        for pc, result, rt in attempts:
            conn.execute(
                "INSERT INTO attempts (session_id, question_json, source_tag, answered_at, result, "
                "attempt_index, rt_ms, detected_pitch_class) VALUES (?, ?, 'new', ?, ?, 1, ?, ?)",
                (
                    session_id,
                    json.dumps(
                        {
                            "item_key": f"note={NOTE_NAMES[pc]}|level={level}",
                            "target_pc": pc,
                            "level_id": level,
                            "string": 1,
                            "fret": 0,
                            "midi": 64,
                        },
                        ensure_ascii=False,
                    ),
                    iso(started),
                    result,
                    rt,
                    pc,
                ),
            )
        conn.commit()
    return session_id


@pytest.fixture
def database(tmp_path) -> Database:
    db = Database(tmp_path / "analytics.db")
    db.initialize()
    return db


@pytest.fixture
def profile_id(database: Database) -> int:
    with database.connect() as conn:
        return int(database.list_profiles(conn)[0]["id"])


class TestTimeHandling:
    def test_parse_utc_z(self) -> None:
        moment = parse_time("2026-10-07T13:00:00Z")
        assert moment == datetime(2026, 10, 7, 13, 0, tzinfo=UTC)

    def test_parse_naive_treated_as_utc(self) -> None:
        assert parse_time("2026-10-07T13:00:00") == datetime(2026, 10, 7, 13, 0, tzinfo=UTC)

    def test_parse_invalid(self) -> None:
        assert parse_time(None) is None
        assert parse_time("") is None
        assert parse_time("not-a-time") is None

    def test_local_date_uses_local_timezone(self) -> None:
        """UTC 与本地日期可能不同天，聚合必须用本地日期。"""
        moment = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)
        assert local_date_of(iso(moment)) == moment.astimezone().date()

    def test_recorder_writes_utc(self, database: Database, profile_id: int) -> None:
        """回归：答题时间必须与 sessions/items 一样存 UTC。

        早期版本答题时间存的是"本地裸时间"，会让按天统计在时区边界错位。
        """
        repository = PracticeRepository(database, profile_id)
        recorder = SessionRecorder(repository, module_id="pitch_find", mode="count", target_value=1)
        assert recorder.session_id is None
        recorder(fake_event("started"))

        from jitatrainer.practice.base import SOURCE_NEW, Question
        from jitatrainer.core.theory.fretboard import Position
        from jitatrainer.practice.session import EVENT_CORRECT, SessionEvent

        question = Question(
            module_id="pitch_find",
            item_key="note=E|level=L1",
            target_pc=4,
            position=Position(string=1, fret=0, midi=64),
            level_id="L1",
            source=SOURCE_NEW,
        )
        recorder(SessionEvent(kind=EVENT_CORRECT, question=question))

        with database.connect() as conn:
            row = conn.execute("SELECT answered_at FROM attempts").fetchone()
        assert row["answered_at"].endswith("Z"), f"答题时间应为 UTC：{row['answered_at']}"
        recorder.close()


def fake_event(kind: str):
    from jitatrainer.practice.session import SessionEvent

    return SessionEvent(kind=kind)


class TestDailySeries:
    def test_empty_database_returns_zero_filled_days(self, database: Database, profile_id: int) -> None:
        repository = AnalyticsRepository(database, profile_id)
        series = repository.daily_series(database.connect(), days=7)
        assert len(series) == 7
        assert all(point.answered == 0 for point in series)
        assert all(point.accuracy == 0.0 for point in series)

    def test_buckets_by_local_date(self, database: Database, profile_id: int) -> None:
        today = datetime.now().astimezone().date()
        started = datetime.now(UTC) - timedelta(minutes=30)
        add_session(
            database,
            profile_id,
            started=started,
            minutes=12.5,
            attempts=[(4, "correct", 1200), (7, "correct", 1500), (9, "wrong", None)],
        )
        with database.connect() as conn:
            series = AnalyticsRepository(database, profile_id).daily_series(conn, days=7, today=today)

        last = series[-1]
        assert last.day == started.astimezone().date()
        assert last.answered == 3
        assert last.correct_first == 2
        assert last.wrong == 1
        assert last.accuracy == pytest.approx(2 / 3, abs=1e-4)
        assert last.avg_rt_ms == 1350
        assert last.minutes == pytest.approx(12.5, abs=0.1)

    def test_old_records_are_excluded(self, database: Database, profile_id: int) -> None:
        old = datetime.now(UTC) - timedelta(days=40)
        add_session(database, profile_id, started=old, minutes=5, attempts=[(4, "correct", 1000)])
        with database.connect() as conn:
            series = AnalyticsRepository(database, profile_id).daily_series(conn, days=7)
        assert sum(point.answered for point in series) == 0

    def test_minutes_sum_multiple_sessions(self, database: Database, profile_id: int) -> None:
        now = datetime.now(UTC) - timedelta(hours=1)
        for minutes in (5.0, 7.5):
            add_session(
                database, profile_id, started=now, minutes=minutes, attempts=[(4, "correct", 900)]
            )
        with database.connect() as conn:
            series = AnalyticsRepository(database, profile_id).daily_series(conn, days=3)
        assert series[-1].minutes == pytest.approx(12.5, abs=0.2)


class TestNoteCells:
    def test_aggregates_by_item(self, database: Database, profile_id: int) -> None:
        now = datetime.now(UTC) - timedelta(hours=2)
        add_session(
            database,
            profile_id,
            started=now,
            minutes=3,
            attempts=[(4, "correct", 1000), (4, "wrong", None), (4, "correct", 2000)],
        )
        add_session(
            database, profile_id, started=now, minutes=3, attempts=[(7, "correct", 800)]
        )
        with database.connect() as conn:
            cells = AnalyticsRepository(database, profile_id).note_cells(conn)

        by_pc = {cell.pitch_class: cell for cell in cells}
        assert set(by_pc) == {4, 7}
        e_cell = by_pc[4]
        assert e_cell.attempts == 3
        assert e_cell.wrong == 1
        assert e_cell.error_rate == pytest.approx(1 / 3, abs=1e-4)
        assert e_cell.avg_rt_ms == 1500
        assert e_cell.item_key == "note=E|level=L1"

    def test_filter_by_level(self, database: Database, profile_id: int) -> None:
        now = datetime.now(UTC) - timedelta(hours=1)
        add_session(database, profile_id, started=now, minutes=2, attempts=[(4, "wrong", None)], level="L1")
        add_session(database, profile_id, started=now, minutes=2, attempts=[(4, "wrong", None)], level="L2")
        with database.connect() as conn:
            repository = AnalyticsRepository(database, profile_id)
            assert len(repository.note_cells(conn, level_id="L1")) == 1
            assert len(repository.note_cells(conn)) == 2

    def test_weak_notes_sorted_by_error_rate(self, database: Database, profile_id: int) -> None:
        now = datetime.now(UTC) - timedelta(hours=1)
        add_session(
            database,
            profile_id,
            started=now,
            minutes=3,
            attempts=[(4, "wrong", None), (4, "wrong", None)],  # 错误率 100%
        )
        add_session(
            database,
            profile_id,
            started=now,
            minutes=3,
            attempts=[(7, "wrong", None), (7, "correct", 1000), (7, "correct", 1000)],  # 33%
        )
        with database.connect() as conn:
            weak = AnalyticsRepository(database, profile_id).weak_notes(conn, limit=5)
        assert [cell.pitch_class for cell in weak] == [4, 7]

    def test_weak_notes_skip_perfect_items(self, database: Database, profile_id: int) -> None:
        now = datetime.now(UTC) - timedelta(hours=1)
        add_session(database, profile_id, started=now, minutes=2, attempts=[(4, "correct", 900)])
        with database.connect() as conn:
            assert AnalyticsRepository(database, profile_id).weak_notes(conn) == []

    def test_wrong_note_ranking(self, database: Database, profile_id: int) -> None:
        now = datetime.now(UTC) - timedelta(hours=1)
        add_session(
            database,
            profile_id,
            started=now,
            minutes=3,
            attempts=[(4, "wrong", None), (4, "wrong", None), (7, "wrong", None), (9, "correct", 900)],
        )
        with database.connect() as conn:
            ranking = AnalyticsRepository(database, profile_id).wrong_note_ranking(conn)
        assert [pc for pc, _wrong, _rate in ranking] == [4, 7, 9]
        assert ranking[0][1] == 2
        assert ranking[2][1] == 0


class TestPeriods:
    def test_compare_current_vs_previous(self, database: Database, profile_id: int) -> None:
        today = datetime.now().astimezone().date()
        now = datetime.now(UTC)
        # 最近 3 天：2 题答对
        add_session(
            database, profile_id, started=now - timedelta(days=1), minutes=6,
            attempts=[(4, "correct", 1000), (7, "correct", 1200)],
        )
        # 前 3 天：4 题里错 2
        add_session(
            database, profile_id, started=now - timedelta(days=5), minutes=10,
            attempts=[(4, "wrong", None), (7, "wrong", None), (9, "correct", 900), (11, "correct", 900)],
        )
        with database.connect() as conn:
            comparison = AnalyticsRepository(database, profile_id).compare(conn, days=3, today=today)

        assert comparison.current.answered == 2
        assert comparison.current.accuracy == pytest.approx(1.0)
        assert comparison.previous.answered == 4
        assert comparison.previous.accuracy == pytest.approx(0.5)
        assert comparison.answer_delta == -2
        assert comparison.accuracy_delta == pytest.approx(0.5, abs=1e-4)
        assert comparison.current.minutes == pytest.approx(6.0, abs=0.2)
        assert comparison.previous.minutes == pytest.approx(10.0, abs=0.2)

    def test_compare_with_no_data(self, database: Database, profile_id: int) -> None:
        with database.connect() as conn:
            comparison = AnalyticsRepository(database, profile_id).compare(conn, days=7)
        assert comparison.current.answered == 0
        assert comparison.answer_delta == 0
        assert comparison.rt_delta_ms is None

    def test_overall_includes_everything(self, database: Database, profile_id: int) -> None:
        now = datetime.now(UTC)
        add_session(database, profile_id, started=now - timedelta(days=100), minutes=4,
                    attempts=[(4, "correct", 1000)])
        add_session(database, profile_id, started=now, minutes=4, attempts=[(7, "wrong", None)])
        with database.connect() as conn:
            overall = AnalyticsRepository(database, profile_id).overall(conn)
        assert overall.answered == 2
        assert overall.correct_first == 1
        assert overall.minutes == pytest.approx(8.0, abs=0.3)


class TestExport:
    def test_attempts_csv_headers_and_rows(self, database: Database, profile_id: int, tmp_path) -> None:
        now = datetime.now(UTC) - timedelta(hours=1)
        add_session(
            database, profile_id, started=now, minutes=3,
            attempts=[(4, "correct", 1234), (7, "wrong", None)],
        )
        target = tmp_path / "attempts.csv"
        result = export_attempts_csv(database, profile_id, target)

        assert result.rows == 2
        assert target.exists()
        raw = target.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), "应带 UTF-8 BOM，Excel 打开中文才不乱码"

        with target.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        assert tuple(rows[0]) == ATTEMPT_HEADERS
        assert len(rows) == 3
        assert rows[1][1] == "note=E|level=L1"
        assert rows[1][2] == "E"
        assert rows[1][5] == "correct"
        assert rows[1][7] == "1234"
        assert rows[2][5] == "wrong"
        assert rows[2][7] == ""

    def test_daily_csv(self, database: Database, profile_id: int, tmp_path) -> None:
        now = datetime.now(UTC) - timedelta(minutes=20)
        add_session(
            database, profile_id, started=now, minutes=18,
            attempts=[(4, "correct", 1000), (7, "wrong", None)],
        )
        target = tmp_path / "daily.csv"
        result = export_daily_csv(database, profile_id, target, days=7)

        assert result.rows == 7
        with target.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        assert tuple(rows[0]) == DAILY_HEADERS
        last = rows[-1]
        assert last[1] == "2"  # 题量
        assert last[2] == "1"  # 首次正确
        assert last[4] == "50.0%"
        assert float(last[6]) == pytest.approx(18.0, abs=0.2)

    def test_export_creates_parent_directory(self, database: Database, profile_id: int, tmp_path) -> None:
        target = tmp_path / "nested" / "deep" / "attempts.csv"
        export_attempts_csv(database, profile_id, target)
        assert target.exists()

    def test_export_empty_database(self, database: Database, profile_id: int, tmp_path) -> None:
        target = tmp_path / "empty.csv"
        result = export_attempts_csv(database, profile_id, target)
        assert result.rows == 0
        with target.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        assert len(rows) == 1  # 只有表头


class TestAnalyticsPageData:
    """统计页使用的数据组合不会抛异常（页面本身由 UI 冒烟测试覆盖）。"""

    def test_all_queries_on_empty_db(self, database: Database, profile_id: int) -> None:
        repository = AnalyticsRepository(database, profile_id)
        with database.connect() as conn:
            assert repository.daily_series(conn, days=30)
            assert repository.note_cells(conn) == []
            assert repository.weak_notes(conn) == []
            assert repository.wrong_note_ranking(conn) == []
            assert repository.overall(conn).answered == 0
            comparison = repository.compare(conn, days=30)
            assert comparison.current.answered == 0
