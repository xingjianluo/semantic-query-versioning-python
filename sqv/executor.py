"""在单一版本快照上执行已校验的查询。

执行器只接收不可变的 TableData 快照，不访问版本仓库，
因此一次执行全程看到的数据必然来自同一份快照。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .dataset import Row, TableData
from .query import AggFunc, Aggregate, CompareOp, Filter, Query


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    version_id: str


def _apply_filter(row: Row, f: Filter) -> bool:
    value = row.get(f.column)
    if value is None:
        return False  # 缺失值不满足任何比较条件
    target = f.value
    if f.op is CompareOp.EQ:
        return value == target
    if f.op is CompareOp.NE:
        return value != target
    if f.op is CompareOp.LT:
        return value < target
    if f.op is CompareOp.LE:
        return value <= target
    if f.op is CompareOp.GT:
        return value > target
    if f.op is CompareOp.GE:
        return value >= target
    raise AssertionError(f"未处理的比较操作: {f.op!r}")  # validate 已拦截


def _aggregate(rows: list[Row], agg: Aggregate) -> Any:
    if agg.func is AggFunc.COUNT and agg.column is None:
        return len(rows)
    values = [r.get(agg.column) for r in rows]  # type: ignore[arg-type]
    values = [v for v in values if v is not None]
    if agg.func is AggFunc.COUNT:
        return len(values)
    if not values:
        return None
    if agg.func is AggFunc.SUM:
        return sum(values)
    if agg.func is AggFunc.AVG:
        return sum(values) / len(values)
    if agg.func is AggFunc.MIN:
        return min(values)
    if agg.func is AggFunc.MAX:
        return max(values)
    raise AssertionError(f"未处理的聚合函数: {agg.func!r}")  # validate 已拦截


class _SortKey:
    """可比较的排序键：None 恒排最后，支持降序。"""

    __slots__ = ("value", "descending")

    def __init__(self, value: Any, descending: bool) -> None:
        self.value = value
        self.descending = descending

    def __lt__(self, other: "_SortKey") -> bool:
        if self.value is None:
            return False
        if other.value is None:
            return True
        if self.descending:
            return self.value > other.value
        return self.value < other.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _SortKey) and self.value == other.value


def execute(query: Query, data: TableData, version_id: str) -> QueryResult:
    rows: list[Row] = [r for r in data.rows if all(_apply_filter(r, f) for f in query.filters)]

    if query.aggregates or query.group_by:
        groups: dict[tuple[Any, ...], list[Row]] = {}
        for r in rows:
            key = tuple(r.get(c) for c in query.group_by)
            groups.setdefault(key, []).append(r)
        if not groups and not query.group_by:
            groups[()] = []  # 全表聚合时空输入也要产出一行
        out_columns = tuple(query.select) + tuple(a.alias for a in query.aggregates)
        out_rows = []
        for key, members in groups.items():
            named = dict(zip(query.group_by, key, strict=True))
            out_rows.append(
                tuple(named.get(c) for c in query.select)
                + tuple(_aggregate(members, a) for a in query.aggregates)
            )
    else:
        out_columns = tuple(query.select)
        out_rows = [tuple(r.get(c) for c in query.select) for r in rows]

    if query.order_by:
        index = {name: i for i, name in enumerate(out_columns)}
        out_rows.sort(
            key=lambda t: tuple(
                _SortKey(t[index[k.column]], k.descending) for k in query.order_by
            )
        )

    return QueryResult(columns=out_columns, rows=tuple(out_rows), version_id=version_id)
