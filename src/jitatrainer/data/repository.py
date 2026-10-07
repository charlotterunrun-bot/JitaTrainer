"""练习数据的持久化。

把会话与每次答题写入 ``sessions`` / ``attempts`` 表（DDL 见 M1 的迁移 v1）。
训练项熟练度（``items``）由 M3 的记忆曲线负责更新。

设计：``SessionRecorder`` 是一个"会话事件观察者"，挂在 ``PracticeSession`` 上。
它跑在 UI 线程（不是音频线程），因此逐条写入即可；SQLite 开 WAL，
不会阻塞采集。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from ..core.judge.base import RESULT_CORRECT, RESULT_TIMEOUT, RESULT_WRONG
from ..practice.session import (
    EVENT_CORRECT,
    EVENT_FINISHED,
    EVENT_SKIPPED,
    EVENT_TIMEOUT,
    EVENT_WRONG,
    SessionEvent,
    SessionSummary,
)
from .db import Database, utc_now_iso


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class PracticeRepository:
    """练习数据的读写。"""

    def __init__(self, db: Database, profile_id: int) -> None:
        self.db = db
        self.profile_id = profile_id

    # ------------------------------------------------------------------ 会话
    def start_session(
        self,
        conn: sqlite3.Connection,
        *,
        module_id: str,
        mode: str,
        target_value: int | None,
        started_at: str | None = None,
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO sessions (profile_id, module_id, mode, target_value, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.profile_id, module_id, mode, target_value, started_at or utc_now_iso()),
        )
        conn.commit()
        return int(cursor.lastrowid)

    def finish_session(self, conn: sqlite3.Connection, session_id: int, summary: SessionSummary) -> None:
        conn.execute(
            "UPDATE sessions SET ended_at = ?, total = ?, correct_first = ?, correct_final = ?, "
            "wrong = ?, skipped = ?, timeout = ?, avg_rt_ms = ?, best_combo = ?, score = ? "
            "WHERE id = ?",
            (
                utc_now_iso(),
                summary.asked,
                summary.correct_first,
                summary.correct_final,
                summary.wrong_attempts,
                summary.skipped,
                summary.timeouts,
                summary.avg_rt_ms,
                summary.best_combo,
                summary.score,
                session_id,
            ),
        )
        conn.commit()

    # ------------------------------------------------------------------ 答题
    def record_attempt(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: int,
        item_id: int | None,
        question_json: str,
        source_tag: str,
        result: str,
        attempt_index: int,
        rt_ms: int | None,
        detected_pitch_class: int | None,
        detected_hz: float | None,
        cents_offset: float | None,
        confidence: float | None,
        answered_at: str | None = None,
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO attempts (session_id, item_id, question_json, source_tag, answered_at, "
            "result, attempt_index, rt_ms, detected_pitch_class, detected_hz, cents_offset, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                item_id,
                question_json,
                source_tag,
                answered_at or utc_now_iso(),
                result,
                attempt_index,
                rt_ms,
                detected_pitch_class,
                detected_hz,
                cents_offset,
                confidence,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)

    # ------------------------------------------------------------------ 查询
    def recent_sessions(self, conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT * FROM sessions WHERE profile_id = ? ORDER BY id DESC LIMIT ?",
                (self.profile_id, limit),
            )
        )

    def session_attempts(self, conn: sqlite3.Connection, session_id: int) -> list[sqlite3.Row]:
        return list(
            conn.execute("SELECT * FROM attempts WHERE session_id = ? ORDER BY id", (session_id,))
        )


class SessionRecorder:
    """把 ``PracticeSession`` 的事件写进数据库。

    用法::

        recorder = SessionRecorder(repository, module_id="pitch_find", mode="count", target_value=30)
        session.observer = recorder
        ...
        recorder.close()
    """

    def __init__(
        self,
        repository: PracticeRepository,
        *,
        module_id: str,
        mode: str,
        target_value: int | None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self.repository = repository
        self.module_id = module_id
        self.mode = mode
        self.target_value = target_value
        self._own_conn = conn is None
        self.conn = conn or repository.db.connect()
        self.session_id: int | None = None
        self.attempts_written = 0
        self.error: str | None = None

    # ------------------------------------------------------------------ 观察者
    def __call__(self, event: SessionEvent) -> None:
        try:
            self._handle(event)
        except Exception as exc:  # noqa: BLE001 - 记录失败不得影响练习
            self.error = f"{type(exc).__name__}: {exc}"

    def _handle(self, event: SessionEvent) -> None:
        if event.kind == "started" and self.session_id is None:
            self.session_id = self.repository.start_session(
                self.conn,
                module_id=self.module_id,
                mode=self.mode,
                target_value=self.target_value,
            )
            return

        if self.session_id is None:
            return

        if event.kind == EVENT_FINISHED:
            if event.summary is not None:
                self.repository.finish_session(self.conn, self.session_id, event.summary)
            return

        result_map = {
            EVENT_CORRECT: RESULT_CORRECT,
            EVENT_WRONG: RESULT_WRONG,
            EVENT_TIMEOUT: RESULT_TIMEOUT,
            EVENT_SKIPPED: "skipped",
        }
        result = result_map.get(event.kind)
        if result is None or event.question is None:
            return

        question = event.question
        outcome = event.outcome
        self.repository.record_attempt(
            self.conn,
            session_id=self.session_id,
            item_id=None,  # 训练项由 M3 的记忆曲线维护
            question_json=json.dumps(
                {
                    "item_key": question.item_key,
                    "target_pc": question.target_pc,
                    "string": question.position.string,
                    "fret": question.position.fret,
                    "midi": question.position.midi,
                    "level_id": question.level_id,
                },
                ensure_ascii=False,
            ),
            source_tag=question.source,
            result=result,
            attempt_index=outcome.attempt_index if outcome else 1,
            rt_ms=int(outcome.elapsed_ms) if outcome and outcome.elapsed_ms else None,
            detected_pitch_class=outcome.detected_pc if outcome else None,
            detected_hz=outcome.detected_hz if outcome else None,
            cents_offset=outcome.cents if outcome else None,
            confidence=None,
            answered_at=_now_iso(),
        )
        self.attempts_written += 1

    # ------------------------------------------------------------------ 收尾
    def close(self) -> None:
        if self._own_conn:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass


__all__ = ["PracticeRepository", "SessionRecorder"]
