"""应用路径解析：绿色版把全部数据放在程序目录内。"""

from __future__ import annotations

import sys
from pathlib import Path

APP_DIR_NAME = "JitaTrainer"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物中。"""
    return getattr(sys, "frozen", False)


def app_root() -> Path:
    """程序根目录。

    - 打包后：exe 所在目录（绿色版：数据就在旁边）
    - 开发时：仓库根目录
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def bundled_root() -> Path:
    """只读资源根目录（i18n、assets）。

    - 打包后：PyInstaller 的 _MEIPASS（onedir 模式下即 _internal 目录）
    - 开发时：与 app_root 相同
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_root()))
    return app_root()


def data_dir() -> Path:
    return app_root() / "data"


def logs_dir() -> Path:
    return app_root() / "logs"


def backups_dir() -> Path:
    return data_dir() / "backups"


def exports_dir() -> Path:
    return data_dir() / "exports"


def db_path() -> Path:
    return data_dir() / "jitatrainer.db"


def i18n_dir() -> Path:
    for candidate in (bundled_root() / "i18n", bundled_root() / "src" / "jitatrainer" / "i18n"):
        if candidate.is_dir():
            return candidate
    return bundled_root() / "i18n"


def ensure_dirs() -> None:
    """创建运行期目录（幂等）。"""
    for path in (data_dir(), logs_dir(), backups_dir(), exports_dir()):
        path.mkdir(parents=True, exist_ok=True)
