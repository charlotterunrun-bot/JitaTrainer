"""环境兼容层。

本机（Windows + DSH 沙箱）存在一个已验证的硬约束：

    os.mkdir(path, 0o700) 与 tempfile.mkdtemp() 创建的目录**不可写**，
    连 icacls 都无法处理其 ACL；而 0o755 / 0o777 创建的目录完全正常。

后果：pip（内部用 mkdtemp）无法安装任何包；pytest 的临时目录、
任何使用临时目录的第三方库也会失败。

本模块在启动时探测该问题，并只在确实存在问题时把 tempfile.mkdtemp
替换为"用 0o777 创建目录"的等价实现，保证行为与标准库一致（除权限位外）。
"""

from __future__ import annotations

import os
import tempfile

_ORIGINAL_MKDTEMP = tempfile.mkdtemp
_installed = False


def _probe() -> bool:
    """返回 True 表示标准库的 mkdtemp 在本机不可用。"""
    try:
        path = _ORIGINAL_MKDTEMP(prefix="jita-probe-")
    except Exception:  # noqa: BLE001
        return True
    try:
        probe_file = os.path.join(path, "probe")
        with open(probe_file, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe_file)
        return False
    except OSError:
        return True
    finally:
        try:
            os.rmdir(path)
        except OSError:
            pass


def _patched_mkdtemp(suffix: str | None = None, prefix: str | None = None, dir: str | None = None) -> str:
    """与 tempfile.mkdtemp 等价，但目录权限用 0o777。"""
    if dir is None:
        dir = tempfile.gettempdir()
    if prefix is None:
        prefix = tempfile.gettempprefix()
    if suffix is None:
        suffix = ""

    names = tempfile._get_candidate_names()
    for _ in range(tempfile.TMP_MAX):
        path = os.path.join(dir, prefix + next(names) + suffix)
        try:
            os.mkdir(path, 0o777)
        except FileExistsError:
            continue
        return os.path.abspath(path)
    raise FileExistsError(f"在 {dir} 下无法创建唯一的临时目录")


def install(force: bool = False) -> bool:
    """按需安装修补。返回是否发生了修补。"""
    global _installed
    if _installed and not force:
        return True
    if not force and not _probe():
        return False
    tempfile.mkdtemp = _patched_mkdtemp  # type: ignore[assignment]
    _installed = True
    return True


def make_writable_dir(path: str) -> str:
    """创建一个可写目录（已存在则直接复用），返回路径。"""
    os.makedirs(path, mode=0o777, exist_ok=True)
    return path
