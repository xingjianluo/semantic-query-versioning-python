"""版本对比：字段增删改、行增删改、表集合不一致时响亮失败。"""

from __future__ import annotations

import pytest

from semantic_query.errors import (
    UnknownFieldError,
    VersionNotFoundError,
    VersionsIncomparableError,
    VersionUnavailableError,
)
from semantic_query.models import Column, ColumnType, Table, TableSchema
from semantic_query.service import QueryService

from .conftest import log_decision


def _schema(columns: tuple[Column, ...], name: str = "orders") -> TableSchema:
    return TableSchema(name, columns)


def _service() -> QueryService:
    svc = QueryService()
    v1_schema = _schema((
        Column("id", ColumnType.INT),
        Column("status", ColumnType.STR),
        Column("amount", ColumnType.FLOAT),
    ))
    v1_rows = [
        {"id": 1, "status": "paid", "amount": 100.0},   # 未变
        {"id": 2, "status": "void", "amount": 50.0},    # 被改
    ]
    v2_schema = _schema((
        Column("id", ColumnType.INT),
        Column("status", ColumnType.STR),
        Column("amount", ColumnType.INT),               # 类型变化
        Column("channel", ColumnType.STR),              # 新字段
    ))
    v2_rows = [
        {"id": 1, "status": "paid", "amount": 100, "channel": "web"},   # 未变/扩列
        {"id": 2, "status": "paid", "amount": 50, "channel": "web"},    # status 改
        {"id": 3, "status": "paid", "amount": 9, "channel": "app"},     # 新增
    ]
    svc.publish("v1", {"orders": Table(v1_schema, v1_rows)})
    svc.publish("v2", {"orders": Table(v2_schema, v2_rows)})
    return svc


def test_diff_fields_and_rows_with_match_keys():
    svc = _service()
    diff = svc.diff("v1", "v2", {"orders": ("id",)})
    kinds = {(f.field, f.kind) for f in diff.field_changes}
    assert ("channel", "added") in kinds
    assert ("amount", "type_changed") in kinds
    row = diff.row_changes[0]
    assert row.table == "orders"
    assert (row.added, row.removed, row.updated, row.unchanged) == (1, 0, 1, 1)
    assert row.matched_by == ("id",)
    log_decision("diff_with_keys", "diff v1->v2 match id", "v2",
                 "字段 amount 类型变更/channel 新增；行 1 新增 1 改 1 未变")


def test_diff_without_match_keys_only_counts_whole_rows():
    svc = _service()
    diff = svc.diff("v1", "v2")
    row = diff.row_changes[0]
    assert row.matched_by == () and row.updated == 0
    assert row.added == 3 and row.removed == 2  # 内容全部不同，按整行集合统计
    log_decision("diff_no_keys", "diff v1->v2 without match keys", "v2",
                 "无匹配键时只给整行增删，不臆测行级修改")


def test_diff_table_set_mismatch_is_incomparable():
    svc = _service()
    other = _schema((Column("id", ColumnType.INT),), name="payments")
    svc.publish("v3", {"payments": Table(other, [{"id": 1}])})
    with pytest.raises(VersionsIncomparableError) as exc:
        svc.diff("v1", "v3")
    assert exc.value.code == "versions_incomparable"
    assert exc.value.detail["tables_only_in_new"] == ["payments"]
    assert exc.value.detail["tables_only_in_old"] == ["orders"]
    log_decision("diff_incomparable_tables", "diff v1 vs v3 (different tables)", "v3",
                 f"表集合不一致拒绝对比 code={exc.value.code}，detail 说明原因，不给半截结果")


def test_diff_missing_or_purged_version_errors():
    svc = _service()
    with pytest.raises(VersionNotFoundError):
        svc.diff("v1", "nope")
    svc.purge(1)  # v1 被清理
    with pytest.raises(VersionUnavailableError):
        svc.diff("v1", "v2")
    log_decision("diff_bad_versions", "diff v1/nope and purged v1/v2", "v2",
                 "未发布 version_not_found；已清理 version_unavailable，对比响亮失败")


def test_diff_match_key_missing_in_schema():
    svc = _service()
    with pytest.raises(UnknownFieldError):
        svc.diff("v1", "v2", {"orders": ("missing_key",)})
    log_decision("diff_bad_match_key", "diff with missing match key", "v2",
                 "匹配键在某版本结构中不存在 -> unknown_field，响亮失败")
