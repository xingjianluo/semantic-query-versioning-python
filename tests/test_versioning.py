"""版本发布、幂等、冲突与回滚的确定性行为。"""

from __future__ import annotations

import pytest

from semantic_query.errors import NoActiveVersionError, VersionConflictError
from semantic_query.models import Table, TableSchema
from semantic_query.query import parse_query
from semantic_query.service import QueryService

from .conftest import log_decision, orders_rows_v1, orders_schema_v1


def _schema() -> TableSchema:
    return orders_schema_v1()


def test_duplicate_publish_same_content_is_idempotent(service):
    q = parse_query({"table": "orders"})
    before = service.run(q)
    again = service.publish("v1", {"orders": Table(_schema(), orders_rows_v1())})
    after = service.run(q)
    assert again == "v1"
    assert after["version_id"] == "v1"
    assert after["rows"] == before["rows"]
    assert after["cache_hit"] is True  # 同一版本同一查询，结果确定复用
    log_decision("idempotent_publish", "select *", after["version_id"],
                 "同版本同内容发布幂等，结果与缓存键不变")


def test_duplicate_publish_different_content_conflicts(service):
    rows = orders_rows_v1() + [{"id": 5, "status": "paid", "amount": 1.0}]
    with pytest.raises(VersionConflictError) as exc:
        service.publish("v1", {"orders": Table(_schema(), rows)})
    # 冲突后当前版本与数据不变
    assert service.current_version_id() == "v1"
    assert len(service.run(parse_query({"table": "orders"}))["rows"]) == 4
    log_decision("conflict_publish", "publish v1 with different rows", "v1",
                 f"rejected code={exc.value.code}，状态未变")


def test_publish_failure_leaves_state_untouched(service):
    bad_rows = [{"id": 1, "status": "paid", "amount": "not-a-float"}]
    with pytest.raises(Exception) as exc:
        service.publish("v2", {"orders": Table(_schema(), bad_rows)})
    assert service.current_version_id() == "v1"
    assert service.history() == ["v1"]
    log_decision("failed_publish_atomic", "publish v2 with bad row", "v1",
                 f"发布中途失败 code={exc.value.code}，当前版本仍为 v1，无半更新状态")


def test_rollback_restores_previous_version(service):
    service.publish("v2", {"orders": Table(_schema(), orders_rows_v1()[:2])})
    q = parse_query({"table": "orders"})
    assert len(service.run(q)["rows"]) == 2
    back = service.rollback()
    result = service.run(q)
    assert back == "v1"
    assert result["version_id"] == "v1"
    assert len(result["rows"]) == 4
    log_decision("rollback", "select *", result["version_id"],
                 "回滚到 v1，查询看到 v1 的 4 行数据")


def test_rollback_without_history_fails_loudly():
    svc = QueryService()
    with pytest.raises(NoActiveVersionError) as exc:
        svc.rollback()
    log_decision("rollback_empty", None, None, f"rejected code={exc.value.code}")


def test_query_without_any_version_fails_loudly():
    svc = QueryService()
    with pytest.raises(NoActiveVersionError) as exc:
        svc.run(parse_query({"table": "orders"}))
    log_decision("query_no_version", "select *", None, f"rejected code={exc.value.code}")


def test_cache_never_reuses_across_versions(service):
    q = parse_query({"table": "orders", "select": [{"agg": "count", "as": "n"}]})
    r1 = service.run(q)
    service.publish("v2", {"orders": Table(_schema(), orders_rows_v1()[:1])})
    r2 = service.run(q)
    service.rollback()
    r3 = service.run(q)
    assert r1["rows"] == [{"n": 4}] and not r1["cache_hit"]
    assert r2["rows"] == [{"n": 1}] and not r2["cache_hit"]  # 版本变了，键不同，不复用
    assert r3["rows"] == [{"n": 4}] and r3["cache_hit"]      # 回到 v1，命中 v1 自己的缓存
    log_decision("cache_version_isolation", "count(*)", f"{r1['version_id']}->{r2['version_id']}->{r3['version_id']}",
                 "缓存键含 version_id，跨版本不复用，回滚后命中本版本缓存")
