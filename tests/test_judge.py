"""判定层测试：滑动窗口多数表决、容差、宽容期、超时、三种模式。"""

from __future__ import annotations

import pytest

from jitatrainer.core.audio.events import PitchEvent
from jitatrainer.core.judge.base import (
    MODE_GRACE,
    MODE_LENIENT,
    MODE_STRICT,
    RESULT_CORRECT,
    RESULT_TIMEOUT,
    RESULT_WRONG,
    STABLE_PRESETS,
)
from jitatrainer.core.judge.pitch_class_judge import JudgeConfig, PitchClassJudge
from jitatrainer.core.theory.notes import midi_to_hz

E4 = midi_to_hz(64)  # pitch class 4
A2 = midi_to_hz(45)  # pitch class 9


def ev(t: float, hz: float = E4, confidence: float = 0.95) -> PitchEvent:
    return PitchEvent(t=t, hz=hz, confidence=confidence)


def feed_all(judge: PitchClassJudge, events, ) -> list:
    outcomes = []
    for event in events:
        outcome = judge.feed(event)
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


class TestConfig:
    def test_presets(self) -> None:
        assert STABLE_PRESETS == {"fast": 150, "balanced": 200, "robust": 300}
        assert JudgeConfig.from_preset("balanced", target_pc=4).stable_ms == 200
        assert JudgeConfig.from_preset("fast", target_pc=4).stable_ms == 150

    def test_needed_frames(self) -> None:
        cfg = JudgeConfig.from_preset("balanced", target_pc=4, hop_ms=10.0)
        assert cfg.needed_frames == 20
        assert JudgeConfig.from_preset("robust", target_pc=4, hop_ms=10.0).needed_frames == 30

    def test_grace_by_mode(self) -> None:
        base = dict(target_pc=4, grace_ms=2000)
        assert JudgeConfig(mode=MODE_GRACE, **base).effective_grace_ms == 2000
        assert JudgeConfig(mode=MODE_STRICT, **base).effective_grace_ms == 0
        assert JudgeConfig(mode=MODE_LENIENT, **base).effective_grace_ms == 0

    def test_validation(self) -> None:
        with pytest.raises(ValueError):
            JudgeConfig(target_pc=99).validate()
        with pytest.raises(ValueError):
            JudgeConfig(target_pc=4, tolerance_cents=0).validate()
        with pytest.raises(ValueError):
            JudgeConfig(target_pc=4, mode="bogus").validate()
        with pytest.raises(KeyError):
            JudgeConfig.from_preset("turbo", target_pc=4)


class TestGracePeriod:
    def test_events_in_grace_are_ignored(self) -> None:
        cfg = JudgeConfig(target_pc=4, grace_ms=2000, mode=MODE_GRACE)
        judge = PitchClassJudge(cfg, started_at=0.0)
        # 宽容期内弹错也不判错
        assert feed_all(judge, [ev(t / 100, hz=A2) for t in range(1, 150)]) == []

    def test_strict_mode_judges_immediately(self) -> None:
        cfg = JudgeConfig(target_pc=4, grace_ms=2000, mode=MODE_STRICT)
        judge = PitchClassJudge(cfg, started_at=0.0)
        outcomes = feed_all(judge, [ev(t / 100, hz=A2) for t in range(1, 30)])
        assert outcomes and outcomes[0].result == RESULT_WRONG


