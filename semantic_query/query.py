"""查询的解析与静态校验：把 JSON 查询规格解析为 Query AST，并做语法级校验。

语义级校验（字段存在性、类型匹配、聚合合法性）在 engine 中针对具体版本的结构进行。

查询 JSON 规格示例：
{
  "table": "orders",
  "select": ["status", {"agg": "sum", "column": "amount", "as": "total"}],
  "where": [{"column": "amount", "op": "ge", "value": 100}],
  "group_by": ["status"],
  "order_by": [{"column": "total", "desc": true}],
  "limit": 10
}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .errors import QuerySyntaxError


class AggFunc(str, Enum):
    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class CompareOp(str, Enum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LE = "le"
    GT = "gt"
    GE = "ge"


@dataclass(frozen=True)
class Condition:
    column: str
    op: CompareOp
    value: Any


@dataclass(frozen=True)
class SelectItem:
    """普通选列（agg 为 None）或聚合选列。"""

    column: str | None
    agg: AggFunc | None = None
    alias: str = ""


@dataclass(frozen=True)
class OrderItem:
    column: str
    descending: bool = False


@dataclass(frozen=True)
class Query:
    table: str
    select: tuple[SelectItem, ...] = ()
    where: tuple[Condition, ...] = ()
    group_by: tuple[str, ...] = ()
    order_by: tuple[OrderItem, ...] = ()
    limit: int | None = None


_ALLOWED_KEYS = {"table", "select", "where", "group_by", "order_by", "limit"}


def _fail(message: str, detail: object = None) -> None:
    raise QuerySyntaxError(message, detail=detail)


def _parse_select_item(item: Any) -> SelectItem:
    if isinstance(item, str):
        if not item:
            _fail("select 中的列名不能为空字符串")
        return SelectItem(column=item, alias=item)
    if isinstance(item, dict):
        unknown = set(item) - {"agg", "column", "as"}
        if unknown:
            _fail(f"select 聚合项包含不支持的键: {sorted(unknown)}", detail=sorted(unknown))
        agg_raw = item.get("agg")
        try:
            agg = AggFunc(agg_raw)
        except ValueError:
            _fail(f"不支持的聚合函数: {agg_raw!r}", detail=agg_raw)
        column = item.get("column")
        if agg is AggFunc.COUNT and column in (None, "*"):
            column = None
        elif not isinstance(column, str) or not column:
            _fail(f"聚合 {agg.value} 需要非空字符串列名, 得到: {column!r}")
        alias = item.get("as")
        if alias is None:
            alias = agg.value if column is None else f"{agg.value}_{column}"
        if not isinstance(alias, str) or not alias:
            _fail(f"聚合别名 as 必须是非空字符串, 得到: {alias!r}")
        return SelectItem(column=column, agg=agg, alias=alias)
    _fail(f"select 项必须是列名字符串或聚合对象, 得到: {item!r}")


def _parse_condition(item: Any) -> Condition:
    if not isinstance(item, dict):
        _fail(f"where 条件必须是对象, 得到: {item!r}")
    unknown = set(item) - {"column", "op", "value"}
    if unknown:
        _fail(f"where 条件包含不支持的键: {sorted(unknown)}", detail=sorted(unknown))
    column = item.get("column")
    if not isinstance(column, str) or not column:
        _fail(f"where 条件需要非空字符串列名, 得到: {column!r}")
    try:
        op = CompareOp(item.get("op"))
    except ValueError:
        _fail(f"不支持的比较操作符: {item.get('op')!r}", detail=item.get("op"))
    if "value" not in item:
        _fail(f"where 条件缺少 value: {item!r}")
    value = item["value"]
    if value is not None and not isinstance(value, (bool, int, float, str)):
        _fail(f"where 条件的 value 只支持布尔/数值/字符串, 得到: {value!r}")
    return Condition(column=column, op=op, value=value)


def _parse_order_item(item: Any) -> OrderItem:
    if isinstance(item, str):
        if not item:
            _fail("order_by 中的列名不能为空字符串")
        return OrderItem(column=item)
    if isinstance(item, dict):
        unknown = set(item) - {"column", "desc"}
        if unknown:
            _fail(f"order_by 项包含不支持的键: {sorted(unknown)}", detail=sorted(unknown))
        column = item.get("column")
        if not isinstance(column, str) or not column:
            _fail(f"order_by 需要非空字符串列名, 得到: {column!r}")
        desc = item.get("desc", False)
        if not isinstance(desc, bool):
            _fail(f"order_by 的 desc 必须是布尔值, 得到: {desc!r}")
        return OrderItem(column=column, descending=desc)
    _fail(f"order_by 项必须是列名字符串或对象, 得到: {item!r}")


def _parse_str_list(raw: Any, key: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        _fail(f"{key} 必须是数组, 得到: {raw!r}")
    for s in raw:
        if not isinstance(s, str) or not s:
            _fail(f"{key} 的元素必须是非空字符串, 得到: {s!r}")
    return tuple(raw)


def parse_query(spec: dict) -> Query:
    """把 JSON 查询规格解析为 Query；语法非法时抛 QuerySyntaxError。"""
    if not isinstance(spec, dict):
        _fail(f"查询必须是 JSON 对象, 得到: {spec!r}")
    unknown = set(spec) - _ALLOWED_KEYS
    if unknown:
        _fail(f"查询包含不支持的键: {sorted(unknown)}", detail=sorted(unknown))

    table = spec.get("table")
    if not isinstance(table, str) or not table:
        _fail(f"查询缺少 table 或 table 不是非空字符串, 得到: {table!r}")

    select_raw = spec.get("select", [])
    if not isinstance(select_raw, list):
        _fail(f"select 必须是数组, 得到: {select_raw!r}")
    select = tuple(_parse_select_item(i) for i in select_raw)

    where_raw = spec.get("where", [])
    if not isinstance(where_raw, list):
        _fail(f"where 必须是数组, 得到: {where_raw!r}")
    where = tuple(_parse_condition(i) for i in where_raw)

    group_by = _parse_str_list(spec.get("group_by", []), "group_by")

    order_raw = spec.get("order_by", [])
    if not isinstance(order_raw, list):
        _fail(f"order_by 必须是数组, 得到: {order_raw!r}")
    order_by = tuple(_parse_order_item(i) for i in order_raw)

    limit = spec.get("limit")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            _fail(f"limit 必须是正整数, 得到: {limit!r}")

    return Query(
        table=table,
        select=select,
        where=where,
        group_by=group_by,
        order_by=order_by,
        limit=limit,
    )


def canonical(query: Query) -> str:
    """查询的规范化字符串，用于结果缓存键。"""
    obj = {
        "table": query.table,
        "select": [
            {"column": s.column, "agg": s.agg.value if s.agg else None, "alias": s.alias}
            for s in query.select
        ],
        "where": [{"column": c.column, "op": c.op.value, "value": c.value} for c in query.where],
        "group_by": list(query.group_by),
        "order_by": [{"column": o.column, "desc": o.descending} for o in query.order_by],
        "limit": query.limit,
    }
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)
