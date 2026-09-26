"""查询执行引擎：针对某一个不可变版本快照执行 Query。

兼容规则（明确且响亮，绝不静默取错数据）：
- 查询引用的列必须存在于该版本的结构中；字段被改名或删除 -> UnknownFieldError。
- 行中类型与结构声明不一致的值 -> TypeMismatchError（结构演进导致类型变化时响亮失败）。
- 行缺少结构中声明的列 -> 该单元格按 null 处理；null 只匹配 eq/ne，其余比较为假。
- 行里多出结构未声明的键（旧版本残留字段）-> 忽略，查询不可引用。
"""

from __future__ import annotations

from typing import Any

from .errors import (
    InvalidAggregationError,
    TypeMismatchError,
    UnknownFieldError,
    UnknownTableError,
)
from .models import ColumnType, DatasetVersion, Table
from .query import AggFunc, CompareOp, Query, SelectItem

_NUMERIC = (ColumnType.INT, ColumnType.FLOAT)


def _value_matches(column_type: ColumnType, value: Any) -> bool:
    if isinstance(value, bool):
        return column_type is ColumnType.BOOL
    if isinstance(value, int):
        return column_type in _NUMERIC
    if isinstance(value, float):
        return column_type is ColumnType.FLOAT
    if isinstance(value, str):
        return column_type is ColumnType.STR
    return False


def _check_constant(column_type: ColumnType, value: Any, *, where: str) -> None:
    if value is None:
        return  # null 仅允许 eq/ne，在执行处处理
    if not _value_matches(column_type, value):
        raise TypeMismatchError(
            f"常量 {value!r} 的类型与列 {where} 声明的类型 {column_type.value} 不匹配",
            detail={"column": where, "column_type": column_type.value, "value": value},
        )


def _resolve_column(table: Table, name: str, *, clause: str):
    col = table.schema.column_map().get(name)
    if col is None:
        available = [c.name for c in table.schema.columns]
        raise UnknownFieldError(
            f"表 {table.schema.name} 在当前版本中不存在字段 {name!r}"
            f"（可能已改名或删除）；可用字段: {available}",
            detail={"table": table.schema.name, "field": name, "available": available, "clause": clause},
        )
    return col


def _validate(version: DatasetVersion, query: Query) -> Table:
    table = version.table(query.table)
    if table is None:
        raise UnknownTableError(
            f"版本 {version.version_id} 中不存在表 {query.table!r}；可用表: {sorted(version.tables)}",
            detail={"table": query.table, "available": sorted(version.tables)},
        )

    for item in query.select:
        if item.column is not None:
            col = _resolve_column(table, item.column, clause="select")
            if item.agg in (AggFunc.SUM, AggFunc.AVG) and col.type not in _NUMERIC:
                raise InvalidAggregationError(
                    f"聚合 {item.agg.value} 只能用于数值列，列 {item.column!r} 的类型是 {col.type.value}",
                    detail={"column": item.column, "agg": item.agg.value, "column_type": col.type.value},
                )

    has_agg = any(s.agg is not None for s in query.select)
    plain_selected = {s.column for s in query.select if s.agg is None}
    if has_agg and not query.group_by and plain_selected:
        raise InvalidAggregationError(
            f"存在聚合但缺少 group_by 时，select 不能包含普通列: {sorted(plain_selected)}",
            detail={"plain_columns": sorted(plain_selected)},
        )
    if query.group_by:
        for g in query.group_by:
            _resolve_column(table, g, clause="group_by")
        bad = plain_selected - set(query.group_by)
        if bad:
            raise InvalidAggregationError(
                f"group_by 查询中的普通 select 列必须出现在 group_by 中: {sorted(bad)}",
                detail={"not_grouped": sorted(bad), "group_by": list(query.group_by)},
            )

    for cond in query.where:
        col = _resolve_column(table, cond.column, clause="where")
        if cond.value is None and cond.op not in (CompareOp.EQ, CompareOp.NE):
            raise TypeMismatchError(
                f"null 常量只允许 eq/ne 比较，列 {cond.column!r} 使用了 {cond.op.value}",
                detail={"column": cond.column, "op": cond.op.value},
            )
        _check_constant(col.type, cond.value, where=cond.column)

    aliases = {s.alias for s in query.select}
    for order in query.order_by:
        if order.column not in aliases:
            _resolve_column(table, order.column, clause="order_by")

    return table


