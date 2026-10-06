"""历史版本查询：指定版本取数、明确的失败原因、查询期间版本切换的快照隔离。"""

from __future__ import annotations

import threading

import pytest

from semantic_query.errors import VersionNotFoundError, VersionUnavailableError
from semantic_query.models import Table
from semantic_query.query import parse_query

from .conftest import log_decision, orders_rows_v1, orders_schema_v1


def _v1(service):
    return Table(orders_schema_v1(), orders_rows_v1())


def _v2():
    schema = orders_schema_v1()
    return Table(schema, orders_rows_v1()[:1])  # 只有 1 行


def test_query_specific_historical_version(service):
    service.publish("v2", {"orders": _v2()})
    # 当前是 v2（1 行），显式指定 v1 必须看到 v1 的 4 行，不拿最新顶替
    current = service.run(parse_query({"table": "orders"}))
    historical = service.run(parse_query({"table": "orders"}), version_id="v1")
    assert current["version_id"] == "v2" and len(current["rows"]) == 1
    assert historical["version_id"] == "v1" and len(historical["rows"]) == 4
    log_decision("historical_explicit", "select * @ v1", historical["version_id"],
                 "当前为 v2(1 行)，显式指定 v1 完整返回 v1 的 4 行，未用最新顶替")


def test_historical_query_error_codes_are_distinct(service):
    # 从未发布 -> version_not_found
    with pytest.raises(VersionNotFoundError) as exc1:
        service.run(parse_query({"table": "orders"}), version_id="nope")
    assert exc1.value.code == "version_not_found"

    service.publish("v2", {"orders": _v2()})
    service.purge(1)  # 只保留最近 1 个（v2），v1 被清理
    # 曾发布但已清理 -> version_unavailable，二者可区分
    with pytest.raises(VersionUnavailableError) as exc2:
        service.run(parse_query({"table": "orders"}), version_id="v1")
    assert exc2.value.code == "version_unavailable"
    assert exc2.value.detail["current"] == "v2"
    log_decision("historical_distinct_errors", "select * @ nope / @ purged v1", "v2",
                 f"未发布={exc1.value.code}；已清理={exc2.value.code}，均响亮失败不顶替最新")


def test_historical_query_is_isolated_from_version_switch(service, monkeypatch):
    """指定 v1 的查询执行期间，并发发布 v2 再回滚到 v1：本次查询始终看到自己 pin 的快照。"""
    import semantic_query.service as svc_mod

    entered = threading.Event()
    release = threading.Event()
    real_execute = svc_mod.execute

    def blocking_execute(version, query):
        entered.set()
        assert release.wait(timeout=5)
        return real_execute(version, query)

    monkeypatch.setattr(svc_mod, "execute", blocking_execute)

    outcome: dict = {}

    def run_historical():
        outcome["result"] = service.run(parse_query({"table": "orders"}), version_id="v1")

    t = threading.Thread(target=run_historical)
    t.start()
    assert entered.wait(timeout=5)  # 查询已 pin v1 并进入执行
    # 查询执行期间：发布 v2 改变当前版本，再回滚切回 v1
    service.publish("v2", {"orders": _v2()})
    service.rollback()
    assert service.current_version_id() == "v1"
    release.set()
    t.join(timeout=5)

    result = outcome["result"]
    assert result["version_id"] == "v1"
    assert len(result["rows"]) == 4  # 查询看到的是自己开始时 pin 的完整 v1 快照
    log_decision("historical_snapshot_during_switch", "select * @ v1 while publish v2 + rollback",
                 result["version_id"],
                 "查询期间发生发布与版本切换，结果仍完整对应开始时 pin 的 v1")
