"""数据模型：列类型、表结构、表数据、数据集版本。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ColumnType(str, Enum):
    INT = "int"
    FLOAT = "float"
    STR = "str"
    BOOL = "bool"


@dataclass(frozen=True)
class Column:
    name: str
    type: ColumnType


@dataclass(frozen=True)
class TableSchema:
    """一张表的结构定义。"""

    name: str
    columns: tuple[Column, ...]

    def column_map(self) -> dict[str, Column]:
        return {c.name: c for c in self.columns}


@dataclass
class Table:
    """结构 + 行数据。行是 dict，键为列名。"""

    schema: TableSchema
    rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class DatasetVersion:
    """一次发布的不可变快照。查询在整个执行期间持有同一实例。"""

    version_id: str
    tables: dict[str, Table]
    fingerprint: str = ""

    def table(self, name: str) -> Table | None:
        return self.tables.get(name)
