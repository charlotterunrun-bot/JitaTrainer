"""开发期启动入口。

用法：
    python run.py

它负责把 .vendor 与 src 加入 sys.path，然后启动图形界面。
打包后由 PyInstaller 直接调用 jitatrainer.app:main，不经过本文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for extra in (ROOT / ".vendor", ROOT / "src"):
    if extra.is_dir() and str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from jitatrainer import compat  # noqa: E402

compat.install()

from jitatrainer.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
