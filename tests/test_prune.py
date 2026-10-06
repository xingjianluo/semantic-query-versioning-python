"""保留清理：保留范围、钉住保护、原子性、回滚范围收窄、清理与查询并发。"""

from __future__ import annotations

import threading

import pytest

from semantic_query.errors import (
    QuerySyntaxError,
    VersionNotFoundError,
    VersionRetiredError,
)
from semantic_query.models import Column, ColumnType, Table, TableSchema
from semantic_query.query import parse_query
from semantic_query.service import QueryService

from .conftest import log_decision


def _table(tag: int) -> Table:
    schema = TableSchema("t", (Column("id", ColumnType.INT),))
    return Table(schema, [{"id": tag}])


def _service_with(n: int) -> QueryService:
    svc = QueryService()
    for k in range(1, n + 1):
        svc.publish(f"v{k}", {"t": _table(k)})
    return svc


def test_prune_keep_last_validation():
    svc = _service_with(3)
    for bad in (0, -1, "2", 1.5, None, True):
        with pytest.raises(QuerySyntaxError):
            svc.prune(bad)
    log_decision("prune_validation", "prune keep_last in {0,-1,'2',1.5,None,True}", "-",
                 "非法保留范围 -> query_syntax_error，状态不变")
    assert svc.history() == ["v1", "v2", "v3"]


def test_prune_removes_old_keeps_current_and_narrows_rollback():
    svc = _service_with(5)
    report = svc.prune(2)
    assert report["pruned"] == ["v1", "v2", "v3"]
    assert report["current_version"] == "v5"
    assert svc.history() == ["v4", "v5"]
    assert svc.retired() == ["v1", "v2", "v3"]

    # 保留范围内的版本仍可指定查询；已清理的报 version_retired
    assert svc.run(parse_query({"table": "t"}), version_id="v4")["rows"] == [{"id": 4}]
    for vid in ("v1", "v2", "v3"):
        with pytest.raises(VersionRetiredError):
            svc.run(parse_query({"table": "t"}), version_id=vid)

    # 回滚范围收窄：可退到 v4；退到已清理的 v2 报 version_retired；
    # 退到从未发布的报 version_not_found
    assert svc.rollback_to("v4") == "v4"
    with pytest.raises(VersionRetiredError):
        svc.rollback_to("v2")
    with pytest.raises(VersionNotFoundError):
        svc.rollback_to("v999")
    log_decision("prune_narrows_rollback", "prune keep_last=2 then rollback_to", "v4",
                 "v1..v3 被清理且可查集合与历史列表一致；退到已清理版本 -> version_retired")


def test_prune_never_removes_current_version():
    svc = _service_with(3)
    report = svc.prune(1)
    assert report["current_version"] == "v3"
    assert svc.run(parse_query({"table": "t"}))["rows"] == [{"id": 3}]
    log_decision("prune_keeps_current", "prune keep_last=1", "v3",
                 "当前生效版本永远在保留范围内，清理后当前查询不受影响")


def test_prune_skips_pinned_version_then_succeeds_after_release(service, monkeypatch):
    """清理与查询并发：正被查询引用的版本不被清掉，查询结束后再次清理才生效。"""
    import semantic_query.service as svc_mod

    service.publish("v2", {"orders": service._store.get("v1").tables["orders"].__class__(
        service._store.get("v1").tables["orders"].schema,
        [{"id": 9, "status": "paid", "amount": 1.0}],
    )})

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
        outcome["result"] = service.run(parse_query({"table": "orders"}), version_id="v1")

    t = threading.Thread(target=run_historical)
    t.start()
    assert entered.wait(timeout=5)  # v1 已被查询钉住

    report = service.prune(1)  # 只保留当前 v2；v1 被钉住应跳过
    assert report["pruned"] == []
    assert report["kept_pinned"] == ["v1"]
    assert "v1" in service.history()  # 列表与可查集合一致：钉住的版本仍在

    release.set()
    t.join(timeout=5)
    assert outcome["result"]["version_id"] == "v1"  # 进行中的查询完整拿到 v1 数据

    report2 = service.prune(1)  # 查询结束、钉已解除，再次清理生效
    assert report2["pruned"] == ["v1"]
    with pytest.raises(VersionRetiredError):
        service.run(parse_query({"table": "orders"}), version_id="v1")
    log_decision("prune_skips_pinned", "prune during pinned query on v1", "v1",
                 "清理时 v1 被钉住 -> kept_pinned 跳过；查询结束后再清理才生效")


def test_concurrent_prune_and_queries_never_torn():
    """多线程清理与指定版本查询并发：每次查询要么拿到完整版本数据，要么 version_retired。"""
    svc = _service_with(6)
    q = parse_query({"table": "t"})
    errors: list[str] = []
    stop = threading.Event()

    def querier(vid: str):
        while not stop.is_set():
            try:
                r = svc.run(q, version_id=vid)
                if r["rows"] != [{"id": int(vid[1:])}]:
                    errors.append(f"torn read on {vid}: {r['rows']}")
            except VersionRetiredError:
                pass  # 已清理是合法、可区分的结果
            except Exception as e:  # noqa: BLE001 - 任何其他异常都不可接受，记录后断言失败
                errors.append(f"unexpected {type(e).__name__} on {vid}: {e}")

    threads = [threading.Thread(target=querier, args=(f"v{k}",)) for k in range(1, 7)]
    for t in threads:
        t.start()
    for keep in (3, 2, 1):
        svc.prune(keep)
    stop.set()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    # 查询线程全部退出、钉已解除后再做一次清理：老版本应被清干净
    svc.prune(1)
    # 清理后：历史列表中的每个版本都可查，不在列表中的都报 version_retired
    remaining = set(svc.history())
    assert remaining == {"v6"}
    for k in range(1, 7):
        vid = f"v{k}"
        try:
            svc.run(q, version_id=vid)
            assert vid in remaining, f"{vid} 可查但不在版本列表中"
        except VersionRetiredError:
            assert vid not in remaining, f"{vid} 在版本列表中却报已清理"
    log_decision("concurrent_prune_query", "prune 3/2/1 vs queries on v1..v6", "v6",
                 "并发下无撕裂读；版本列表与可查集合始终一致")


def test_prune_evicts_cache_of_retired_versions():
    """被清版本的缓存一并失效：清理后不会从缓存拿到已清理版本的结果。"""
    svc = _service_with(2)
    q = parse_query({"table": "t"})
    r1 = svc.run(q, version_id="v1")
    assert r1["cache_hit"] is False
    assert svc.run(q, version_id="v1")["cache_hit"] is True  # 已入缓存
    svc.prune(1)
    with pytest.raises(VersionRetiredError):
        svc.run(q, version_id="v1")  # 不命中残留缓存，响亮报已清理
    log_decision("prune_evicts_cache", "query v1 cached, prune, query v1 again", "-",
                 "清理使被清版本的缓存失效，不会静默返回残留结果")
