"""内存稳定性测试（NFR-06 / 验收标准 §10-16）。

长时间练习内存不得持续增长。

判据用 **tracemalloc + 存活对象数**，而不是常驻内存（RSS）：
压缩模拟会在很短的时间里造出大量短命对象，RSS 会被分配器碎片推高，
单独看 RSS 会把"没有泄漏"误判成"泄漏"。
"""

from __future__ import annotations

import gc
import random
import tracemalloc

import pytest

from jitatrainer.core.audio.events import PitchEvent
from jitatrainer.core.judge.pitch_class_judge import JudgeConfig
from jitatrainer.core.theory.notes import midi_to_hz
from jitatrainer.practice.pitch_find.module import PitchFindModule
from jitatrainer.practice.session import MODE_FREE, PracticeSession, SessionConfig
from jitatrainer.scheduling.scheduler import QuestionScheduler

WARMUP_FRAMES = 20_000
MEASURE_FRAMES = 60_000


def judge_factory(question, **_ignored):  # noqa: ANN001
    return JudgeConfig(
        target_pc=question.target_pc, stable_ms=200, grace_ms=2000, mode="grace", hop_ms=10.0
    )


def build_session() -> PracticeSession:
    scheduler = QuestionScheduler(
        level_id="L1",
        pitch_classes=list(range(12)),
        item_key_of=lambda pc: f"note={pc}|level=L1",
        rng=random.Random(11),
    )
    session = PracticeSession(
        PitchFindModule(random.Random(11)),
        SessionConfig(mode=MODE_FREE, level_id="L1"),
        judge_factory,
        scheduler=scheduler,
    )
    session.start()
    return session


def run_frames(session: PracticeSession, frames: int, start: int = 0) -> int:
    for frame in range(start, start + frames):
        question = session.current_question
        if question is None:
            break
        session.feed(
            PitchEvent(
                t=frame * 0.01,
                hz=midi_to_hz(60 + question.target_pc),
                confidence=0.95,
                is_onset=(frame % 300 == 0),
            )
        )
    return start + frames


class TestMemoryStability:
    def test_no_python_level_leak_over_long_session(self) -> None:
        """回归：会话状态里不允许出现随题量增长的结构。

        真实缺陷：``SessionStats.reaction_times`` 曾是一个每次答对都 append 的列表，
        题量越大占用越多（现在改为"累加 + 计数"）。
        """
        session = build_session()
        cursor = run_frames(session, WARMUP_FRAMES, 0)  # 预热，让缓存与 dict 扩容稳定
        gc.collect()

        tracemalloc.start(10)
        try:
            before = tracemalloc.take_snapshot()
            objects_before = len(gc.get_objects())

            run_frames(session, MEASURE_FRAMES, cursor)
            gc.collect()

            after = tracemalloc.take_snapshot()
            objects_after = len(gc.get_objects())
        finally:
            tracemalloc.stop()

        traced = sum(stat.size_diff for stat in after.compare_to(before, "lineno"))
        object_delta = objects_after - objects_before

        assert session.stats.asked > 200, "测试应真的答了不少题"
        assert object_delta < 2000, f"存活对象数增长过多：{object_delta}"
        assert traced < 512 * 1024, f"Python 层内存增长过多：{traced / 1024:.1f} KB"

    def test_session_state_size_is_bounded(self) -> None:
        """会话内部结构都应有界（题量再大也不增长）。"""
        session = build_session()
        run_frames(session, WARMUP_FRAMES, 0)

        scheduler = session.scheduler
        assert scheduler is not None
        assert len(scheduler.states) <= 12, "训练项数量应固定为题库大小"
        assert scheduler.requeue_size <= 12, "回炉队列不应超过题库大小"
        assert len(session.stats.wrong_by_item) <= 12
        assert session.stats.rt_count > 0
        # 反应时间不再保存逐条样本（只保留累加值）
        assert not hasattr(session.stats, "reaction_times")

    def test_avg_rt_still_correct_after_refactor(self) -> None:
        """改成累加式之后平均值仍要算对。"""
        stats = PracticeSession(
            PitchFindModule(random.Random(1)),
            SessionConfig(mode=MODE_FREE),
            judge_factory,
        ).stats
        for value in (1000, 2000, 3000):
            stats.add_reaction_time(value)
        assert stats.rt_count == 3
        assert stats.avg_rt_ms == 2000

    def test_avg_rt_none_without_samples(self) -> None:
        session = PracticeSession(
            PitchFindModule(random.Random(1)), SessionConfig(mode=MODE_FREE), judge_factory
        )
        assert session.stats.avg_rt_ms is None
