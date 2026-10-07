"""练习模块注册表。

新增模块 = 新增一个包 + 一行 ``@register_module``，主程序不需要改动
（需求里"可以不断扩展"的落地方式）。
"""

from __future__ import annotations

from collections.abc import Callable

from .base import PracticeModule

_REGISTRY: dict[str, PracticeModule] = {}
_BUILTIN_LOADED = False


def register_module(cls: type | PracticeModule) -> type | PracticeModule:
    """注册模块类：实例化后放入注册表（可用作装饰器）。"""
    instance = cls() if isinstance(cls, type) else cls
    module_id = getattr(instance, "id", None)
    if not module_id:
        raise ValueError("练习模块必须定义 id")
    if module_id in _REGISTRY:
        raise ValueError(f"练习模块 id 重复：{module_id}")
    _REGISTRY[module_id] = instance
    return cls


def all_modules() -> list[PracticeModule]:
    load_builtin_modules()
    return list(_REGISTRY.values())


def get_module(module_id: str) -> PracticeModule:
    load_builtin_modules()
    try:
        return _REGISTRY[module_id]
    except KeyError as exc:
        raise KeyError(f"未注册的练习模块：{module_id}") from exc


def available_module_ids() -> list[str]:
    return [module.id for module in all_modules()]


def clear_registry() -> None:
    """仅用于测试。"""
    global _BUILTIN_LOADED
    _REGISTRY.clear()
    _BUILTIN_LOADED = False


def load_builtin_modules() -> None:
    """导入内置模块，触发它们的注册。"""
    global _BUILTIN_LOADED
    if _BUILTIN_LOADED:
        return
    _BUILTIN_LOADED = True
    from . import pitch_find  # noqa: F401  导入即注册


def factory(module_id: str) -> Callable[[], PracticeModule]:
    """返回一个可以重复构造的工厂（便于测试隔离）。"""

    def _make() -> PracticeModule:
        return get_module(module_id)

    return _make