class TestVoting:
    def test_correct_note_is_detected(self) -> None:
        cfg = JudgeConfig.from_preset("balanced", target_pc=4)
        judge = PitchClassJudge(cfg, started_at=0.0)
        outcomes = feed_all(judge, [ev(2.0 + i * 0.01, hz=E4) for i in range(25)])
        assert len(outcomes) == 1
        assert outcomes[0].result == RESULT_CORRECT
        assert outcomes[0].detected_pc == 4
        assert judge.finished

    def test_wrong_note_is_detected_and_window_resets(self) -> None:
        cfg = JudgeConfig.from_preset("balanced", target_pc=4)
        judge = PitchClassJudge(cfg, started_at=0.0)
        outcomes = feed_all(judge, [ev(2.0 + i * 0.01, hz=A2) for i in range(25)])
        assert len(outcomes) == 1
        assert outcomes[0].result == RESULT_WRONG
        assert outcomes[0].detected_pc == 9
        assert judge.attempt_index == 2
        assert not judge.finished

    def test_recovers_after_playing_correctly(self) -> None:
        """答错后停留，用户改正即通过（需求 FR-537）。"""
        cfg = JudgeConfig.from_preset("balanced", target_pc=4)
        judge = PitchClassJudge(cfg, started_at=0.0)
        wrong = feed_all(judge, [ev(2.0 + i * 0.01, hz=A2) for i in range(25)])
        assert wrong[0].result == RESULT_WRONG
        right = feed_all(judge, [ev(3.0 + i * 0.01, hz=E4) for i in range(25)])
        assert right and right[0].result == RESULT_CORRECT
        assert right[0].attempt_index == 2

    def test_majority_vote_tolerates_dropouts(self) -> None:
        """滑动窗口多数表决必须能容忍偶发丢帧（M0 实测的关键结论）。"""
        cfg = JudgeConfig.from_preset("balanced", target_pc=4)
        judge = PitchClassJudge(cfg, started_at=0.0)
        events = []
        for i in range(30):
            if i % 5 == 0:  # 每 5 帧丢 1 帧
                events.append(PitchEvent(t=2.0 + i * 0.01, hz=0.0, confidence=0.0))
            else:
                events.append(ev(2.0 + i * 0.01, hz=E4))
        outcomes = feed_all(judge, events)
        assert outcomes and outcomes[0].result == RESULT_CORRECT

    def test_no_decision_when_alternating(self) -> None:
        """音名反复横跳时不允许草率判定。"""
        cfg = JudgeConfig.from_preset("balanced", target_pc=4)
        judge = PitchClassJudge(cfg, started_at=0.0)
        events = [ev(2.0 + i * 0.01, hz=E4 if i % 2 else A2) for i in range(20)]
        assert feed_all(judge, events) == []


class TestTolerance:
    def test_sharp_note_within_tolerance_passes(self) -> None:
        cfg = JudgeConfig.from_preset("balanced", target_pc=4, tolerance_cents=25.0)
        judge = PitchClassJudge(cfg, started_at=0.0)
        detuned = E4 * 2 ** (20 / 1200)  # +20 音分
        outcomes = feed_all(judge, [ev(2.0 + i * 0.01, hz=detuned) for i in range(25)])
        assert outcomes and outcomes[0].result == RESULT_CORRECT

    def test_note_outside_tolerance_is_not_correct(self) -> None:
        cfg = JudgeConfig.from_preset("balanced", target_pc=4, tolerance_cents=25.0)
        judge = PitchClassJudge(cfg, started_at=0.0)
        detuned = E4 * 2 ** (40 / 1200)  # +40 音分，超出容差
        outcomes = feed_all(judge, [ev(2.0 + i * 0.01, hz=detuned) for i in range(25)])
        assert outcomes and outcomes[0].result == RESULT_WRONG
        assert outcomes[0].detected_pc == 4  # 音名对，音准不对
        assert outcomes[0].cents is not None and outcomes[0].cents > 25


class TestTimeout:
    def test_timeout_after_silence(self) -> None:
        cfg = JudgeConfig.from_preset("balanced", target_pc=4, timeout_ms=8000)
        judge = PitchClassJudge(cfg, started_at=0.0)
        silent = [PitchEvent(t=t / 10, hz=0.0) for t in range(1, 200)]
        outcomes = feed_all(judge, silent)
        assert outcomes and outcomes[0].result == RESULT_TIMEOUT

    def test_no_timeout_while_playing(self) -> None:
        cfg = JudgeConfig.from_preset("balanced", target_pc=4, timeout_ms=1000)
        judge = PitchClassJudge(cfg, started_at=0.0)
        # 一直弹错但持续有声音：不应超时，只应不断判错
        events = []
        for i in range(300):
            events.append(ev(2.0 + i * 0.01, hz=A2))
            events.append(ev(2.0 + i * 0.01 + 0.005, hz=A2))
        outcomes = feed_all(judge, events)
        assert outcomes
        assert all(o.result == RESULT_WRONG for o in outcomes)


