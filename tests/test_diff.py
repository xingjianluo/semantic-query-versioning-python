"""版本对比：结构差异（列增删/类型变化）与行差异（增删改），以及不可对比情形的明确报错。"""

from __future__ import annotations

import pytest

from semantic_query.errors import (
    DiffIncompatibleError,
    VersionNotFoundError,
    VersionRetiredError,
)
from semantic_query.models import Column, ColumnType, Table, TableSchema
from semantic_query.service import QueryService

from .conftest import log_decision


def _table(columns: list[tuple[str, ColumnType]], rows: list[dict], name: str = "t") -> Table:
    return Table(
        TableSchema(name, tuple(Column(n, ty) for n, ty in columns)),
        rows,
    )


def _service_with_two_versions() -> QueryService:
    svc = QueryService()
    svc.publish("v1", {"t": _table(
        [("id", ColumnType.INT), ("name", ColumnType.STR), ("amount", ColumnType.INT)],
        [{"id": 1, "name": "a", "amount": 10},
         {"id": 2, "name": "b", "amount": 20},
         {"id": 3, "name": "c", "amount": 30}],
    )})
    svc.publish("v2", {"t": _table(
        # amount: int -> float（类型变化）；新增 currency；删除 name
        [("id", ColumnType.INT), ("amount", ColumnType.FLOAT), ("currency", ColumnType.STR)],
        [{"id": 1, "amount": 10.0, "currency": "USD"},   # id=1 内容变化（删 name/加 currency 外的 amount 不变）
         {"id": 3, "amount": 99.5, "currency": "EUR"},   # id=3 amount 变化 -> modified
         {"id": 4, "amount": 40.0, "currency": "USD"}],  # id=4 新增；id=2 删除
    )})
    return svc


def test_diff_reports_schema_and_row_changes():
    svc = _service_with_two_versions()
    d = svc.diff("v1", "v2")
    assert d["from_version"] == "v1" and d["to_version"] == "v2"
    (t,) = d["tables"]
    assert t["table"] == "t"
    assert t["added_columns"] == ["currency"]
    assert t["removed_columns"] == ["name"]
    assert t["type_changes"] == [{"column": "amount", "from": "int", "to": "float"}]
    # id 对齐：id=2 删除、id=4 新增；id=3 amount 10->99.5 为修改；
    # id=1 的共有列（id, amount）内容一致，不算修改
    assert t["rows"] == {"added": 1, "removed": 1, "modified": 1}
    assert t["key_column"] == "id"
    log_decision("diff_schema_and_rows", "diff v1->v2", "v1,v2",
                 "列增删/类型变化逐条列出；行按 id 对齐给出增/删/改数量")


def test_diff_without_id_column_falls_back_to_multiset():
    svc = QueryService()
    svc.publish("v1", {"t": _table([("k", ColumnType.STR)], [{"k": "a"}, {"k": "b"}])})
    svc.publish("v2", {"t": _table([("k", ColumnType.STR)], [{"k": "b"}, {"k": "c"}])})
    (t,) = svc.diff("v1", "v2")["tables"]
    assert t["key_column"] is None
    assert t["rows"] == {"added": 1, "removed": 1, "modified": 0}
    log_decision("diff_multiset_fallback", "diff v1->v2 (no id)", "v1,v2",
                 "无稳定 id 列时退化为多重集比较，只报增删")


def test_diff_identical_versions_is_empty():
    svc = _service_with_two_versions()
    (t,) = svc.diff("v1", "v1")["tables"]
    assert t["added_columns"] == [] and t["removed_columns"] == []
    assert t["type_changes"] == [] and t["rows"] == {"added": 0, "removed": 0, "modified": 0}
    log_decision("diff_identical", "diff v1->v1", "v1", "同版本对比差异为空")


def test_diff_unknown_version_fails_loudly():
    svc = _service_with_two_versions()
    with pytest.raises(VersionNotFoundError) as exc:
        svc.diff("v1", "v999")
    assert exc.value.code == "version_not_found"
    log_decision("diff_unknown_version", "diff v1->v999", "-",
                 "任一版本不存在 -> version_not_found，不给半截结果")


def test_diff_retired_version_fails_loudly():
    svc = _service_with_two_versions()
    svc.prune(1)  # v1 被清理
    with pytest.raises(VersionRetiredError) as exc:
        svc.diff("v1", "v2")
    assert exc.value.code == "version_retired"
    log_decision("diff_retired_version", "diff v1(retired)->v2", "-",
                 "版本已被清理 -> version_retired，与从未发布可区分")


def test_diff_table_set_mismatch_fails_loudly():
    svc = QueryService()
    svc.publish("v1", {"a": _table([("id", ColumnType.INT)], [{"id": 1}], name="a")})
    svc.publish("v2", {
        "a": _table([("id", ColumnType.INT)], [{"id": 1}], name="a"),
        "b": _table([("id", ColumnType.INT)], [{"id": 1}], name="b"),
    })
    with pytest.raises(DiffIncompatibleError) as exc:
        svc.diff("v1", "v2")
    assert exc.value.code == "diff_incompatible"
    assert exc.value.detail["only_in_to"] == ["b"]
    log_decision("diff_table_mismatch", "diff v1->v2 (table b only in v2)", "-",
                 "表集合不一致 -> diff_incompatible 并说明各自独有的表，不给半截结果")
