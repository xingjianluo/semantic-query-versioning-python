"""本地内存数据集：按列名存行，行数据随版本快照绑定。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schema import TableSchema

Row = dict[str, Any]


@dataclass(frozen=True)
class TableData:
    """某版本下一张表的不可变数据快照。"""

    schema: TableSchema
    rows: tuple[Row, ...]
