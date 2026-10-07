"""练习数据持久化测试：会话与答题写入数据库。"""

from __future__ import annotations

import json
import random

import pytest

from jitatrainer.core.audio.events import PitchEvent
from jitatrainer.core.theory.notes import midi_to_hz
from jitatrainer.data.db import Database
from jitatrainer.data.repository import PracticeRepository, SessionRecorder
from jitatrainer.practice.pitch_find.module import PitchFindModule
from jitatrainer.practice.session import MODE_COUNT, MODE_DURATION, PracticeSession, SessionConfig


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def fast_judge_config(question, **_ignored: object):
    from jitatrainer.core.judge.pitch_class_judge import JudgeConfig

    return JudgeConfig(
        target_pc=question.target_pc,
        stable_ms=100,
        grace_ms=100,
        timeout_ms=5000,
        mode="grace",
        hop_ms=10.0,
    )


FRAMES = 340


def run_session(db: Database, profile_id: int, *, count: int = 3, answer_correctly: bool = True):
    session = PracticeSession(
        PitchFindModule(random.Random(7)),
        SessionConfig(mode=MODE_COUNT, target_count=count, level_id="L1"),
        fast_judge_config,
        profile_id=profile_id,
        clock=FakeClock(),
    )
    recorder = SessionRecorder(
        PracticeRepository(db, profile_id), module_id="pitch_find", mode=MODE_COUNT, target_value=count
    )
    session.observer = recorder

    session.start()
    for index in range(count):
        question = session.current_question
        if question is None:
            break
        pc = question.target_pc if answer_correctly else (question.target_pc + 5) % 12
        hz = midi_to_hz(60 + pc)
        for frame in range(FRAMES):
            for _event in session.feed(
                PitchEvent(t=index * 4.0 + frame * 0.01, hz=hz, confidence=0.95, is_onset=(frame == 0))
            ):
                pass
    recorder.close()
    return session, recorder


@pytest.fixture
def database(tmp_path) -> Database:
    db = Database(tmp_path / "practice.db")
    db.initialize()
    return db


class TestRepository:
    def test_start_and_finish_session(self, database: Database) -> None:
        with database.connect() as conn:
            profile_id = database.list_profiles(conn)[0]["id"]
            repository = PracticeRepository(database, profile_id)
            session_id = repository.start_session(
                conn, module_id="pitch_find", mode=MODE_COUNT, target_value=30
            )
            assert session_id > 0

            session, recorder = run_session(database, profile_id, count=2)
            repository.finish_session(conn, session_id, session.build_summary())

            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            assert row["ended_at"] is not None
            assert row["total"] == 2
            assert row["correct_first"] == 2

    def test_recent_sessions_and_attempts(self, database: Database) -> None:
        with database.connect() as conn:
            profile_id = database.list_profiles(conn)[0]["id"]
        _session, recorder = run_session(database, profile_id, count=3)
        assert recorder.session_id is not None

        with database.connect() as conn:
            repository = PracticeRepository(database, profile_id)
            sessions = repository.recent_sessions(conn)
            assert len(sessions) == 1
            assert sessions[0]["total"] == 3
            assert sessions[0]["score"] > 0

            attempts = repository.session_attempts(conn, recorder.session_id)
            assert len(attempts) == 3
            assert all(row["result"] == "correct" for row in attempts)
            assert all(row["rt_ms"] is not None for row in attempts)
            question = json.loads(attempts[0]["question_json"])
            assert question["level_id"] == "L1"
            assert "fret" in question and "string" in question


