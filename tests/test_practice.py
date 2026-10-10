"""模块层与会话控制器测试（无需音频设备、无需 Qt）。"""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from jitatrainer.core.audio.events import PitchEvent
from jitatrainer.core.judge.pitch_class_judge import JudgeConfig
from jitatrainer.core.theory.notes import NATURAL_PITCH_CLASSES, midi_to_hz, pitch_class
from jitatrainer.practice import registry
from jitatrainer.practice.base import SOURCE_REQUEUE
from jitatrainer.practice.pitch_find.module import (
    MAX_SAME_PITCH_CLASS_RUN,
    PitchFindModule,
    item_key,
    make_context,
)
from jitatrainer.practice.session import (
    TIMEOUT_MANUAL,
    EVENT_CORRECT,
    EVENT_FINISHED,
    EVENT_QUESTION,
    EVENT_SKIPPED,
    EVENT_TIMEOUT,
    EVENT_WRONG,
    LEVEL_DIFFICULTY,
    MODE_COUNT,
    MODE_DURATION,
    MODE_FREE,
    PracticeSession,
    SessionConfig,
)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def fast_judge_config(question, *, stable_ms: int = 100, timeout_ms: int = 5000) -> JudgeConfig:
    """测试用判定配置：短稳定时长 + 短宽容期。

    保留宽容期是刻意的：上一题答对后，环形缓冲里还剩着旧音的音频帧，
    新题如果不设宽容期就会被这些"残留帧"判错（严格模式下这是设计如此，
    但练习默认走宽容期模式）。
    """
    return JudgeConfig(
        target_pc=question.target_pc,
        stable_ms=stable_ms,
        grace_ms=100,
        timeout_ms=timeout_ms,
        mode="grace",
        tolerance_cents=25.0,
        hop_ms=10.0,
    )


#: 一道题要喂多少帧：10 帧落在宽容期内 + 10 帧用于触发判定，再留余量
FRAMES_PER_QUESTION = 30


def note_events(target_pc: int, *, start: float = 0.0, count: int = FRAMES_PER_QUESTION, step: float = 0.01):
    """生成一串"弹对了"的事件。

    第一帧标记为起音（is_onset）——真实拨弦一定有起音，判定器靠它区分
    "新弹的音"与"上一题还在响的残留音"。
    """
    midi = 60 + target_pc  # C4 起的同名音
    hz = midi_to_hz(midi)
    return [
        PitchEvent(t=start + i * step, hz=hz, confidence=0.95, is_onset=(i == 0))
        for i in range(count)
    ]


def wrong_events(
    target_pc: int, *, start: float = 0.0, count: int = FRAMES_PER_QUESTION, step: float = 0.01
):
    other = (target_pc + 5) % 12
    midi = 60 + other
    hz = midi_to_hz(midi)
    return [
        PitchEvent(t=start + i * step, hz=hz, confidence=0.95, is_onset=(i == 0))
        for i in range(count)
    ]


def silence_events(*, start: float = 0.0, count: int = 20, step: float = 0.01):
    return [PitchEvent(t=start + i * step, hz=0.0) for i in range(count)]


# ---------------------------------------------------------------------------
# 注册表与出题
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_builtin_module_is_registered(self) -> None:
        assert "pitch_find" in registry.available_module_ids()

    def test_get_module_and_metadata(self) -> None:
        module = registry.get_module("pitch_find")
        assert module.id == "pitch_find"
        assert module.label("zh_CN") == "看谱找音"
        assert module.label("en_US") == "Note Finding"
        assert "六线谱" in module.describe("zh_CN")

    def test_unknown_module(self) -> None:
        with pytest.raises(KeyError):
            registry.get_module("not_here")

    def test_duplicate_registration_rejected(self) -> None:
        class Duplicate:
            id = "pitch_find"
            version = "0"
            name = {"zh_CN": "x", "en_US": "x"}
            description = {"zh_CN": "x", "en_US": "x"}

        with pytest.raises(ValueError):
            registry.register_module(Duplicate)


