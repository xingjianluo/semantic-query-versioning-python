"""受控查询的抽象语法定义与校验入口。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from .errors import (
    InvalidAggregationError,
    QuerySyntaxError,
    TypeMismatchError,
    UnknownFieldError,
)
from .schema import ColumnType, TableSchema


class CompareOp(enum.Enum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LE = "le"
    GT = "gt"
    GE = "ge"


class AggFunc(enum.Enum):
    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


@dataclass(frozen=True)
class Filter:
    column: str
    op: CompareOp
    value: Any


@dataclass(frozen=True)
class Aggregate:
    func: AggFunc
    column: str | None  # count 允许为 None 表示 count(*)
    alias: str


@dataclass(frozen=True)
class OrderKey:
    column: str  # 可以是输出列名或聚合别名
    descending: bool = False


@dataclass(frozen=True)
class Query:
    """一条受控查询：选列、过滤、分组聚合、排序。"""

    table: str
    select: tuple[str, ...] = field(default_factory=tuple)
    filters: tuple[Filter, ...] = field(default_factory=tuple)
    group_by: tuple[str, ...] = field(default_factory=tuple)
    aggregates: tuple[Aggregate, ...] = field(default_factory=tuple)
    order_by: tuple[OrderKey, ...] = field(default_factory=tuple)


def _value_matches(value: Any, col_type: ColumnType) -> bool:
    """字面量类型与列类型的匹配规则（bool 不算 int）。"""
    if col_type is ColumnType.BOOL:
        return isinstance(value, bool)
    if col_type is ColumnType.INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if col_type is ColumnType.FLOAT:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if col_type is ColumnType.STRING:
        return isinstance(value, str)
    return False


def _require_column(schema: TableSchema, name: str, where: str) -> ColumnType:
    col = schema.column(name)
    if col is None:
        raise UnknownFieldError(f"{where} 引用了不存在的字段: {schema.table}.{name}")
    return col.type


def validate(query: Query, schema: TableSchema) -> None:
    """按表结构校验查询，任何非法情况都抛出可区分的具体异常。"""
    if not query.select and not query.aggregates:
        raise QuerySyntaxError("select 与 aggregates 不能同时为空")

    for name in query.select:
        _require_column(schema, name, "select")

    for f in query.filters:
        col_type = _require_column(schema, f.column, "filter")
        if not isinstance(f.op, CompareOp):
            raise QuerySyntaxError(f"不支持的比较操作: {f.op!r}")
        if not _value_matches(f.value, col_type):
            raise TypeMismatchError(
                f"filter 字段 {f.column} 类型为 {col_type.value}，"
                f"与字面量 {f.value!r} 不匹配"
            )

    for name in query.group_by:
        _require_column(schema, name, "group_by")

    seen_aliases: set[str] = set()
    for agg in query.aggregates:
        if not isinstance(agg.func, AggFunc):
            raise QuerySyntaxError(f"不支持的聚合函数: {agg.func!r}")
        if not agg.alias or not agg.alias.strip():
            raise QuerySyntaxError("聚合输出别名不能为空")
        if agg.alias in seen_aliases:
            raise QuerySyntaxError(f"聚合输出别名重复: {agg.alias}")
        seen_aliases.add(agg.alias)
        if agg.func is AggFunc.COUNT and agg.column is None:
            continue  # count(*)
        if agg.column is None:
            raise InvalidAggregationError(f"聚合 {agg.func.value} 必须指定目标字段")
        col_type = _require_column(schema, agg.column, "aggregate")
        if agg.func in (AggFunc.SUM, AggFunc.AVG) and col_type not in (
            ColumnType.INT,
            ColumnType.FLOAT,
        ):
            raise InvalidAggregationError(
                f"聚合 {agg.func.value} 仅支持数值字段，字段 {agg.column} 类型为 {col_type.value}"
            )
        if agg.func in (AggFunc.MIN, AggFunc.MAX) and col_type is ColumnType.BOOL:
            raise InvalidAggregationError(
                f"聚合 {agg.func.value} 不支持布尔字段 {agg.column}"
            )

    if query.aggregates:
        group_set = set(query.group_by)
        for name in query.select:
            if name not in group_set:
                raise QuerySyntaxError(
                    f"含聚合时 select 字段必须出现在 group_by 中: {name}"
                )

    orderable = set(query.select) | seen_aliases
    for key in query.order_by:
        if key.column not in orderable:
            raise UnknownFieldError(f"order_by 引用了不可排序的字段: {key.column}")
