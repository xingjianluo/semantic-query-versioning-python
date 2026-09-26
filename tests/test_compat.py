"""表结构演进下的向后兼容：新增/缺失/改名/类型变化字段的明确处理规则。"""
from __future__ import annotations

import logging

import pytest

from sqv.dataset import TableData
from sqv.errors import IncompatibleSchemaError, UnknownFieldError
from sqv.query import Query
from sqv.schema import Column, ColumnType, TableSchema
from sqv.service import QueryService
from sqv.versioning import DatasetVersion, VersionStore

logger = logging.getLogger("test.compat")

V1_SCHEMA = TableSchema(
    "users",
    (Column("id", ColumnType.INT), Column("name", ColumnType.STRING), Column("age", ColumnType.INT)),
)


def _service_with(*versions: DatasetVersion) -> QueryService:
    svc = QueryService(VersionStore())
    for v in versions:
        svc.publish(v)
    return svc


def test_added_column_is_backward_compatible() -> None:
    """数据侧新增字段：针对旧结构的查询照常工作。"""
    v2_schema = TableSchema(
        "users",
        V1_SCHEMA.columns + (Column("email", ColumnType.STRING),),
    )
    svc = _service_with(
        DatasetVersion("v1", {"users": TableData(V1_SCHEMA, ({"id": 1, "name": "a", "age": 30},))}),
        DatasetVersion("v2", {"users": TableData(v2_schema, ({"id": 2, "name": "b", "age": 40, "email": "b@x"},))}),
    )
    query = Query(table="users", select=("id", "age"))
    logger.info("查询输入=%s | 判定依据=新增字段 email 向后兼容，旧结构查询应命中 v2", query)
    result = svc.submit(query, assumed_schema=V1_SCHEMA)
    logger.info("命中版本=%s | 结果=%s", result.version_id, result.rows)
    assert result.version_id == "v2"
    assert result.rows == ((2, 40),)


def test_renamed_column_rejected_not_silently_wrong() -> None:
    """字段改名后，针对旧结构的查询必须被拒绝，不得静默取到错误数据。"""
    v2_schema = TableSchema(
        "users",
        (Column("id", ColumnType.INT), Column("full_name", ColumnType.STRING), Column("age", ColumnType.INT)),
    )
    svc = _service_with(
        DatasetVersion("v1", {"users": TableData(V1_SCHEMA, ({"id": 1, "name": "a", "age": 30},))}),
        DatasetVersion("v2", {"users": TableData(v2_schema, ({"id": 1, "full_name": "a", "age": 30},))}),
    )
    query = Query(table="users", select=("name",))
    logger.info("查询输入=%s | 判定依据=name 已改名为 full_name，必须拒绝", query)
    with pytest.raises(IncompatibleSchemaError) as excinfo:
        svc.submit(query, assumed_schema=V1_SCHEMA)
    logger.info("拒绝原因=%s: %s", IncompatibleSchemaError.__name__, excinfo.value)
    assert excinfo.value.reason == "incompatible_schema"


def test_deleted_column_rejected() -> None:
    v2_schema = TableSchema("users", (Column("id", ColumnType.INT), Column("name", ColumnType.STRING)))
    svc = _service_with(
        DatasetVersion("v1", {"users": TableData(V1_SCHEMA, ())}),
        DatasetVersion("v2", {"users": TableData(v2_schema, ())}),
    )
    query = Query(table="users", select=("age",))
    logger.info("查询输入=%s | 判定依据=age 已删除，按旧结构查询必须拒绝", query)
    with pytest.raises(IncompatibleSchemaError):
        svc.submit(query, assumed_schema=V1_SCHEMA)


def test_query_without_assumed_schema_validates_against_snapshot() -> None:
    """不携带旧结构时，按当前快照结构校验：引用已删除字段报 unknown_field。"""
    v2_schema = TableSchema("users", (Column("id", ColumnType.INT),))
    svc = _service_with(DatasetVersion("v1", {"users": TableData(v2_schema, ())}))
    query = Query(table="users", select=("ghost",))
    logger.info("查询输入=%s | 判定依据=快照结构中无 ghost 字段", query)
    with pytest.raises(UnknownFieldError) as excinfo:
        svc.submit(query)
    logger.info("拒绝原因=%s: %s", UnknownFieldError.__name__, excinfo.value)


def test_int_to_float_widening_compatible() -> None:
    v2_schema = TableSchema(
        "users",
        (Column("id", ColumnType.INT), Column("name", ColumnType.STRING), Column("age", ColumnType.FLOAT)),
    )
    svc = _service_with(
        DatasetVersion("v1", {"users": TableData(V1_SCHEMA, ())}),
        DatasetVersion("v2", {"users": TableData(v2_schema, ({"id": 1, "name": "a", "age": 30},))}),
    )
    query = Query(table="users", select=("age",))
    logger.info("查询输入=%s | 判定依据=int->float 属兼容加宽", query)
    result = svc.submit(query, assumed_schema=V1_SCHEMA)
    logger.info("命中版本=%s | 结果=%s", result.version_id, result.rows)
    assert result.rows == ((30,),)


def test_float_to_int_narrowing_rejected() -> None:
    v1_float = TableSchema("users", (Column("score", ColumnType.FLOAT),))
    v2_int = TableSchema("users", (Column("score", ColumnType.INT),))
    svc = _service_with(
        DatasetVersion("v1", {"users": TableData(v1_float, ())}),
        DatasetVersion("v2", {"users": TableData(v2_int, ())}),
    )
    query = Query(table="users", select=("score",))
    logger.info("查询输入=%s | 判定依据=float->int 属缩窄，不兼容", query)
    with pytest.raises(IncompatibleSchemaError, match="加宽"):
        svc.submit(query, assumed_schema=v1_float)


def test_result_cache_never_returns_stale_data_across_versions() -> None:
    """同一查询跨版本不得复用缓存结果。"""
    svc = _service_with(
        DatasetVersion("v1", {"users": TableData(V1_SCHEMA, ({"id": 1, "name": "a", "age": 30},))})
    )
    query = Query(table="users", select=("id",))
    r1 = svc.submit(query)
    svc.publish(
        DatasetVersion("v2", {"users": TableData(V1_SCHEMA, ({"id": 99, "name": "b", "age": 1},))})
    )
    r2 = svc.submit(query)
    logger.info(
        "查询输入=%s | 判定依据=缓存按版本隔离 | 命中版本: %s -> %s | 结果: %s -> %s",
        query, r1.version_id, r2.version_id, r1.rows, r2.rows,
    )
    assert r1.version_id == "v1" and r1.rows == ((1,),)
    assert r2.version_id == "v2" and r2.rows == ((99,),)


def test_result_cache_hit_within_same_version() -> None:
    svc = _service_with(
        DatasetVersion("v1", {"users": TableData(V1_SCHEMA, ({"id": 1, "name": "a", "age": 30},))})
    )
    query = Query(table="users", select=("id",))
    r1 = svc.submit(query)
    r2 = svc.submit(query)
    logger.info("查询输入=%s | 命中版本=%s | 判定依据=同版本缓存命中，结果对象一致", query, r2.version_id)
    assert r1 is r2
