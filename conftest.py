"""pytest 全局配置。

作用：
  1. 把 .vendor（工作区内的依赖目录）与 src 加入 sys.path；
  2. 尽早导入 compat，修补本机 tempfile.mkdtemp 的目录权限问题；
  3. 把 pytest 的临时目录固定到工作区内，避免落到不可写的系统临时目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for extra in (ROOT / ".vendor", ROOT / "src"):
    if extra.is_dir() and str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

# 必须在任何可能使用临时目录的库之前导入
try:
    from jitatrainer import compat  # noqa: F401

    compat.install()
except ImportError:  # 包尚未就绪时不应阻塞测试收集
    pass

# 本机环境修复：pytest 自带的 basetemp / tmp_path 机制会创建 0o700 目录并在
# 会话结束时对其执行清理，这两步在本机（Windows + DSH 沙箱）都会因目录权限
# 模型而失败。因此这里**接管 tmp_path 夹具**，把临时目录固定在工作区内、
# 自己创建、自己管理，完全不触发 pytest 的临时目录回收逻辑。
import itertools  # noqa: E402
import re  # noqa: E402

import pytest  # noqa: E402

_TMP_ROOT = ROOT / ".tmp" / "tests"
_counter = itertools.count()


@pytest.fixture(scope="session", autouse=True)
def _prepare_test_tmp_root():
    import shutil

    shutil.rmtree(_TMP_ROOT, ignore_errors=True)
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture
def tmp_path(request):
    """替代 pytest 内建的 tmp_path：目录固定在工作区内，权限正常。"""
    safe = re.sub(r"[^A-Za-z0-9_.\-]+", "_", request.node.nodeid)[:70]
    path = _TMP_ROOT / f"{next(_counter):03d}-{safe}"
    path.mkdir(parents=True, exist_ok=True)
    return path