def _cell(table: Table, row: dict, column: str) -> Any:
    """取单元格并做行级类型校验；缺列返回 None。"""
    col = table.schema.column_map()[column]
    value = row.get(column)
    if value is not None and not _value_matches(col.type, value):
        raise TypeMismatchError(
            f"行数据与结构不兼容：列 {column!r} 声明为 {col.type.value}，实际值为 {value!r}",
            detail={"column": column, "column_type": col.type.value, "value": value},
        )
    return value


def _match(cond, value: Any) -> bool:
    if value is None or cond.value is None:
        eq = value is None and cond.value is None
        return eq if cond.op is CompareOp.EQ else (not eq if cond.op is CompareOp.NE else False)
    return {
        CompareOp.EQ: value == cond.value,
        CompareOp.NE: value != cond.value,
        CompareOp.LT: value < cond.value,
        CompareOp.LE: value <= cond.value,
        CompareOp.GT: value > cond.value,
        CompareOp.GE: value >= cond.value,
    }[cond.op]


def _aggregate(func: AggFunc, values: list[Any]) -> Any:
    present = [v for v in values if v is not None]
    if func is AggFunc.COUNT:
        return len(present)
    if not present:
        return None
    if func is AggFunc.SUM:
        return sum(present)
    if func is AggFunc.AVG:
        return sum(present) / len(present)
    if func is AggFunc.MIN:
        return min(present)
    if func is AggFunc.MAX:
        return max(present)
    raise InvalidAggregationError(f"不支持的聚合函数: {func}")


def _project(table: Table, rows: list[dict], query: Query) -> list[dict[str, Any]]:
    select = query.select or tuple(
        SelectItem(column=c.name, alias=c.name) for c in table.schema.columns
    )
    out = []
    for row in rows:
        out.append({item.alias: _cell(table, row, item.column) for item in select})
    return out


def _group_and_aggregate(table: Table, rows: list[dict], query: Query) -> list[dict[str, Any]]:
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = tuple(_cell(table, row, g) for g in query.group_by)
        groups.setdefault(key, []).append(row)

    out = []
    for key, members in groups.items():
        record = dict(zip(query.group_by, key))
        for item in query.select:
            if item.agg is None:
                record[item.alias] = record[item.column]
            elif item.column is None:  # count(*)
                record[item.alias] = len(members)
            else:
                values = [_cell(table, m, item.column) for m in members]
                record[item.alias] = _aggregate(item.agg, values)
        out.append(record)
    return out


def _sort_key(value: Any) -> tuple:
    return (value is None, value)


def execute(version: DatasetVersion, query: Query) -> list[dict[str, Any]]:
    """在指定版本快照上执行查询，返回结果行。

    校验失败时抛出 UnknownTableError / UnknownFieldError /
    TypeMismatchError / InvalidAggregationError，绝不静默返回错误结果。
    """
    table = _validate(version, query)

    rows = [
        row
        for row in table.rows
        if all(_match(cond, _cell(table, row, cond.column)) for cond in query.where)
    ]

    has_agg = any(s.agg is not None for s in query.select)
    if query.group_by or has_agg:
        result = _group_and_aggregate(table, rows, query)
    else:
        result = _project(table, rows, query)

    for order in reversed(query.order_by):
        result.sort(key=lambda r, c=order.column: _sort_key(r[c]), reverse=order.descending)

    if query.limit is not None:
        result = result[: query.limit]
    return result