class TestQuestionGeneration:
    def test_question_is_inside_level(self) -> None:
        module = PitchFindModule(random.Random(1))
        ctx = make_context(level_id="L1")
        for _ in range(50):
            question = module.generate(ctx)
            assert question.position.fret <= 3
            assert question.position.pitch_class == question.target_pc
            assert question.item_key == item_key(question.target_pc, "L1")

    def test_item_key_format(self) -> None:
        assert item_key(11, "L2") == "note=B|level=L2"

    def test_natural_only_excludes_accidentals(self) -> None:
        module = PitchFindModule(random.Random(2))
        ctx = make_context(level_id="L3", include_accidentals=False)
        for _ in range(60):
            assert module.generate(ctx).target_pc in NATURAL_PITCH_CLASSES

    def test_accidentals_can_appear(self) -> None:
        module = PitchFindModule(random.Random(3))
        ctx = make_context(level_id="L3", include_accidentals=True)
        seen = {module.generate(ctx).target_pc for _ in range(200)}
        assert seen - NATURAL_PITCH_CLASSES, "开启升降号后应能出现变化音"

    def test_avoids_same_pitch_class_run(self) -> None:
        """同一音名不允许连续出现超过 MAX_SAME_PITCH_CLASS_RUN 次。"""
        module = PitchFindModule(random.Random(4))
        ctx = make_context(level_id="L4", include_accidentals=True)
        history: list[int] = []
        for _ in range(200):
            history.append(module.generate(ctx).target_pc)
        worst = 1
        run = 1
        for prev, cur in zip(history, history[1:]):
            run = run + 1 if cur == prev else 1
            worst = max(worst, run)
        assert worst <= MAX_SAME_PITCH_CLASS_RUN, f"最长连续同音 {worst} 次"

    def test_requested_pitch_class_is_respected(self) -> None:
        """调度层指定音名时（M3 记忆曲线用），模块必须出这个音。"""
        module = PitchFindModule(random.Random(5))
        ctx = make_context(level_id="L1", requested_pc=11)
        for _ in range(20):
            assert module.generate(ctx).target_pc == 11

    def test_source_tag_propagates(self) -> None:
        module = PitchFindModule(random.Random(6))
        ctx = make_context(level_id="L1")
        question = module.generate(ctx)
        assert question.source == "new"

    def test_render_spec(self) -> None:
        module = PitchFindModule(random.Random(7))
        question = module.generate(make_context(level_id="L2"))
        spec = module.render_spec(question, show_note_name=True)
        assert spec.kind == "tab"
        assert spec.show_note_name is True
        assert "弦" in spec.label_zh
        assert spec.note_name == question.position.note_name

    def test_judge_config_from_settings(self) -> None:
        class StubSettings:
            def get_float(self, key, default=0.0):
                return {"tolerance_cents": 30.0, "confidence_min": 0.9}.get(key, default)

            def get_int(self, key, default=0):
                return {"grace_ms": 1500, "timeout_ms": 9000}.get(key, default)

            def get(self, key, default=None):
                return {"retry_mode": "strict"}.get(key, default)

            def stable_ms(self):
                return 250

        module = PitchFindModule(random.Random(8))
        question = module.generate(make_context(level_id="L1"))
        config = module.make_judge_config(question, StubSettings())
        assert config.target_pc == question.target_pc
        assert config.tolerance_cents == 30.0
        assert config.stable_ms == 250
        assert config.timeout_ms == 9000
        assert config.mode == "strict"


# ---------------------------------------------------------------------------
# 会话控制器
# ---------------------------------------------------------------------------
def make_session(config: SessionConfig, clock: FakeClock | None = None) -> PracticeSession:
    # 宽容期与超时现在由会话配置决定（用户策略），测试里显式设短，
    # 与 fast_judge_config 的意图保持一致。
    config = replace(config, grace_seconds=0.1)
    module = PitchFindModule(random.Random(42))
    return PracticeSession(
        module,
        config,
        fast_judge_config,
        profile_id=1,
        clock=clock or FakeClock(),
    )


