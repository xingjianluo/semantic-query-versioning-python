"""历史版本指定查询：命中指定版本、错误原因可区分、执行期间版本切换不影响本次查询。"""

from __future__ import annotations

import threading

import pytest

from semantic_query.errors import VersionNotFoundError, VersionRetiredError
from semantic_query.models import Table
from semantic_query.query import parse_query

from .conftest import log_decision, orders_rows_v1, orders_schema_v1


def _v2_table() -> Table:
    return Table(orders_schema_v1(), [{"id": 99, "status": "paid", "amount": 1.0}])


def test_query_specific_published_version(service):
    """带版本标识的查询命中该历史版本，而不是当前最新版本。"""
    service.publish("v2", {"orders": _v2_table()})
    q = parse_query({"table": "orders", "order_by": ["id"]})

    latest = service.run(q)
    assert latest["version_id"] == "v2" and len(latest["rows"]) == 1

    historical = service.run(q, version_id="v1")
    assert historical["version_id"] == "v1"
    assert historical["rows"] == sorted(orders_rows_v1(), key=lambda r: r["id"])
    log_decision("query_historical_version", "select * version=v1", historical["version_id"],
                 "当前版本已是 v2，指定 v1 的查询返回 v1 自己的 4 行数据")


def test_query_unknown_version_is_distinguishable(service):
    """从未发布过的版本：version_not_found，与其他失败原因可区分。"""
    with pytest.raises(VersionNotFoundError) as exc:
        service.run(parse_query({"table": "orders"}), version_id="v999")
    assert exc.value.code == "version_not_found"
    log_decision("query_unknown_version", "select * version=v999", "-",
                 "版本从未发布 -> version_not_found，绝不静默回落到最新版本")


def test_query_retired_version_is_distinguishable(service):
    """已发布但被保留策略清理的版本：version_retired，与从未发布可区分。"""
    service.publish("v2", {"orders": _v2_table()})
    service.prune(1)  # 只保留 v2，v1 被清理
    with pytest.raises(VersionRetiredError) as exc:
        service.run(parse_query({"table": "orders"}), version_id="v1")
    assert exc.value.code == "version_retired"
    with pytest.raises(VersionNotFoundError):
        service.run(parse_query({"table": "orders"}), version_id="v999")
    log_decision("query_retired_version", "select * version=v1(after prune)", "-",
                 "已清理 -> version_retired；从未发布 -> version_not_found，两者可区分")


def test_historical_query_isolated_from_version_switch(service, monkeypatch):
    """指定历史版本的查询执行期间发生新版本发布与回滚：本次查询始终看到自己那份快照。"""
    import semantic_query.service as svc_mod

    service.publish("v2", {"orders": _v2_table()})
    entered = threading.Event()
    release = threading.Event()
    real_execute = svc_mod.execute

    def blocking_execute(version, query):
        entered.set()
        assert release.wait(timeout=5)
        return real_execute(version, query)

    monkeypatch.setattr(svc_mod, "execute", blocking_execute)

    outcome = {}

    def run_historical():
        outcome["result"] = service.run(
            parse_query({"table": "orders", "order_by": ["id"]}), version_id="v1"
        )

    t = threading.Thread(target=run_historical)
    t.start()
    assert entered.wait(timeout=5)  # 查询已钉住 v1 快照并进入执行
    # 执行期间：发布 v3，再回滚到 v2 —— 当前版本指针来回切换
    service.publish("v3", {"orders": Table(orders_schema_v1(), [{"id": 7, "status": "void", "amount": 3.0}])})
    service.rollback()
    release.set()
    t.join(timeout=5)

    result = outcome["result"]
    assert result["version_id"] == "v1"
    assert result["rows"] == sorted(orders_rows_v1(), key=lambda r: r["id"])
    assert service.current_version_id() == "v2"
    log_decision("historical_query_during_switch", "select * version=v1 during publish+rollback",
                 result["version_id"], "执行期间发布 v3 又回滚到 v2，本次查询仍完整看到 v1 快照")


def test_historical_query_cache_isolated_per_version(service):
    """同一查询串针对不同版本各自缓存，互不串数据。"""
    service.publish("v2", {"orders": _v2_table()})
    q = parse_query({"table": "orders"})
    r1 = service.run(q, version_id="v1")
    r2 = service.run(q, version_id="v2")
    r1_again = service.run(q, version_id="v1")
    assert r1["version_id"] == "v1" and len(r1["rows"]) == 4
    assert r2["version_id"] == "v2" and len(r2["rows"]) == 1
    assert r1_again["cache_hit"] is True and r1_again["rows"] == r1["rows"]
    log_decision("historical_cache_isolation", "same query on v1/v2", "v1,v2",
                 "缓存键含 version_id，两个版本的结果各自缓存互不污染")
