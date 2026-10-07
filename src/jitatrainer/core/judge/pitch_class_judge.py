"""音名判定：滑动窗口 + 多数表决 + 稳定时长确认。

实现依据 M0 实测（技术方案 §6.7 / §7）：

  - **不能**要求"连续 N 帧都合格"：20dB 噪声下会因单帧置信度抖动而全灭（实测 0/7）
  - 正确做法是在最近 N 帧的滑动窗口内做多数表决（实测 7/7）
  - 单帧置信度下限 0.80，用于拦掉窗口不足时的错误解

判定只认音名（pitch class），与八度、弦、品无关。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace

from ...core.audio.events import PitchEvent
from ...core.theory.notes import nearest_cents_offset
from .base import (
    DEFAULT_STABLE_PRESET,
    MODE_GRACE,
    MODE_IDS,
    MODE_LENIENT,
    MODE_STRICT,
    RESULT_CORRECT,
    RESULT_TIMEOUT,
    RESULT_WRONG,
    STABLE_PRESETS,
    JudgeOutcome,
)


@dataclass(frozen=True, slots=True)
class JudgeConfig:
    """一题的判定配置。"""

    target_pc: int
    tolerance_cents: float = 25.0
    stable_ms: int = STABLE_PRESETS[DEFAULT_STABLE_PRESET]
    grace_ms: int = 2000
    timeout_ms: int = 8000
    confidence_min: float = 0.80
    vote_ratio: float = 0.70
    min_valid_ratio: float = 0.40
    hop_ms: float = 10.0
    mode: str = MODE_GRACE

    @classmethod
    def from_preset(cls, preset: str, **kwargs) -> JudgeConfig:
        """按三档预设构造配置（fast / balanced / robust）。"""
        if preset not in STABLE_PRESETS:
            raise KeyError(f"未知稳定时长预设：{preset}")
        return cls(stable_ms=STABLE_PRESETS[preset], **kwargs)

    @property
    def needed_frames(self) -> int:
        """滑动窗口长度（帧数）。"""
        return max(1, int(round(self.stable_ms / self.hop_ms)))

    @property
    def effective_grace_ms(self) -> int:
        """按模式返回实际宽容期：严格模式与宽容模式都没有宽容期。"""
        if self.mode in (MODE_STRICT, MODE_LENIENT):
            return 0
        return self.grace_ms

    def validate(self) -> None:
        if self.mode not in MODE_IDS:
            raise ValueError(f"未知试音模式：{self.mode}")
        if not 0 <= self.target_pc <= 11:
            raise ValueError("目标音名必须在 0..11 之间")
        if self.tolerance_cents <= 0:
            raise ValueError("容差必须为正")
        if not 0.0 < self.vote_ratio <= 1.0:
            raise ValueError("投票比例必须在 (0, 1] 区间")
        if self.stable_ms <= 0 or self.hop_ms <= 0:
            raise ValueError("稳定时长与帧移必须为正")


@dataclass(frozen=True, slots=True)
class _Reading:
    """一帧的读数；``pc`` 为 None 表示该帧无效。"""

    pc: int | None
    cents: float | None
    hz: float | None


class PitchClassJudge:
    """一题的判定状态机。

    用法::

        judge = PitchClassJudge(JudgeConfig(target_pc=4), started_at=now)
        for event in pitch_events:
            outcome = judge.feed(event)
            if outcome is not None:
                ...  # 正确 / 错误 / 超时
    """

    def __init__(self, config: JudgeConfig, started_at: float) -> None:
        config.validate()
        self.config = config
        self.started_at = started_at
        self._window: deque[_Reading] = deque(maxlen=config.needed_frames)
        self._grace_end = started_at + config.effective_grace_ms / 1000.0
        self._last_signal = self._grace_end
        self._attempt = 1
        self._decided = False

    # ------------------------------------------------------------------ 内部
    def _to_reading(self, event: PitchEvent) -> _Reading:
        if not event.valid or event.confidence < self.config.confidence_min:
            return _Reading(None, None, None)
        pc = event.pitch_class
        if pc is None:
            return _Reading(None, None, None)
        return _Reading(pc, nearest_cents_offset(event.hz, pc), event.hz)

    def _decide(self, now: float) -> JudgeOutcome | None:
        window = list(self._window)
        if len(window) < self.config.needed_frames:
            return None

        valid = [r for r in window if r.pc is not None]
        min_valid = max(3, int(self.config.min_valid_ratio * self.config.needed_frames))
        if len(valid) < min_valid:
            return None

        counts: dict[int, int] = {}
        for reading in valid:
            assert reading.pc is not None
            counts[reading.pc] = counts.get(reading.pc, 0) + 1
        mode_pc, mode_count = max(counts.items(), key=lambda kv: kv[1])
        if mode_count < self.config.vote_ratio * len(valid):
            return None

        mode_readings = [r for r in valid if r.pc == mode_pc]
        cents_values = sorted(r.cents for r in mode_readings if r.cents is not None)
        hz_values = sorted(r.hz for r in mode_readings if r.hz is not None)
        typical_cents = cents_values[len(cents_values) // 2] if cents_values else None
        typical_hz = hz_values[len(hz_values) // 2] if hz_values else None
        in_tune = sum(
            1
            for r in mode_readings
            if r.cents is not None and abs(r.cents) <= self.config.tolerance_cents
        )

        elapsed_ms = (now - self.started_at) * 1000.0

        if mode_pc == self.config.target_pc and in_tune >= self.config.vote_ratio * len(valid):
            self._decided = True
            return JudgeOutcome(
                result=RESULT_CORRECT,
                detected_pc=mode_pc,
                detected_hz=typical_hz,
                cents=typical_cents,
                elapsed_ms=elapsed_ms,
                attempt_index=self._attempt,
                feedback=True,
            )

        # 判错：清空窗口，等待用户改正（需求 FR-537：停留直到弹对）
        self._window.clear()
        self._last_signal = now
        outcome = JudgeOutcome(
            result=RESULT_WRONG,
            detected_pc=mode_pc,
            detected_hz=typical_hz,
            cents=typical_cents,
            elapsed_ms=elapsed_ms,
            attempt_index=self._attempt,
            feedback=self.config.mode != MODE_LENIENT,
        )
        self._attempt += 1
        return outcome

    # ------------------------------------------------------------------ 对外
    def feed(self, event: PitchEvent) -> JudgeOutcome | None:
        """送入一帧检测事件；返回判定结论或 None（继续等待）。"""
        if self._decided:
            return None

        if event.t < self._grace_end:
            return None  # 宽容期：只观察，不判定

        reading = self._to_reading(event)
        self._window.append(reading)
        if reading.pc is not None:
            self._last_signal = event.t

        outcome = self._decide(event.t)
        if outcome is not None:
            return outcome

        deadline_base = max(self._grace_end, self._last_signal)
        if (event.t - deadline_base) * 1000.0 >= self.config.timeout_ms:
            self._decided = True
            return JudgeOutcome(
                result=RESULT_TIMEOUT,
                elapsed_ms=(event.t - self.started_at) * 1000.0,
                attempt_index=self._attempt,
                feedback=True,
            )
        return None

    @property
    def attempt_index(self) -> int:
        return self._attempt

    @property
    def finished(self) -> bool:
        return self._decided


def with_mode(config: JudgeConfig, mode: str) -> JudgeConfig:
    """基于既有配置切换试音模式。"""
    return replace(config, mode=mode)
