"""设置存储：类型化读写 + 全局/档案两级作用域。

约定：
  - **全局设置**（音频设备、语言、主题、向导状态）对所有档案生效；
  - **档案设置**（难度、判定参数、练习偏好）按档案保存。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import Database

#: 全局作用域的键；其余键默认按档案存储
GLOBAL_KEYS = frozenset(
    {
        "language",
        "theme",
        "input_device_index",
        "samplerate",
        "noise_floor_db",
        "gate_offset_db",
        "calibration_offset_cents",
        "wizard_completed",
        "last_profile_id",
    }
)

DEFAULTS: dict[str, str] = {
    # 全局
    "language": "zh_CN",
    "theme": "dark",
    "input_device_index": "",
    "samplerate": "48000",
    "noise_floor_db": "-60.0",
    "gate_offset_db": "12.0",
    "calibration_offset_cents": "0.0",
    "wizard_completed": "false",
    "last_profile_id": "1",
    # 档案
    "stable_preset": "balanced",
    "stable_ms_custom": "",
    "tolerance_cents": "25.0",
    "grace_ms": "2000",
    "timeout_ms": "8000",
    "retry_mode": "grace",
    "confidence_min": "0.80",
    "level_id": "L1",
    "include_accidentals": "false",
    "show_note_name": "false",
    "show_pitch_meter": "true",
    "scoring_enabled": "true",
}

_TRUE = {"1", "true", "yes", "on", "是"}


class Settings:
    """设置访问器。"""

    def __init__(self, db: Database, conn: sqlite3.Connection, profile_id: int | None = None) -> None:
        self.db = db
        self.conn = conn
        self.profile_id = profile_id

    # ------------------------------------------------------------------ 读
    def get(self, key: str, default: str | None = None) -> str:
        """读取设置。

        ``default=None``（默认）表示"没有存储值就用 DEFAULTS 里的默认值"；
        显式传入 default 才会覆盖 DEFAULTS。类型化 getter 依赖这个语义，
        因此它们一律不传 default。
        """
        fallback = default if default is not None else DEFAULTS.get(key, "")
        if key in GLOBAL_KEYS:
            value = self.db.get_app_setting(self.conn, key)
        else:
            value = (
                self.db.get_profile_setting(self.conn, self.profile_id, key)
                if self.profile_id is not None
                else None
            )
        return fallback if value is None else value

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(float(self.get(key)))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key))
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        raw = self.get(key) if key in DEFAULTS else ("true" if default else "false")
        return raw.strip().lower() in _TRUE

    def get_optional_int(self, key: str) -> int | None:
        raw = self.get(key).strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.get(key, "")
        if not raw:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    def as_dict(self, keys: list[str] | None = None) -> dict[str, str]:
        return {key: self.get(key) for key in (keys or DEFAULTS)}

    # ------------------------------------------------------------------ 写
    def set(self, key: str, value: Any) -> None:
        text = _to_text(value)
        if key in GLOBAL_KEYS:
            self.db.set_app_setting(self.conn, key, text)
        elif self.profile_id is not None:
            self.db.set_profile_setting(self.conn, self.profile_id, key, text)

    def update(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            self.set(key, value)

    # ------------------------------------------------------------------ 便捷派生
    def stable_ms(self) -> int:
        """稳定时长（毫秒）：优先自定义，否则用预设。"""
        from ..core.judge.base import DEFAULT_STABLE_PRESET, STABLE_PRESETS

        custom = self.get_optional_int("stable_ms_custom")
        if custom:
            return max(150, min(600, custom))
        preset = self.get("stable_preset", DEFAULT_STABLE_PRESET)
        return STABLE_PRESETS.get(preset, STABLE_PRESETS[DEFAULT_STABLE_PRESET])


def _to_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
