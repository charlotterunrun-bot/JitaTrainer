"""模块一「看谱找音」（Note Finding）。

玩法：屏幕用六线谱给出一个音符（标出弦与品），用户在吉他上弹出**正确音名**，
麦克风听音判定；答对出下一题，答错停留直到弹对。

判定只认音名（需求 FR-531）：弹第 3 弦第 7 品与第 5 弦第 2 品都算 B。
"""

from __future__ import annotations

import random
from collections import deque

from ..base import (
    SOURCE_NEW,
    Question,
    QuestionContext,
    RenderSpec,
)
from ..registry import register_module
from ...core.judge.pitch_class_judge import JudgeConfig
from ...core.theory.fretboard import Position, level_pitch_classes, positions_for_pitch_class
from ...core.theory.levels import DEFAULT_LEVEL_ID, get_level
from ...core.theory.notes import NOTE_NAMES, pitch_class_name

MODULE_ID = "pitch_find"

#: 同一音名不允许连续出现的次数
MAX_SAME_PITCH_CLASS_RUN = 2


def item_key(pitch_class: int, level_id: str) -> str:
    """训练项键 = 音名 × 把位区间（需求 FR-610）。"""
    return f"note={NOTE_NAMES[pitch_class % 12]}|level={level_id}"


@register_module
class PitchFindModule:
    """看谱找音模块。"""

    id = MODULE_ID
    version = "1.0"
    name = {"zh_CN": "看谱找音", "en_US": "Note Finding"}
    description = {
        "zh_CN": "屏幕给出六线谱音符，你在吉他上弹出正确音高",
        "en_US": "Read the TAB on screen and play the note on your guitar",
    }

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()

    # ------------------------------------------------------------------ 元信息
    def label(self, language: str) -> str:
        return self.name.get(language) or self.name["zh_CN"]

    def describe(self, language: str) -> str:
        return self.description.get(language) or self.description["zh_CN"]

    def default_config(self) -> dict:
        return {"level_id": DEFAULT_LEVEL_ID, "include_accidentals": False}

    # ------------------------------------------------------------------ 出题
    def candidate_pitch_classes(self, level_id: str, include_accidentals: bool) -> tuple[int, ...]:
        return level_pitch_classes(level_id, include_accidentals)

    def _choose_pitch_class(self, ctx: QuestionContext, candidates: tuple[int, ...]) -> int:
        if ctx.requested_pc is not None:
            return ctx.requested_pc % 12

        recent = list(ctx.recent_pitch_classes)
        # 若候选音名已被同一音名连出多次，则从候选中排除它
        blocked: set[int] = set()
        if len(recent) >= MAX_SAME_PITCH_CLASS_RUN and len(set(recent[-MAX_SAME_PITCH_CLASS_RUN:])) == 1:
            blocked = {recent[-1]}

        pool = [pc for pc in candidates if pc not in blocked] or list(candidates)
        return self.rng.choice(pool)

    def _choose_position(self, level_id: str, pitch_class: int, ctx: QuestionContext) -> Position:
        positions = positions_for_pitch_class(pitch_class, level_id)
        if not positions:
            raise ValueError(f"{level_id} 中没有音名 {pitch_class_name(pitch_class)} 的位置")

        recent = set(ctx.recent_positions)
        fresh = [pos for pos in positions if (pos.string, pos.fret) not in recent]
        pool = fresh or list(positions)
        return self.rng.choice(pool)

    def generate(self, ctx: QuestionContext) -> Question:
        level = get_level(ctx.level_id)
        candidates = self.candidate_pitch_classes(level.id, ctx.include_accidentals)
        if not candidates:
            raise ValueError(f"{level.id} 没有可出的音名")

        pitch_class = self._choose_pitch_class(ctx, candidates)
        position = self._choose_position(level.id, pitch_class, ctx)

        ctx.recent_positions.append((position.string, position.fret))
        ctx.recent_pitch_classes.append(pitch_class)

        return Question(
            module_id=self.id,
            item_key=item_key(pitch_class, level.id),
            target_pc=pitch_class,
            position=position,
            level_id=level.id,
            source=ctx.source or SOURCE_NEW,
        )

    # ------------------------------------------------------------------ 渲染
    def render_spec(self, question: Question, *, show_note_name: bool = False) -> RenderSpec:
        return RenderSpec(
            kind="tab",
            position=question.position,
            label_zh=question.position.label_zh(),
            label_en=question.position.label_en(),
            note_name=question.position.note_name,
            show_note_name=show_note_name,
        )

    # ------------------------------------------------------------------ 判定
    def make_judge_config(self, question: Question, settings) -> JudgeConfig:  # noqa: ANN001
        """按设置构造判定配置（判定只认音名，与八度/弦/品无关）。"""
        return JudgeConfig(
            target_pc=question.target_pc,
            tolerance_cents=settings.get_float("tolerance_cents", 25.0),
            stable_ms=settings.stable_ms(),
            grace_ms=settings.get_int("grace_ms", 2000),
            timeout_ms=settings.get_int("timeout_ms", 8000),
            confidence_min=settings.get_float("confidence_min", 0.80),
            mode=settings.get("retry_mode", "grace"),
        )


def make_context(
    *,
    profile_id: int | None = None,
    level_id: str = DEFAULT_LEVEL_ID,
    include_accidentals: bool = False,
    requested_pc: int | None = None,
    source: str = SOURCE_NEW,
) -> QuestionContext:
    """构建出题上下文（便利函数，供 UI 与会话控制器使用）。"""
    return QuestionContext(
        profile_id=profile_id,
        level_id=level_id,
        include_accidentals=include_accidentals,
        requested_pc=requested_pc,
        source=source,
        recent_positions=deque(maxlen=8),
        recent_pitch_classes=deque(maxlen=3),
    )