class TestSessionBasic:
    def test_start_emits_question(self) -> None:
        session = make_session(SessionConfig(mode=MODE_COUNT, target_count=3))
        events = session.start()
        assert [e.kind for e in events] == ["started", EVENT_QUESTION]
        assert session.current_question is not None
        assert session.question_index == 1

    def test_correct_answer_advances(self) -> None:
        session = make_session(SessionConfig(mode=MODE_COUNT, target_count=3))
        session.start()
        question = session.current_question
        assert question is not None
        events = []
        for pitch in note_events(question.target_pc):
            events.extend(session.feed(pitch))
        assert [e.kind for e in events] == [EVENT_CORRECT, EVENT_QUESTION]
        assert session.stats.correct_first == 1
        assert session.stats.combo == 1

    def test_count_mode_finishes_at_target(self) -> None:
        target = 3
        session = make_session(SessionConfig(mode=MODE_COUNT, target_count=target))
        session.start()
        kinds: list[str] = []
        base = 0.0
        while not session.is_finished:
            question = session.current_question
            assert question is not None
            for pitch in note_events(question.target_pc, start=base):
                kinds.extend(e.kind for e in session.feed(pitch))
                if session.is_finished:
                    break
            base += 1.0
        assert kinds[-1] == EVENT_FINISHED
        summary = session.build_summary()
        assert summary.asked == target
        assert summary.correct_first == target
        assert summary.accuracy_first == 1.0

    def test_wrong_then_correct_counts_first_attempt(self) -> None:
        session = make_session(SessionConfig(mode=MODE_COUNT, target_count=2))
        session.start()
        question = session.current_question
        assert question is not None

        events = []
        for pitch in wrong_events(question.target_pc, start=0.0):
            events.extend(session.feed(pitch))
        assert events[0].kind == EVENT_WRONG
        assert session.current_question is question  # 答错停留，不出新题

        for pitch in note_events(question.target_pc, start=1.0):
            events.extend(session.feed(pitch))
        assert session.stats.asked == 1
        assert session.stats.correct_first == 0
        assert session.stats.correct_final == 1
        assert session.stats.wrong_attempts == 1

    def test_timeout_marks_unknown_and_advances(self) -> None:
        # 超时秒数现在由会话配置决定（用户策略），不再取模块的 judge 配置
        session = make_session(
            SessionConfig(mode=MODE_COUNT, target_count=3, timeout_seconds=5.0)
        )
        session.start()
        events = []
        for pitch in silence_events(count=600, step=0.01):
            events.extend(session.feed(pitch))
            if events:
                break
        assert events and events[0].kind == EVENT_TIMEOUT
        assert events[0].reason == "timeout"
        assert events[-1].kind == EVENT_QUESTION
        assert session.stats.timeouts == 1

    def test_manual_mode_never_times_out(self) -> None:
        """手动推进模式：再久也不会自动换题。"""
        session = make_session(
            SessionConfig(mode=MODE_COUNT, target_count=3, timeout_mode=TIMEOUT_MANUAL)
        )
        session.start()
        for pitch in silence_events(count=3000, step=0.01):  # 等效 30 秒静音
            assert session.feed(pitch) == []
        assert session.stats.timeouts == 0
        assert session.stats.asked == 0
        assert session.current_question is not None

    def test_manual_advance_records_as_weak(self) -> None:
        session = make_session(
            SessionConfig(mode=MODE_COUNT, target_count=3, timeout_mode=TIMEOUT_MANUAL)
        )
        session.start()
        events = session.advance()
        assert events[0].kind == EVENT_SKIPPED
        assert events[0].reason == "manual"
        assert events[-1].kind == EVENT_QUESTION
        assert session.stats.skipped == 1
        assert session.stats.wrong_by_item

    def test_remaining_ms_tracks_timeout(self) -> None:
        session = make_session(
            SessionConfig(mode=MODE_COUNT, target_count=3, timeout_seconds=5.0)
        )
        session.start()
        first = next(iter(silence_events(count=1)))
        session.feed(first)
        remaining = session.remaining_ms(first.t)
        assert remaining is not None
        assert 0 < remaining <= 6000

    def test_remaining_ms_is_none_in_manual_mode(self) -> None:
        session = make_session(
            SessionConfig(mode=MODE_COUNT, target_count=3, timeout_mode=TIMEOUT_MANUAL)
        )
        session.start()
        first = next(iter(silence_events(count=1)))
        session.feed(first)
        assert session.remaining_ms(first.t) is None

    def test_skip_advances_and_records_weak_item(self) -> None:
        session = make_session(SessionConfig(mode=MODE_COUNT, target_count=3))
        session.start()
        question = session.current_question
        assert question is not None
        events = session.skip()
        assert [e.kind for e in events] == [EVENT_SKIPPED, EVENT_QUESTION]
        assert session.stats.skipped == 1
        assert session.stats.weakest_items()[0][0] == question.item_key

    def test_free_mode_only_finishes_on_request(self) -> None:
        session = make_session(SessionConfig(mode=MODE_FREE))
        session.start()
        for index in range(5):
            question = session.current_question
            assert question is not None
            for pitch in note_events(question.target_pc, start=float(index)):
                session.feed(pitch)
        assert not session.is_finished
        events = session.finish()
        assert events[0].kind == EVENT_FINISHED
        assert session.build_summary().asked == 5

    def test_feed_after_finish_is_ignored(self) -> None:
        session = make_session(SessionConfig(mode=MODE_COUNT, target_count=1))
        session.start()
        question = session.current_question
        assert question is not None
        for pitch in note_events(question.target_pc):
            session.feed(pitch)
        assert session.is_finished
        assert session.feed(PitchEvent(t=99.0, hz=440.0, confidence=1.0)) == []