class TestArming:
    """回归：残留音与重复判错。

    吉他的一根弦可以响好几秒，比宽容期还长。上一题答对后，那个音还在响，
    这些"残留帧"曾被喂给下一题并判错（真实缺陷）。修复方式是判定器引入
    "武装"机制：指定要忽略的音名，直到出现**新拨弦**或**不同音名**才参与判定。
    """

    def test_ignored_pitch_class_is_not_judged(self) -> None:
        cfg = JudgeConfig(
            target_pc=4, grace_ms=0, stable_ms=100, mode=MODE_GRACE, ignore_pitch_class=9
        )
        judge = PitchClassJudge(cfg, started_at=0.0)
        # 反复喂上一题残留的音（A），不应产生任何判定
        tail = [ev(t / 100, hz=A2) for t in range(1, 40)]
        assert feed_all(judge, tail) == []

    def test_new_onset_arms_the_judge(self) -> None:
        cfg = JudgeConfig(
            target_pc=4, grace_ms=0, stable_ms=100, mode=MODE_GRACE, ignore_pitch_class=9
        )
        judge = PitchClassJudge(cfg, started_at=0.0)
        assert feed_all(judge, [ev(t / 100, hz=A2) for t in range(1, 20)]) == []

        # 用户重新拨弦（带起音）——弹的还是 A，这次必须判错
        fresh = [
            PitchEvent(t=1.0 + i * 0.01, hz=A2, confidence=0.95, is_onset=(i == 0))
            for i in range(20)
        ]
        outcomes = feed_all(judge, fresh)
        assert outcomes and outcomes[0].result == RESULT_WRONG

    def test_different_pitch_class_arms_without_onset(self) -> None:
        """没有检测到起音时，弹出**不同音名**也应开始判定。"""
        cfg = JudgeConfig(
            target_pc=4, grace_ms=0, stable_ms=100, mode=MODE_GRACE, ignore_pitch_class=9
        )
        judge = PitchClassJudge(cfg, started_at=0.0)
        assert feed_all(judge, [ev(t / 100, hz=A2) for t in range(1, 15)]) == []
        # 换弹 E4（目标音名）→ 应立即判定为正确
        outcomes = feed_all(judge, [ev(1.0 + i * 0.01, hz=E4) for i in range(20)])
        assert outcomes and outcomes[0].result == RESULT_CORRECT

    def test_wrong_verdict_requires_new_onset(self) -> None:
        """同一根还在响的弦不得被反复判错（否则一次弹错会被记成很多次）。"""
        cfg = JudgeConfig.from_preset("balanced", target_pc=4)
        judge = PitchClassJudge(cfg, started_at=0.0)
        first = feed_all(judge, [ev(2.0 + i * 0.01, hz=A2) for i in range(25)])
        assert len(first) == 1 and first[0].result == RESULT_WRONG
        assert first[0].attempt_index == 1

        second = feed_all(judge, [ev(3.0 + i * 0.01, hz=A2) for i in range(25)])
        assert second == [], "同一个持续音被重复判错"
        assert judge.attempt_index == 2

        third = feed_all(
            judge,
            [
                PitchEvent(t=4.0 + i * 0.01, hz=A2, confidence=0.95, is_onset=(i == 0))
                for i in range(25)
            ],
        )
        assert len(third) == 1 and third[0].attempt_index == 2

    def test_silence_resets_ignored_pitch_class(self) -> None:
        """静音足够久说明上一个音已衰减，此时同一音名也应恢复判定。"""
        cfg = JudgeConfig(
            target_pc=4,
            grace_ms=0,
            stable_ms=100,
            mode=MODE_GRACE,
            ignore_pitch_class=9,
            silence_reset_ms=100,
            hop_ms=10.0,
        )
        judge = PitchClassJudge(cfg, started_at=0.0)
        # 200ms 静音
        assert feed_all(judge, [PitchEvent(t=i * 0.01, hz=0.0) for i in range(20)]) == []
        outcomes = feed_all(judge, [ev(1.0 + i * 0.01, hz=A2) for i in range(20)])
        assert outcomes and outcomes[0].result == RESULT_WRONG

    def test_ignore_is_none_by_default(self) -> None:
        cfg = JudgeConfig(target_pc=4, grace_ms=0, stable_ms=100)
        judge = PitchClassJudge(cfg, started_at=0.0)
        outcomes = feed_all(judge, [ev(t / 100, hz=E4) for t in range(1, 20)])
        assert outcomes and outcomes[0].result == RESULT_CORRECT


class TestModes:
    def test_lenient_mode_does_not_ask_for_feedback(self) -> None:
        cfg = JudgeConfig.from_preset("balanced", target_pc=4, mode=MODE_LENIENT)
        judge = PitchClassJudge(cfg, started_at=0.0)
        outcomes = feed_all(judge, [ev(0.1 + i * 0.01, hz=A2) for i in range(30)])
        assert outcomes and outcomes[0].result == RESULT_WRONG
        assert outcomes[0].feedback is False

    def test_grace_mode_reports_feedback(self) -> None:
        cfg = JudgeConfig.from_preset("balanced", target_pc=4, mode=MODE_GRACE)
        judge = PitchClassJudge(cfg, started_at=0.0)
        outcomes = feed_all(judge, [ev(2.0 + i * 0.01, hz=A2) for i in range(30)])
        assert outcomes and outcomes[0].feedback is True

    def test_low_confidence_frames_are_ignored(self) -> None:
        cfg = JudgeConfig.from_preset("balanced", target_pc=4, confidence_min=0.80)
        judge = PitchClassJudge(cfg, started_at=0.0)
        events = [ev(2.0 + i * 0.01, hz=E4, confidence=0.5) for i in range(30)]
        assert feed_all(judge, events) == []
