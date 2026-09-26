"""版本发布与回滚：确定性结果、快照一致性与错误原因可区分。"""
from __future__ import annotations

import logging

import pytest

from sqv.dataset import TableData
from sqv.errors import VersionConflictError
from sqv.query import CompareOp, Filter, Query
from sqv.schema import Column, ColumnType, TableSchema
from sqv.service import QueryService
from sqv.versioning import DatasetVersion, VersionStatus, VersionStore

logger = logging.getLogger("test.versioning")

SCHEMA = TableSchema("t", (Column("id", ColumnType.INT),))


def _version(vid: str, ids: list[int]) -> DatasetVersion:
    return DatasetVersion(vid, {"t": TableData(SCHEMA, tuple({"id": i} for i in ids))})


@pytest.fixture
def service() -> QueryService:
    return QueryService(VersionStore())


def test_publish_and_query_hits_current_version(service: QueryService) -> None:
    service.publish(_version("v1", [1, 2]))
    query = Query(table="t", select=("id",), filters=(Filter("id", CompareOp.GT, 1),))
    logger.info("查询输入=%s | 判定依据=当前版本为 v1", query)
    result = service.submit(query)
    logger.info("命中版本=%s | 结果=%s", result.version_id, result.rows)
    assert result.version_id == "v1"
    assert result.rows == ((2,),)


def test_duplicate_publish_rejected_deterministically(service: QueryService) -> None:
    service.publish(_version("v1", [1]))
    logger.info("判定依据=重复发布同一版本号必须得到确定的冲突错误")
    with pytest.raises(VersionConflictError) as excinfo:
        service.publish(_version("v1", [999]))
    logger.info("拒绝原因=%s: %s", VersionConflictError.__name__, excinfo.value)
    assert excinfo.value.reason == "version_conflict"
    # 原版本数据未被覆盖
    assert service._store.get("v1").tables["t"].rows == ({"id": 1},)


def test_rollback_restores_previous_version(service: QueryService) -> None:
    service.publish(_version("v1", [1]))
    service.publish(_version("v2", [2]))
    rolled = service.rollback("v2")
    logger.info("判定依据=回滚当前版本后应回到 v1 | 回滚结果=%s", rolled.status)
    assert rolled.status is VersionStatus.ROLLED_BACK
    query = Query(table="t", select=("id",))
    result = service.submit(query)
    logger.info("查询输入=%s | 命中版本=%s | 结果=%s", query, result.version_id, result.rows)
    assert result.version_id == "v1"
    assert result.rows == ((1,),)


def test_rollback_non_current_version_rejected(service: QueryService) -> None:
    service.publish(_version("v1", [1]))
    service.publish(_version("v2", [2]))
    logger.info("判定依据=仅允许回滚当前版本")
    with pytest.raises(VersionConflictError, match="不是当前版本"):
        service.rollback("v1")


def test_rollback_unknown_version_rejected(service: QueryService) -> None:
    logger.info("判定依据=回滚不存在的版本必须报 version_conflict")
    with pytest.raises(VersionConflictError, match="不存在"):
        service.rollback("nope")


def test_double_rollback_rejected(service: QueryService) -> None:
    service.publish(_version("v1", [1]))
    service.rollback("v1")
    logger.info("判定依据=已回滚的版本不能重复回滚")
    with pytest.raises(VersionConflictError, match="已被回滚"):
        service.rollback("v1")


def test_query_without_any_version_rejected(service: QueryService) -> None:
    logger.info("判定依据=无任何已发布版本时查询必须失败而非返回空结果")
    with pytest.raises(VersionConflictError, match="没有任何已发布版本"):
        service.submit(Query(table="t", select=("id",)))


def test_rollback_last_version_leaves_no_current(service: QueryService) -> None:
    service.publish(_version("v1", [1]))
    service.rollback("v1")
    with pytest.raises(VersionConflictError, match="没有任何已发布版本"):
        service._store.current()


def test_snapshot_isolated_from_later_publish(service: QueryService) -> None:
    """已取到的快照在后续发布后内容不变（不可变快照）。"""
    service.publish(_version("v1", [1]))
    snapshot = service._store.current()
    service.publish(_version("v2", [2]))
    logger.info(
        "判定依据=发布 v2 后，之前获取的快照仍应属于 v1 且数据不变 | 快照版本=%s",
        snapshot.version_id,
    )
    assert snapshot.version_id == "v1"
    assert snapshot.tables["t"].rows == ({"id": 1},)