class TestRecorder:
    def test_records_wrong_answer(self, database: Database) -> None:
        with database.connect() as conn:
            profile_id = database.list_profiles(conn)[0]["id"]
        _session, recorder = run_session(database, profile_id, count=1, answer_correctly=False)
        assert recorder.session_id is not None

        with database.connect() as conn:
            rows = PracticeRepository(database, profile_id).session_attempts(conn, recorder.session_id)
        assert rows, "答错的尝试也必须入库"
        assert rows[0]["result"] == "wrong"
        assert rows[0]["detected_pitch_class"] is not None

    def test_records_skip(self, database: Database) -> None:
        with database.connect() as conn:
            profile_id = database.list_profiles(conn)[0]["id"]
        repository = PracticeRepository(database, profile_id)
        recorder = SessionRecorder(
            repository, module_id="pitch_find", mode=MODE_COUNT, target_value=2
        )
        session = PracticeSession(
            PitchFindModule(random.Random(1)),
            SessionConfig(mode=MODE_COUNT, target_count=2),
            fast_judge_config,
            profile_id=profile_id,
            clock=FakeClock(),
        )
        session.observer = recorder
        session.start()
        session.skip()
        session.finish()
        recorder.close()

        with database.connect() as conn:
            rows = repository.session_attempts(conn, recorder.session_id)
            session_row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (recorder.session_id,)
            ).fetchone()
        assert rows[0]["result"] == "skipped"
        assert session_row["skipped"] == 1

    def test_observer_errors_do_not_break_practice(self, database: Database) -> None:
        """观察者抛异常时练习必须继续（数据库故障不能中断训练）。"""

        def broken_observer(event) -> None:  # noqa: ANN001
            raise RuntimeError("数据库炸了")

        session = PracticeSession(
            PitchFindModule(random.Random(2)),
            SessionConfig(mode=MODE_COUNT, target_count=1),
            fast_judge_config,
            clock=FakeClock(),
        )
        session.observer = broken_observer
        session.start()
        question = session.current_question
        assert question is not None
        hz = midi_to_hz(60 + question.target_pc)
        for frame in range(FRAMES):
            session.feed(PitchEvent(t=frame * 0.01, hz=hz, confidence=0.95, is_onset=(frame == 0)))
        assert session.is_finished
        assert session.build_summary().asked == 1

    def test_recorder_error_is_captured(self, database: Database) -> None:
        """数据库异常要记录在 recorder.error 里，但不能抛出去。"""
        with database.connect() as conn:
            profile_id = database.list_profiles(conn)[0]["id"]
        repository = PracticeRepository(database, profile_id)
        recorder = SessionRecorder(repository, module_id="pitch_find", mode=MODE_COUNT, target_value=1)
        recorder.conn.close()  # 让后续写入失败

        session = PracticeSession(
            PitchFindModule(random.Random(3)),
            SessionConfig(mode=MODE_COUNT, target_count=1),
            fast_judge_config,
            clock=FakeClock(),
        )
        session.observer = recorder
        session.start()
        question = session.current_question
        assert question is not None
        hz = midi_to_hz(60 + question.target_pc)
        for frame in range(FRAMES):
            session.feed(PitchEvent(t=frame * 0.01, hz=hz, confidence=0.95, is_onset=(frame == 0)))
        assert session.is_finished, "记录失败不应中断练习"
        assert recorder.error is not None


class TestDurationSessionPersistence:
    def test_duration_mode_writes_target_value(self, database: Database) -> None:
        with database.connect() as conn:
            profile_id = database.list_profiles(conn)[0]["id"]
        repository = PracticeRepository(database, profile_id)
        recorder = SessionRecorder(
            repository, module_id="pitch_find", mode=MODE_DURATION, target_value=5
        )
        clock = FakeClock()
        session = PracticeSession(
            PitchFindModule(random.Random(4)),
            SessionConfig(mode=MODE_DURATION, target_minutes=5),
            fast_judge_config,
            clock=clock,
        )
        session.observer = recorder
        session.start()
        clock.now = 301.0
        session.tick()
        recorder.close()

        with database.connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (recorder.session_id,)).fetchone()
        assert row["mode"] == MODE_DURATION
        assert row["target_value"] == 5
        assert row["ended_at"] is not None
