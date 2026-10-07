"""数据库迁移定义。

说明：迁移用 Python 模块而不是独立 .sql 文件，是为了让 PyInstaller 打包时
无需额外收集数据文件、也不会因为路径问题在绿色版里找不到迁移脚本。
每个迁移只包含纯 SQL，保持可审计。
"""

from __future__ import annotations

from dataclasses import dataclass

from .v001_initial import VERSION as V001_VERSION, SQL as V001_SQL


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    description: str
    sql: str


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=V001_VERSION, description="初始结构：档案/设置/训练项/会话/答题/事件", sql=V001_SQL),
)

LATEST_VERSION = max(m.version for m in MIGRATIONS)
