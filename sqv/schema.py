"""表结构（schema）定义与类型系统。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class ColumnType(enum.Enum):
    INT = "int"
    FLOAT = "float"
    STRING = "string"
    BOOL = "bool"


@dataclass(frozen=True)
class Column:
    name: str
    type: ColumnType


@dataclass(frozen=True)
class TableSchema:
    """一张表在某一版本下的结构定义。"""

    table: str
    columns: tuple[Column, ...] = field(default_factory=tuple)

    def column(self, name: str) -> Column | None:
        for col in self.columns:
            if col.name == name:
                return col
        return None

    def column_names(self) -> tuple[str, ...]:
        return tuple(col.name for col in self.columns)
