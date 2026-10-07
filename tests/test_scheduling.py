"""M3 学习机制测试：SM-2 记忆曲线、易错加权、出题调度、回炉、难度建议。"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from jitatrainer.scheduling.advice import AdviceConfig, SessionRecord, should_advance
from jitatrainer.scheduling.scheduler import Decision, QuestionScheduler, ScheduleConfig
from jitatrainer.scheduling.srs import (
    EVENT_CORRECT_FIRST,
    EVENT_CORRECT_RETRY,
    EVENT_HINTED,
    EVENT_SKIP,
    EVENT_WRONG,
    INTERVALS_DAYS,
    ItemState,
    initial_state,
    review,
)
from jitatrainer.scheduling.weights import (
    error_factor,
    is_weak,
    reaction_factor,
    time_decay_factor,
    weak_weight,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
PITCH_CLASSES = list(range(12))


def key_of(pc: int, level: str = "L2") -> str:
    return f"note={pc}|level={level}"


# ---------------------------------------------------------------------------
# SM-2
# ---------------------------------------------------------------------------
class TestSrs:
    def test_initial_state(self) -> None:
        state = initial_state(key_of(4), 4, "L2")
        assert state.is_new
        assert state.interval_index == 0
        assert state.interval_days == 0
        assert state.due_at is None

    def test_correct_first_advances_interval(self) -> None:
        state = initial_state(key_of(4), 4, "L2")
        updated = review(state, EVENT_CORRECT_FIRST, now=NOW, rt_ms=1200)
        assert updated.interval_index == 1
        assert updated.interval_days == INTERVALS_DAYS[1]
        assert updated.due_at == NOW + timedelta(days=INTERVALS_DAYS[1])
        assert updated.seen_count == 1
        assert updated.correct_count == 1
        assert updated.reps == 1

    def test_interval_sequence_and_cap(self) -> None:
        state = initial_state(key_of(4), 4, "L2")
        for step in range(1, len(INTERVALS_DAYS)):
            state = review(state, EVENT_CORRECT_FIRST, now=NOW, rt_ms=1000)
            assert state.interval_index == step
        # 再答对也不越过最后一档
        for _ in range(3):
            state = review(state, EVENT_CORRECT_FIRST, now=NOW, rt_ms=1000)
        assert state.interval_index == len(INTERVALS_DAYS) - 1
        assert state.interval_days == INTERVALS_DAYS[-1]

    def test_wrong_resets_to_today(self) -> None:
        state = initial_state(key_of(4), 4, "L2")
        for _ in range(3):
            state = review(state, EVENT_CORRECT_FIRST, now=NOW, rt_ms=1000)
        assert state.interval_index == 3

        failed = review(state, EVENT_WRONG, now=NOW)
        assert failed.interval_index == 0
        assert failed.due_at == NOW
        assert failed.lapses == 1.0
        assert failed.error_rate > 0

    def test_correct_retry_counts_as_lapse(self) -> None:
        state = review(initial_state(key_of(4), 4, "L2"), EVENT_CORRECT_FIRST, now=NOW, rt_ms=900)
        retried = review(state, EVENT_CORRECT_RETRY, now=NOW, rt_ms=4000)
        assert retried.interval_index == 0
        assert retried.due_at == NOW
        assert retried.lapses == 1.0
        assert retried.correct_count == 2  # 最终弹对了，计入最终正确

    def test_skip_has_half_lapse(self) -> None:
        state = initial_state(key_of(4), 4, "L2")
        skipped = review(state, EVENT_SKIP, now=NOW)
        assert skipped.lapses == 0.5
        assert skipped.interval_index == 0
        assert skipped.due_at == NOW

    def test_hinted_correct_does_not_raise_mastery(self) -> None:
        state = initial_state(key_of(4), 4, "L2")
        hinted = review(state, EVENT_HINTED, now=NOW)
        assert hinted.interval_index == 0
        assert hinted.due_at is None
        assert hinted.seen_count == 1
        assert hinted.error_rate == 0.0

    def test_error_rate_ema(self) -> None:
        state = initial_state(key_of(4), 4, "L2")
        # 第一次答错 → 错误率直接为 1
        state = review(state, EVENT_WRONG, now=NOW)
        assert state.error_rate == pytest.approx(1.0)
        # 随后答对 → 平滑下降但不会立刻归零
        state = review(state, EVENT_CORRECT_FIRST, now=NOW, rt_ms=1000)
        assert 0.0 < state.error_rate < 1.0
        for _ in range(6):
            state = review(state, EVENT_CORRECT_FIRST, now=NOW, rt_ms=1000)
        assert state.error_rate < 0.15

    def test_avg_rt_ema(self) -> None:
        state = initial_state(key_of(4), 4, "L2")
        state = review(state, EVENT_CORRECT_FIRST, now=NOW, rt_ms=1000)
        assert state.avg_rt_ms == 1000
        state = review(state, EVENT_CORRECT_FIRST, now=NOW, rt_ms=2000)
        assert 1000 < state.avg_rt_ms < 2000

    def test_due_and_overdue(self) -> None:
        state = review(initial_state(key_of(4), 4, "L2"), EVENT_CORRECT_FIRST, now=NOW, rt_ms=900)
        assert not state.is_due(NOW)
        later = NOW + timedelta(days=2)
        assert state.is_due(later)
        assert state.overdue_days(later) == pytest.approx(1.0, abs=0.01)

    def test_unknown_event_rejected(self) -> None:
        with pytest.raises(ValueError):
            review(initial_state(key_of(4), 4, "L2"), "bogus", now=NOW)


# ---------------------------------------------------------------------------
# 易错加权
# ---------------------------------------------------------------------------
class TestWeights:
    def _state(self, **kwargs) -> ItemState:
        base = initial_state(key_of(4), 4, "L2")
        from dataclasses import replace

        return replace(base, **kwargs)

    def test_error_factor(self) -> None:
        assert error_factor(self._state(error_rate=0.0)) == pytest.approx(1.0)
        assert error_factor(self._state(error_rate=0.5)) == pytest.approx(2.0)
        assert error_factor(self._state(error_rate=1.0)) == pytest.approx(3.0)

    def test_reaction_factor(self) -> None:
        assert reaction_factor(self._state(avg_rt_ms=None)) == pytest.approx(1.0)
        assert reaction_factor(self._state(avg_rt_ms=2500), 2500) == pytest.approx(1.0)
        assert reaction_factor(self._state(avg_rt_ms=5000), 2500) == pytest.approx(1.0)  # 慢不加权，只是没有加成
        assert reaction_factor(self._state(avg_rt_ms=1250), 2500) == pytest.approx(1.75)

    def test_time_decay_factor(self) -> None:
        fresh = self._state(last_seen_at=NOW)
        assert time_decay_factor(fresh, NOW) == pytest.approx(0.5)
        week = self._state(last_seen_at=NOW - timedelta(days=7))
        assert time_decay_factor(week, NOW) == pytest.approx(1.0)

    def test_weak_weight_combines_factors(self) -> None:
        state = self._state(error_rate=0.5, avg_rt_ms=1250, last_seen_at=NOW - timedelta(days=7))
        # 2.0 × 1.75 × 1.0 = 3.5
        assert weak_weight(state, now=NOW) == pytest.approx(3.5)

    def test_is_weak(self) -> None:
        assert not is_weak(self._state())
        assert is_weak(self._state(seen_count=3, error_rate=0.2))
        assert is_weak(self._state(seen_count=3, avg_rt_ms=4000))


# ---------------------------------------------------------------------------
# 出题调度
# ---------------------------------------------------------------------------
def make_scheduler(
    *,
    states: dict[str, ItemState] | None = None,
    now: datetime = NOW,
    seed: int = 1,
    config: ScheduleConfig | None = None,
    on_state_change=None,  # noqa: ANN001
) -> QuestionScheduler:
    return QuestionScheduler(
        level_id="L2",
        pitch_classes=PITCH_CLASSES,
        item_key_of=key_of,
        states=states,
        config=config,
        rng=random.Random(seed),
        clock=lambda: now,
        on_state_change=on_state_change,
    )


def due_state(pc: int, days_overdue: float = 1.0) -> ItemState:
    state = initial_state(key_of(pc), pc, "L2")
    from dataclasses import replace

    return replace(
        state,
        seen_count=3,
        correct_count=3,
        interval_index=2,
        due_at=NOW - timedelta(days=days_overdue),
    )


def weak_state(pc: int, *, error_rate: float = 0.5, avg_rt_ms: int = 4000) -> ItemState:
    state = initial_state(key_of(pc), pc, "L2")
    from dataclasses import replace

    return replace(
        state,
        seen_count=3,
        correct_count=1,
        interval_index=1,
        due_at=NOW + timedelta(days=3),
        error_rate=error_rate,
        avg_rt_ms=avg_rt_ms,
        last_seen_at=NOW - timedelta(days=2),
    )


class TestSchedulerBasics:
    def test_new_profile_is_all_new(self) -> None:
        """首次使用无历史数据 → 全为新题（需求 §14-3）。"""
        scheduler = make_scheduler()
        sources = [scheduler.next_decision().source for _ in range(10)]
        assert set(sources) == {"new"}

    def test_pool_covers_all_pitch_classes(self) -> None:
        scheduler = make_scheduler()
        assert len(scheduler.states) == 12
        assert all(state.is_new for state in scheduler.states.values())

    def test_due_is_picked_first(self) -> None:
        states = {key_of(3): due_state(3, days_overdue=5), key_of(7): due_state(7, days_overdue=1)}
        scheduler = make_scheduler(states=states)
        first = scheduler.next_decision()
        assert first.source == "review"
        assert first.pitch_class == 3  # 逾期更久

    def test_quota_window(self) -> None:
        """每 10 题：到期 ≤4、薄弱 ≤3、新题 ≥3。"""
        states: dict[str, ItemState] = {}
        for pc in (0, 1, 2, 3, 4):
            states[key_of(pc)] = due_state(pc)
        for pc in (5, 6, 7, 8):
            states[key_of(pc)] = weak_state(pc)

        scheduler = make_scheduler(states=states)
        sources = [scheduler.next_decision().source for _ in range(10)]
        assert sources.count("review") == 4
        assert sources.count("weak") == 3
        assert sources.count("new") == 3

        # 第二个窗口重新计数
        more = [scheduler.next_decision().source for _ in range(10)]
        assert more.count("review") <= 4
        assert more.count("weak") <= 3

    def test_quota_falls_back_when_category_exhausted(self) -> None:
        states = {key_of(0): due_state(0)}
        scheduler = make_scheduler(states=states)
        sources = []
        for _ in range(12):
            decision = scheduler.next_decision()
            sources.append(decision.source)
            scheduler.on_result(decision, correct=True, rt_ms=1200)
        assert sources.count("review") == 1
        assert sources.count("new") == 11

    def test_no_consecutive_repeats(self) -> None:
        """同一个音不允许连着出现（实测过连续 4 次同一个音）。"""
        scheduler = make_scheduler()
        keys = []
        for _ in range(24):
            decision = scheduler.next_decision()
            keys.append(decision.item_key)
            scheduler.on_result(decision, correct=True, rt_ms=1200)
        for previous, current in zip(keys, keys[1:]):
            assert previous != current, f"连续出现同一题：{current}"

    def test_requeued_item_is_locked_until_it_reappears(self) -> None:
        """已排期回炉的题，在回炉重现前不走普通通道。"""
        scheduler = make_scheduler(seed=2)
        first = scheduler.next_decision()
        scheduler.on_result(first, correct=False)

        for _ in range(12):
            decision = scheduler.next_decision()
            if decision.source != "requeue":
                assert decision.item_key != first.item_key, "等待回炉的题被普通通道抢先抽到"
            scheduler.on_result(decision, correct=True, rt_ms=1200)
            if decision.source == "requeue":
                break

    def test_no_new_items_left_uses_review_or_weak(self) -> None:
        states = {}
        for pc in range(12):
            states[key_of(pc)] = due_state(pc) if pc % 2 == 0 else weak_state(pc)
        scheduler = make_scheduler(states=states)
        for _ in range(40):
            decision = scheduler.next_decision()
            assert decision.source in ("review", "weak")

    def test_all_mastered_and_not_due_still_returns_something(self) -> None:
        from dataclasses import replace

        states = {}
        for pc in range(12):
            state = initial_state(key_of(pc), pc, "L2")
            states[key_of(pc)] = replace(
                state,
                seen_count=5,
                correct_count=5,
                interval_index=6,
                due_at=NOW + timedelta(days=30),
                last_seen_at=NOW - timedelta(days=20),
            )
        scheduler = make_scheduler(states=states)
        decision = scheduler.next_decision()
        assert decision is not None
        assert decision.pitch_class in PITCH_CLASSES


class TestRequeue:
    def test_wrong_item_comes_back_after_gap(self) -> None:
        scheduler = make_scheduler(seed=3)
        decisions: list[Decision] = []
        for index in range(20):
            decision = scheduler.next_decision()
            decisions.append(decision)
            if index == 0:
                scheduler.on_result(decision, correct=False)
            else:
                scheduler.on_result(decision, correct=True)

        requeued = [(i, d) for i, d in enumerate(decisions) if d.source == "requeue"]
        assert requeued, "答错的题必须回炉"
        first_index, first_decision = requeued[0]
        assert first_decision.item_key == decisions[0].item_key
        assert 5 <= first_index - 0 <= 8, f"回炉间隔应在 5~8 题，实际 {first_index}"

    def test_two_consecutive_corrects_clear_requeue(self) -> None:
        scheduler = make_scheduler(seed=5)
        first = scheduler.next_decision()
        scheduler.on_result(first, correct=False)
        assert scheduler.requeue_size == 1

        # 一直出到回炉题出现，并连续答对两次
        for _ in range(60):
            decision = scheduler.next_decision()
            scheduler.on_result(decision, correct=True)
            if scheduler.requeue_size == 0:
                break
        assert scheduler.requeue_size == 0, "连续答对后应移出回炉队列"

    def test_requeue_failure_reschedules(self) -> None:
        scheduler = make_scheduler(seed=7)
        first = scheduler.next_decision()
        scheduler.on_result(first, correct=False)

        # 找到回炉题并再次答错
        for _ in range(20):
            decision = scheduler.next_decision()
            if decision.source == "requeue":
                scheduler.on_result(decision, correct=False)
                break
            scheduler.on_result(decision, correct=True)
        assert scheduler.requeue_size == 1, "回炉题再错应仍在队列中"

    def test_requeue_ratio_is_limited(self) -> None:
        """回炉题总量不超过 30%。"""
        config = ScheduleConfig(requeue_max_ratio=0.2)
        scheduler = make_scheduler(seed=11, config=config)
        requeued = 0
        for index in range(60):
            decision = scheduler.next_decision()
            if decision.source == "requeue":
                requeued += 1
            scheduler.on_result(decision, correct=False)  # 一直答错 → 一直想回炉
            assert requeued <= max(2, index * config.requeue_max_ratio + 2), (
                f"第 {index} 题时回炉 {requeued} 次，超过比例限制"
            )


class TestSchedulerResults:
    def test_correct_updates_state_and_callback(self) -> None:
        changes: list[ItemState] = []
        scheduler = make_scheduler(on_state_change=changes.append)
        decision = scheduler.next_decision()
        updated = scheduler.on_result(decision, correct=True, rt_ms=1200)

        assert updated.interval_index == 1
        assert updated.due_at is not None
        assert changes and changes[-1] == updated
        assert scheduler.state_of(decision.item_key) == updated

    def test_skip_updates_with_half_lapse(self) -> None:
        scheduler = make_scheduler()
        decision = scheduler.next_decision()
        updated = scheduler.on_result(decision, correct=False, skipped=True)
        assert updated.lapses == 0.5
        assert updated.due_at is not None

    def test_timeout_updates(self) -> None:
        scheduler = make_scheduler()
        decision = scheduler.next_decision()
        updated = scheduler.on_result(decision, correct=False, timed_out=True)
        assert updated.lapses == 0.5

    def test_retry_correct_is_lapse(self) -> None:
        scheduler = make_scheduler()
        decision = scheduler.next_decision()
        scheduler.on_result(decision, correct=False)
        updated = scheduler.on_result(decision, correct=True, first_attempt=False, rt_ms=5000)
        assert updated.interval_index == 0

    def test_hinted_correct_does_not_advance(self) -> None:
        scheduler = make_scheduler()
        decision = scheduler.next_decision()
        updated = scheduler.on_result(decision, correct=True, hinted=True)
        assert updated.interval_index == 0
        assert updated.due_at is None

    def test_callback_failure_does_not_break(self) -> None:
        def broken(_state) -> None:
            raise RuntimeError("数据库炸了")

        scheduler = make_scheduler(on_state_change=broken)
        decision = scheduler.next_decision()
        updated = scheduler.on_result(decision, correct=True)
        assert updated.interval_index == 1

    def test_stats_track_sources(self) -> None:
        scheduler = make_scheduler()
        for _ in range(5):
            decision = scheduler.next_decision()
            scheduler.on_result(decision, correct=True)
        assert scheduler.stats.asked == 5
        assert scheduler.stats.count("new") == 5


class TestCrossDayReview:
    """端到端：今天练过的题，明天/后天真的会作为"到期复习"排进来。

    这是记忆曲线最核心的承诺，必须能被验证，而不只是"公式看起来对"。
    """

    def test_answered_items_come_back_as_review_next_day(self) -> None:
        # 第一天：12 个音全部答对一次
        day1 = NOW
        scheduler = make_scheduler(now=day1, seed=21)
        answered: list[str] = []
        for _ in range(12):
            decision = scheduler.next_decision()
            scheduler.on_result(decision, correct=True, rt_ms=1200)
            answered.append(decision.item_key)

        assert len(set(answered)) == 12
        for key in answered:
            state = scheduler.state_of(key)
            assert state is not None
            assert state.interval_index == 1
            assert state.due_at == day1 + timedelta(days=INTERVALS_DAYS[1])

        # 同一天再练：没有到期项，应全部是新题（已练过的 12 个都不到期）
        same_day = make_scheduler(states=dict(scheduler.states), now=day1, seed=22)
        assert same_day.due_items() == []

        # 第二天：全部到期；此时没有新题也没有薄弱项，
        # 配额顺延给到期复习（需求 FR-710 的"某类无可用项时配额顺延"）
        day2 = day1 + timedelta(days=1, hours=1)
        later = make_scheduler(states=dict(scheduler.states), now=day2, seed=23)
        assert len(later.due_items()) == 12

        sources = [later.next_decision().source for _ in range(10)]
        assert set(sources) == {"review"}, "只有到期项时应全部出复习题（否则无题可出）"

    def test_review_quota_respected_when_new_items_available(self) -> None:
        """有到期项也有新题时，配额才真正生效：每 10 题最多 4 题复习。"""
        day1 = NOW
        scheduler = make_scheduler(now=day1, seed=24)
        # 只练 6 个音 → 另外 6 个仍是新题
        for _ in range(6):
            decision = scheduler.next_decision()
            scheduler.on_result(decision, correct=True, rt_ms=1200)

        day2 = day1 + timedelta(days=1, hours=1)
        later = make_scheduler(states=dict(scheduler.states), now=day2, seed=25)
        assert len(later.due_items()) == 6
        assert len(later.new_items()) == 6

        sources = [later.next_decision().source for _ in range(10)]
        assert sources.count("review") == 4, "到期复习占满配额 4 题"
        assert sources.count("new") == 6, "其余配额顺延给新题"

    def test_overdue_items_sorted_by_lateness(self) -> None:
        day1 = NOW
        scheduler = make_scheduler(now=day1, seed=31)
        decisions = [scheduler.next_decision() for _ in range(3)]
        for index, decision in enumerate(decisions):
            scheduler.on_result(decision, correct=True, rt_ms=1000)
            # 把三个训练项伪造成不同逾期天数
            state = scheduler.states[decision.item_key]
            from dataclasses import replace

            scheduler.states[decision.item_key] = replace(
                state, due_at=day1 - timedelta(days=(index + 1) * 3)
            )

        later = make_scheduler(states=dict(scheduler.states), now=day1, seed=32)
        order = [later.next_decision().pitch_class for _ in range(3)]
        # 逾期最久的那个（index=2）应该最先出
        assert order[0] == decisions[2].pitch_class

    def test_interval_grows_across_days(self) -> None:
        """连续几天都答对：间隔应按 1 → 3 → 7 天增长。"""
        key = key_of(4)
        states: dict[str, ItemState] = {}
        moment = NOW
        for days in (1, 3, 7):
            # 用"只有一个音"的题库，确保每轮出的都是它
            scheduler = QuestionScheduler(
                level_id="L2",
                pitch_classes=[4],
                item_key_of=key_of,
                states=dict(states),
                rng=random.Random(41),
                clock=lambda m=moment: m,
            )
            decision = scheduler.next_decision()
            assert decision.item_key == key
            scheduler.on_result(decision, correct=True, rt_ms=1000)
            states = dict(scheduler.states)
            assert states[key].interval_days == days
            assert states[key].due_at is not None
            moment = states[key].due_at  # 到期日再练


class TestAcceptanceCriterion13:
    """验收标准 §10-13：**把系统时间向后调 3 天后启动，能正确产生"到期复习项"**。

    这里走完整链路：练一天 → 训练项落库 → 换个时间"重新启动" → 从库里读回来调度。
    """

    def test_three_days_later_produces_due_reviews(self, tmp_path) -> None:
        from jitatrainer.data.db import Database
        from jitatrainer.data.repository import DbItemStore

        database = Database(tmp_path / "acceptance.db")
        database.initialize()
        with database.connect() as conn:
            profile_id = int(database.list_profiles(conn)[0]["id"])

        day1 = NOW
        store = DbItemStore(database, profile_id, "pitch_find")
        store.conn.close()  # 用独立连接，模拟不同次启动

        # 第一天：练 12 个音并全部答对
        first_run = QuestionScheduler(
            level_id="L1",
            pitch_classes=list(range(12)),
            item_key_of=lambda pc: key_of(pc, "L1"),
            states={},
            rng=random.Random(101),
            clock=lambda: day1,
        )
        persist = DbItemStore(database, profile_id, "pitch_find")
        for _ in range(12):
            decision = first_run.next_decision()
            state = first_run.on_result(decision, correct=True, rt_ms=1200)
            persist.save(state)
        persist.close()

        # 第二天：不到期，全是新题/复习之外的来源
        day2 = day1 + timedelta(days=1, hours=2)
        second_store = DbItemStore(database, profile_id, "pitch_find")
        day2_states = second_store.load("L1")
        assert len(day2_states) == 12, "训练项应从数据库读回来"
        day2_scheduler = QuestionScheduler(
            level_id="L1",
            pitch_classes=list(range(12)),
            item_key_of=lambda pc: key_of(pc, "L1"),
            states=day2_states,
            rng=random.Random(102),
            clock=lambda: day2,
        )
        assert len(day2_scheduler.due_items()) == 12, "第二天应全部到期"

        # 第三天：把时间往后调 3 天再"启动"，必须有到期复习项
        day4 = day1 + timedelta(days=3)
        third_store = DbItemStore(database, profile_id, "pitch_find")
        states = third_store.load("L1")
        third_store.close()
        scheduler = QuestionScheduler(
            level_id="L1",
            pitch_classes=list(range(12)),
            item_key_of=lambda pc: key_of(pc, "L1"),
            states=states,
            rng=random.Random(103),
            clock=lambda: day4,
        )

        due = scheduler.due_items()
        assert due, "向后调 3 天后必须产生到期复习项"
        assert len(due) == 12
        assert all(state.overdue_days(day4) > 0 for state in due)

        # 启动后的第一批题应当是复习题
        sources = [scheduler.next_decision().source for _ in range(4)]
        assert sources.count("review") == 4, f"首批应优先出复习题，实际 {sources}"

    def test_future_due_items_are_not_yet_due(self, tmp_path) -> None:
        """反向验证：间隔没到就不该被当成到期项（否则等于缓存失效）。"""
        from jitatrainer.data.db import Database

        database = Database(tmp_path / "future.db")
        database.initialize()
        with database.connect() as conn:
            profile_id = int(database.list_profiles(conn)[0]["id"])

        scheduler = QuestionScheduler(
            level_id="L1",
            pitch_classes=[4],
            item_key_of=lambda pc: key_of(pc, "L1"),
            rng=random.Random(7),
            clock=lambda: NOW,
        )
        decision = scheduler.next_decision()
        state = scheduler.on_result(decision, correct=True, rt_ms=1000)
        assert state.due_at is not None

        just_before = state.due_at - timedelta(minutes=1)
        earlier = QuestionScheduler(
            level_id="L1",
            pitch_classes=[4],
            item_key_of=lambda pc: key_of(pc, "L1"),
            states={state.item_key: state},
            rng=random.Random(7),
            clock=lambda: just_before,
        )
        assert earlier.due_items() == []


class TestAdvice:
    def _records(self, count: int, accuracy: float, rt: int | None, asked: int = 25) -> list[SessionRecord]:
        return [SessionRecord(asked=asked, accuracy_first=accuracy, avg_rt_ms=rt) for _ in range(count)]

    def test_advises_when_ready(self) -> None:
        assert should_advance(self._records(3, 0.95, 1800))

    def test_requires_three_sessions(self) -> None:
        assert not should_advance(self._records(2, 0.99, 1500))

    def test_requires_enough_answered(self) -> None:
        assert not should_advance(self._records(3, 0.99, 1500, asked=10))

    def test_rejects_low_accuracy(self) -> None:
        records = self._records(3, 0.95, 1800)
        assert not should_advance([SessionRecord(15, 0.6, 1800), *records])

    def test_rejects_slow_reaction(self) -> None:
        records = self._records(3, 0.95, 4000)
        assert not should_advance(records)

    def test_missing_rt_is_tolerated(self) -> None:
        assert should_advance(self._records(3, 0.95, None))

    def test_custom_config(self) -> None:
        config = AdviceConfig(recent_sessions=2, min_accuracy=0.8, max_avg_rt_ms=5000, min_answered=20)
        assert should_advance(self._records(2, 0.85, 4000, asked=15), config)