class TestSessionTiming:
    def test_duration_mode_finishes_on_tick(self) -> None:
        clock = FakeClock()
        session = make_session(SessionConfig(mode=MODE_DURATION, target_minutes=1), clock)
        session.start()
        assert session.remaining_s == pytest.approx(60.0)

        clock.advance(30.0)
        assert session.tick() == []
        assert session.remaining_s == pytest.approx(30.0)

        clock.advance(31.0)
        events = session.tick()
        assert events and events[0].kind == EVENT_FINISHED
        assert session.build_summary().duration_s == pytest.approx(61.0, abs=0.5)

    def test_pause_excludes_time_and_blocks_feed(self) -> None:
        clock = FakeClock()
        session = make_session(SessionConfig(mode=MODE_DURATION, target_minutes=1), clock)
        session.start()
        question = session.current_question
        assert question is not None

        clock.advance(10.0)
        session.pause()
        assert session.is_paused
        clock.advance(100.0)
        assert session.feed(PitchEvent(t=110.0, hz=midi_to_hz(60 + question.target_pc), confidence=0.95)) == []
        assert session.elapsed_s == pytest.approx(10.0)
        assert session.tick() == []

        session.resume()
        clock.advance(5.0)
        assert session.elapsed_s == pytest.approx(15.0)

    def test_toggle_pause(self) -> None:
        session = make_session(SessionConfig())
        session.start()
        assert session.toggle_pause() is True
        assert session.toggle_pause() is False


class TestResidualNoteRegression:
    """回归：上一题的残留音不得把下一题判错（真实缺陷）。

    吉他音可以响好几秒，比宽容期更长。修好之前，连续两题音名相同时，
    第二题会被上一题还在响的音"秒判错"。
    """

    def test_previous_note_tail_does_not_affect_next_question(self) -> None:
        from jitatrainer.core.theory.fretboard import Position
        from jitatrainer.practice.base import Question

        class FixedModule:
            """永远出同一个音（最容易触发残留音问题）。"""

            id = "fixed"
            version = "1"
            name = {"zh_CN": "测试", "en_US": "test"}
            description = {"zh_CN": "测试", "en_US": "test"}

            def generate(self, ctx: QuestionContext) -> Question:  # noqa: ANN001
                return Question(
                    module_id=self.id,
                    item_key="note=E|level=L1",
                    target_pc=4,
                    position=Position(string=1, fret=0, midi=64),
                    level_id="L1",
                )

        session = PracticeSession(
            FixedModule(),
            SessionConfig(mode=MODE_COUNT, target_count=2),
            fast_judge_config,
            clock=FakeClock(),
        )
        session.start()

        # 第 1 题：正常弹对
        events = []
        for pitch in note_events(4, start=0.0):
            events.extend(session.feed(pitch))
        assert events[0].kind == EVENT_CORRECT
        assert events[-1].kind == EVENT_QUESTION

        # 上一题的音还在响（无起音的残留帧）→ 不得产生任何判定
        tail = [
            PitchEvent(t=0.30 + i * 0.01, hz=midi_to_hz(64), confidence=0.95)
            for i in range(30)
        ]
        tail_events: list = []
        for pitch in tail:
            tail_events.extend(session.feed(pitch))
        assert tail_events == [], f"残留音产生了判定：{[e.kind for e in tail_events]}"

        # 用户真正弹第 2 题（带起音）
        for pitch in note_events(4, start=1.0):
            session.feed(pitch)

        summary = session.build_summary()
        assert summary.wrong_attempts == 0
        assert summary.asked == 2
        assert summary.correct_first == 2


