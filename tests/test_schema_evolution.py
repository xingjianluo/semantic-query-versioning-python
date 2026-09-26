"""结构演进下的兼容规则：新增列向后兼容；改名/删除/类型变化必须响亮失败。"""

from __future__ import annotations

import pytest

from semantic_query.errors import TypeMismatchError, UnknownFieldError
from semantic_query.models import Column, ColumnType, Table, TableSchema
from semantic_query.query import parse_query

from .conftest import log_decision, orders_rows_v1, orders_schema_v1


def test_added_column_is_backward_compatible(service):
    """新增列：针对旧结构的查询（显式选列）完全不受影响。"""
    q = parse_query({
        "table": "orders",
        "select": ["id", "status", "amount"],
        "where": [{"column": "status", "op": "eq", "value": "paid"}],
    })
    before = service.run(q)

    schema_v2 = TableSchema("orders", orders_schema_v1().columns + (Column("channel", ColumnType.STR),))
    rows_v2 = [dict(r, channel="web") for r in orders_rows_v1()]
    service.publish("v2", {"orders": Table(schema_v2, rows_v2)})

    after = service.run(q)
    assert after["version_id"] == "v2"
    assert after["rows"] == before["rows"]  # 旧查询不 select 新列，结果一致
    log_decision("add_column_compatible", "where status=paid", after["version_id"],
                 "新增 channel 列后旧查询结果不变")


def test_renamed_column_fails_loudly(service):
    """字段改名（amount -> total_amount）：旧查询必须 UnknownFieldError，不得静默取错数据。"""
    schema_v2 = TableSchema(
        "orders",
        (Column("id", ColumnType.INT), Column("status", ColumnType.STR), Column("total_amount", ColumnType.FLOAT)),
    )
    rows_v2 = [{"id": r["id"], "status": r["status"], "total_amount": r["amount"]} for r in orders_rows_v1()]
    service.publish("v2", {"orders": Table(schema_v2, rows_v2)})

    q = parse_query({"table": "orders", "select": [{"agg": "sum", "column": "amount"}]})
    with pytest.raises(UnknownFieldError) as exc:
        service.run(q)
    assert exc.value.detail["available"] == ["id", "status", "total_amount"]
    log_decision("renamed_column", "sum(amount)", "v2",
                 f"rejected code={exc.value.code}，可用字段已列出，未静默返回错误数据")


def test_deleted_column_fails_loudly(service):
    """字段删除：引用该字段的 where 条件必须 UnknownFieldError。"""
    schema_v2 = TableSchema("orders", (Column("id", ColumnType.INT), Column("status", ColumnType.STR)))
    rows_v2 = [{"id": r["id"], "status": r["status"]} for r in orders_rows_v1()]
    service.publish("v2", {"orders": Table(schema_v2, rows_v2)})

    q = parse_query({"table": "orders", "where": [{"column": "amount", "op": "ge", "value": 100}]})
    with pytest.raises(UnknownFieldError) as exc:
        service.run(q)
    log_decision("deleted_column", "where amount>=100", "v2", f"rejected code={exc.value.code}")


def test_type_changed_column_fails_loudly(service):
    """列类型变化（amount float -> str）：发布时校验即拒绝，不会进入可查询状态。"""
    schema_v2 = TableSchema(
        "orders",
        (Column("id", ColumnType.INT), Column("status", ColumnType.STR), Column("amount", ColumnType.STR)),
    )
    rows_v2 = [dict(r, amount=str(r["amount"])) for r in orders_rows_v1()]
    with pytest.raises(TypeMismatchError) as exc:
        # 声明为 str 但混入旧 float 行 -> 发布校验拒绝
        service.publish("v2-bad", {"orders": Table(schema_v2, orders_rows_v1())})
    assert service.current_version_id() == "v1"
    log_decision("type_change_publish_rejected", "publish amount:str with float rows", "v1",
                 f"rejected code={exc.value.code}，半更新状态未出现")

    # 合法演进：类型与数据一起改 -> 发布成功；旧查询用 float 常量比较 -> TypeMismatchError
    service.publish("v2", {"orders": Table(schema_v2, rows_v2)})
    q = parse_query({"table": "orders", "where": [{"column": "amount", "op": "ge", "value": 100.0}]})
    with pytest.raises(TypeMismatchError) as exc2:
        service.run(q)
    log_decision("type_change_query_rejected", "where amount>=100.0", "v2",
                 f"rejected code={exc2.value.code}，常量类型与新列类型不匹配")


def test_missing_field_in_row_is_null(service):
    """行缺少结构声明的列：按 null 处理，eq null 可匹配，比较谓词为假，绝不报错或错取。"""
    schema = TableSchema("t", (Column("id", ColumnType.INT), Column("note", ColumnType.STR)))
    service.publish("v2", {"t": Table(schema, [{"id": 1, "note": "x"}, {"id": 2}])})

    r_null = service.run(parse_query({"table": "t", "where": [{"column": "note", "op": "eq", "value": None}]}))
    assert r_null["rows"] == [{"id": 2, "note": None}]
    r_cmp = service.run(parse_query({"table": "t", "where": [{"column": "note", "op": "eq", "value": "x"}]}))
    assert r_cmp["rows"] == [{"id": 1, "note": "x"}]
    log_decision("missing_field_null", "note eq null / note eq 'x'", "v2",
                 "缺列按 null：eq null 命中 id=2，eq 'x' 只命中 id=1")


def test_extra_key_in_row_rejected_at_publish(service):
    """行包含结构未声明的残留字段：发布时拒绝，避免旧字段被静默忽略。"""
    from semantic_query.errors import QuerySyntaxError

    schema = TableSchema("t", (Column("id", ColumnType.INT),))
    with pytest.raises(QuerySyntaxError) as exc:
        service.publish("v2", {"t": Table(schema, [{"id": 1, "legacy_field": "x"}])})
    assert service.current_version_id() == "v1"
    log_decision("extra_key_rejected", "publish row with legacy_field", "v1",
                 f"rejected code={exc.value.code}")
