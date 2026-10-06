"""保留清理：保留窗口、当前/在用版本保护、清理与查询并发、清理后回退提示。"""

from __future__ import annotations

import threading

import pytest

from semantic_query.errors import (
    QuerySyntaxError,
    VersionNotFoundError,
    VersionUnavailableError,
)
from semantic_query.models import Column, ColumnType, Table, TableSchema
from semantic_query.query import parse_query
from semantic_query.service import QueryService

from .conftest import log_decision, orders_rows_v1, orders_schema_v1


def _service_with_versions(n: int) -> QueryService:
    svc = QueryService()
    schema = orders_schema_v1()
    for i in range(1, n + 1):
        rows = orders_rows_v1()[:i]  # 每版行数不同，便于区分结果
        svc.publish(f"v{i}", {"orders": Table(schema, rows)})
    return svc


def test_purge_keeps_window_and_current():
    svc = _service_with_versions(5)
    result = svc.purge(2)  # 保留最近 2 个：v4,v5
    assert result["purged"] == ["v1", "v2", "v3"]
    assert svc.history() == ["v4", "v5"]
    assert svc.current_version_id() == "v5"  # 当前版本绝不清掉
    # 清单一致：历史里的都能查，被清的响亮报错
    for vid in svc.history():
        assert svc.run(parse_query({"table": "orders"}), version_id=vid)["version_id"] == vid
    with pytest.raises(VersionUnavailableError):
        svc.run(parse_query({"table": "orders"}), version_id="v1")
    log_decision("purge_window", "keep=2 over v1..v5", "v5",
                 f"purged={result['purged']}，保留窗口与当前版本，清单与可查版本一致")


def test_purge_current_protected_even_when_outside_window():
    # keep=1 且当前是最早版本时，当前版本仍必须保留
    svc = _service_with_versions(3)
    svc.rollback_to("v1")
    result = svc.purge(1)
    assert "v1" not in result["purged"]
    assert svc.current_version_id() == "v1"
    assert svc.run(parse_query({"table": "orders"}), version_id="v1")["version_id"] == "v1"
    log_decision("purge_protect_current", "rollback to v1 then keep=1", "v1",
                 "当前生效版本即使落在保留窗口外也不清掉")


def test_purge_skips_version_in_query(monkeypatch):
    """清理与查询并发：查询正在引用的老版本本次跳过，查询结束后可在下一轮清掉。"""
    import semantic_query.service as svc_mod

    svc = _service_with_versions(4)
    entered = threading.Event()
    release = threading.Event()
    real_execute = svc_mod.execute

    def blocking_execute(version, query):
        entered.set()
        assert release.wait(timeout=5)
        return real_execute(version, query)

    monkeypatch.setattr(svc_mod, "execute", blocking_execute)
    outcome: dict = {}

    def run_old():
        outcome["result"] = svc.run(parse_query({"table": "orders"}), version_id="v1")

    t = threading.Thread(target=run_old)
    t.start()
    assert entered.wait(timeout=5)  # v1 正被查询 pin 住
    result = svc.purge(1)  # 理论上 v1..v3 都该清，但 v1 被引用
    assert "v1" in result["skipped_in_use"]
    assert "v1" not in result["purged"]
    assert "v1" in svc.history()  # 在用版本仍在清单且仍可查
    release.set()
    t.join(timeout=5)
    assert len(outcome["result"]["rows"]) == 1  # v1 的 1 行，查询完整跑完
    # 查询结束、引用释放后，下一轮清理可以清掉 v1
    second = svc.purge(1)
    assert "v1" in second["purged"]
    with pytest.raises(VersionUnavailableError):
        svc.run(parse_query({"table": "orders"}), version_id="v1")
    log_decision("purge_concurrent_query", "keep=1 while query pinned v1", "v4",
                 "在用版本跳过清理且查询完整；释放后下一轮清理生效")


def test_concurrent_purge_and_publish_keeps_consistent_list():
    """发布与清理反复并发：版本清单必须始终等于实际可查版本集合。"""
    svc = QueryService()
    schema = TableSchema("t", (Column("id", ColumnType.INT),))
    errors: list[str] = []
    stop = threading.Event()

    def publisher():
        for i in range(1, 41):
            svc.publish(f"w{i}", {"t": Table(schema, [{"id": i}])})
        stop.set()

    def purger():
        while not stop.is_set():
            svc.purge(3)

    from semantic_query.errors import SemanticQueryError

    def auditor():
        q = parse_query({"table": "t"})
        while not stop.is_set():
            for vid in list(svc.history()):
                try:
                    svc.run(q, version_id=vid)
                except SemanticQueryError as e:  # 清单与可查性不一致就是错误
                    errors.append(f"{vid} listed but query failed: {type(e).__name__}")

    threads = [threading.Thread(target=publisher),
               threading.Thread(target=purger),
               threading.Thread(target=auditor)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    # 发布全部结束后再做一次最终清理，验证收敛结果
    svc.purge(3)
    assert not errors, errors
    assert svc.current_version_id() == "w40"
    assert svc.history() == ["w38", "w39", "w40"]  # 收敛到保留窗口，清单与可查版本一致
    log_decision("purge_publish_concurrent", "publish x40 + purge keep=3 + audit", "w40",
                 "并发清理/发布下清单与可查版本始终一致，最终收敛到保留窗口，无半清理状态")


def test_rollback_to_purged_version_fails_loudly():
    svc = _service_with_versions(3)
    svc.purge(1)  # v1,v2 被清理，当前 v3
    with pytest.raises(VersionUnavailableError) as exc:
        svc.rollback_to("v1")
    assert exc.value.code == "version_unavailable"
    assert svc.current_version_id() == "v3"  # 被拒绝，当前不变
    with pytest.raises(VersionNotFoundError):
        svc.rollback_to("v9")  # 从未发布
    log_decision("rollback_purged", "rollback_to purged v1 / unknown v9", "v3",
                 f"回退到已清理版本被拒绝 code={exc.value.code}，回退范围随清理收窄")


def test_purge_invalid_keep():
    svc = _service_with_versions(2)
    for bad in (0, -1, 1.5, True):
        with pytest.raises(QuerySyntaxError):
            svc.purge(bad)
    log_decision("purge_bad_keep", "keep in {0,-1,1.5,true}", "v2", "非法 keep 响亮失败，状态不变")
