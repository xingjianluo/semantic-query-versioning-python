"""跨版本结构兼容规则：新增/缺失/类型变化/改名/删除字段的处理。

兼容规则（query_schema 为查询编写时针对的结构，data_schema 为数据实际结构）：
- 数据侧新增字段：兼容，查询不受影响；
- 查询侧字段在数据侧缺失（被删除或改名）：不兼容，拒绝执行，
  绝不允许静默映射到别的列取到错误数据；
- 类型变化：仅 INT -> FLOAT 的加宽兼容，其余（含 FLOAT -> INT、
  与 STRING/BOOL 互转）一律不兼容。
"""
from __future__ import annotations

from .errors import IncompatibleSchemaError
from .schema import ColumnType, TableSchema

_WIDENING: dict[ColumnType, frozenset[ColumnType]] = {
    ColumnType.INT: frozenset({ColumnType.FLOAT}),
}


def check_query_compatible(query_schema: TableSchema, data_schema: TableSchema) -> None:
    """校验查询针对的结构与数据实际结构是否兼容，不兼容则抛 IncompatibleSchemaError。"""
    for col in query_schema.columns:
        data_col = data_schema.column(col.name)
        if data_col is None:
            raise IncompatibleSchemaError(
                f"字段 {col.name} 在数据版本 {data_schema.table} 中不存在"
                "（可能已被删除或改名），拒绝执行以避免取到错位数据"
            )
        if data_col.type is col.type:
            continue
        allowed = _WIDENING.get(col.type, frozenset())
        if data_col.type not in allowed:
            raise IncompatibleSchemaError(
                f"字段 {col.name} 类型从 {col.type.value} 变为 {data_col.type.value}，"
                "不属于兼容的加宽变化"
            )