class TestScoring:
    def _session(self, level_id: str = "L1") -> PracticeSession:
        return make_session(SessionConfig(mode=MODE_COUNT, target_count=99, level_id=level_id))

    def test_score_formula(self) -> None:
        from jitatrainer.core.judge.base import JudgeOutcome

        session = self._session("L1")
        session.start()
        question = session.current_question
        assert question is not None

        outcome = JudgeOutcome(result="correct", elapsed_ms=1000, attempt_index=1)
        # 100 × 1.0(难度) × 1.5(<=1.5s) × 1.0(无连击) = 150
        assert session.score_for(outcome, 0, question) == 150
        # 连击 10 → 1 + 0.05×10 = 1.5（封顶）
        assert session.score_for(outcome, 10, question) == 100 * 1.0 * 1.5 * 1.5

    def test_difficulty_and_time_tiers(self) -> None:
        from jitatrainer.core.judge.base import JudgeOutcome

        assert LEVEL_DIFFICULTY["L4"] == 1.6
        session = self._session("L4")
        session.start()
        question = session.current_question
        assert question is not None

        slow = JudgeOutcome(result="correct", elapsed_ms=9000, attempt_index=1)
        medium = JudgeOutcome(result="correct", elapsed_ms=4000, attempt_index=1)
        assert session.score_for(slow, 0, question) == round(100 * 1.6 * 0.8)
        assert session.score_for(medium, 0, question) == round(100 * 1.6 * 1.0)

    def test_requeue_scores_half(self) -> None:
        from dataclasses import replace

        from jitatrainer.core.judge.base import JudgeOutcome

        session = self._session("L1")
        session.start()
        question = session.current_question
        assert question is not None
        requeued = replace(question, source=SOURCE_REQUEUE)
        outcome = JudgeOutcome(result="correct", elapsed_ms=1000, attempt_index=1)
        assert session.score_for(outcome, 0, requeued) == 75

    def test_scoring_can_be_disabled(self) -> None:
        session = make_session(SessionConfig(mode=MODE_COUNT, target_count=3, scoring_enabled=False))
        session.start()
        question = session.current_question
        assert question is not None
        for index in range(3):
            question = session.current_question
            assert question is not None
            for pitch in note_events(question.target_pc, start=float(index)):
                session.feed(pitch)
        assert session.build_summary().score == 0


class TestSummary:
    def test_summary_reports_weakest_items(self) -> None:
        session = make_session(SessionConfig(mode=MODE_COUNT, target_count=4))
        session.start()
        weak_key = None
        for index in range(4):
            question = session.current_question
            assert question is not None
            if index == 0:
                weak_key = question.item_key
                session.skip()
            else:
                for pitch in note_events(question.target_pc, start=float(index)):
                    session.feed(pitch)
        summary = session.build_summary()
        assert summary.asked == 4
        assert summary.skipped == 1
        assert summary.weakest and summary.weakest[0][0] == weak_key

    def test_summary_avg_reaction_time(self) -> None:
        session = make_session(SessionConfig(mode=MODE_COUNT, target_count=2))
        session.start()
        for index in range(2):
            question = session.current_question
            assert question is not None
            for pitch in note_events(question.target_pc, start=float(index), step=0.01):
                session.feed(pitch)
        summary = session.build_summary()
        assert summary.avg_rt_ms is not None and summary.avg_rt_ms > 0
        assert summary.best_combo == 2


class TestConfigValidation:
    def test_invalid_mode(self) -> None:
        with pytest.raises(ValueError):
            SessionConfig(mode="bogus").validate()

    def test_invalid_targets(self) -> None:
        with pytest.raises(ValueError):
            SessionConfig(mode=MODE_DURATION, target_minutes=0).validate()
        with pytest.raises(ValueError):
            SessionConfig(mode=MODE_COUNT, target_count=0).validate()

    def test_describe(self) -> None:
        assert SessionConfig(mode=MODE_COUNT, target_count=30).describe() == "30 题"
        assert SessionConfig(mode=MODE_DURATION, target_minutes=15).describe("en_US") == "15 min"
        assert SessionConfig(mode=MODE_FREE).describe() == "自由练习"
