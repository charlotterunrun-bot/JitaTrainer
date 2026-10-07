"""判定层公共定义。"""

from __future__ import annotations

from dataclasses import dataclass

#: 判定结果
RESULT_CORRECT = "correct"
RESULT_WRONG = "wrong"
RESULT_TIMEOUT = "timeout"

#: 试音处理模式（需求 FR-535）
MODE_GRACE = "grace"  # 宽容期模式：期内试音不判错
MODE_STRICT = "strict"  # 严格模式：立即判定
MODE_LENIENT = "lenient"  # 宽容模式：全程不判错，只记录用时

MODE_IDS = (MODE_GRACE, MODE_STRICT, MODE_LENIENT)

#: 稳定时长三档预设（CR-001，单位毫秒）
STABLE_PRESETS: dict[str, int] = {
    "fast": 150,
    "balanced": 200,
    "robust": 300,
}
DEFAULT_STABLE_PRESET = "balanced"

#: 各档位的预计端到端延迟（毫秒），用于设置页向用户展示权衡
PRESET_LATENCY_MS: dict[str, int] = {
    "fast": 243,
    "balanced": 293,
    "robust": 393,
}


@dataclass(frozen=True, slots=True)
class JudgeOutcome:
    """一次判定的结论。"""

    result: str
    detected_pc: int | None = None
    detected_hz: float | None = None
    cents: float | None = None
    elapsed_ms: float = 0.0
    attempt_index: int = 1
    #: 是否应当向用户反馈（宽容模式下错误不打断用户）
    feedback: bool = True

    @property
    def is_correct(self) -> bool:
        return self.result == RESULT_CORRECT
