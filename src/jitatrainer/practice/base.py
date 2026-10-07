"""练习模块接口与公共数据结构。

设计要点（技术方案 §8）：

- **判定策略属于模块**：``make_judge_config`` 决定怎么判，因此未来做
  「音名+八度」「指定弦位」只需换模块里的实现，音频层与调度层不动；
- **渲染与逻辑分离**：模块只产出 ``RenderSpec``（数据），由 UI 的通用控件绘制，
  未来加五线谱/和弦图是加控件，不是改模块；
- **模块不依赖 PySide6**，可独立单元测试。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..core.judge.pitch_class_judge import JudgeConfig
from ..core.theory.fretboard import Position

#: 题目来源标签（调度层用；M2 先用 new，M3 接入复习/强化/回炉）
SOURCE_NEW = "new"
SOURCE_REVIEW = "review"
SOURCE_WEAK = "weak"
SOURCE_REQUEUE = "requeue"
SOURCE_IDS = (SOURCE_NEW, SOURCE_REVIEW, SOURCE_WEAK, SOURCE_REQUEUE)


@dataclass(frozen=True, slots=True)
class Question:
    """一道题。"""

    module_id: str
    item_key: str
    target_pc: int
    position: Position
    level_id: str
    source: str = SOURCE_NEW

    @property
    def target_label(self) -> str:
        from ..core.theory.notes import pitch_class_name

        return pitch_class_name(self.target_pc)


@dataclass(frozen=True, slots=True)
class RenderSpec:
    """谱面渲染描述（由 UI 通用控件解释）。"""

    kind: str
    position: Position
    label_zh: str
    label_en: str
    note_name: str
    show_note_name: bool = False
    highlight: bool = False


@dataclass
class QuestionContext:
    """出题上下文。"""

    profile_id: int | None = None
    level_id: str = "L1"
    include_accidentals: bool = False
    #: 调度层指定要出的音名（M3 的记忆曲线用）；None 表示由模块自行选择
    requested_pc: int | None = None
    source: str = SOURCE_NEW
    #: 最近出过的位置，用于避免连续重复
    recent_positions: deque[tuple[int, int]] = field(default_factory=lambda: deque(maxlen=8))
    #: 最近出过的音名，用于避免同一音名连出多次
    recent_pitch_classes: deque[int] = field(default_factory=lambda: deque(maxlen=3))


@runtime_checkable
class PracticeModule(Protocol):
    """练习模块必须实现的接口。"""

    id: str
    version: str
    #: {"zh_CN": 名称, "en_US": name}
    name: dict[str, str]
    description: dict[str, str]

    def default_config(self) -> dict: ...

    def generate(self, ctx: QuestionContext) -> Question: ...

    def render_spec(self, question: Question, *, show_note_name: bool = False) -> RenderSpec: ...

    def make_judge_config(self, question: Question, settings) -> JudgeConfig: ...  # noqa: ANN001

    def label(self, language: str) -> str: ...

    def describe(self, language: str) -> str: ...
