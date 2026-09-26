"""并发发布与查询：快照隔离、无半更新状态、并发重复发布结果确定。"""

from __future__ import annotations

import threading

from semantic_query.models import Column, ColumnType, Table, TableSchema
from semantic_query.query import parse_query
from semantic_query.service import QueryService

from .conftest import log_decision


def _schema() -> TableSchema:
    return TableSchema("t", (Column("id", ColumnType.INT), Column("grp", ColumnType.STR)))


def _rows(version_no: int, count: int) -> list[dict]:
    return [{"id": version_no * 1000 + i, "grp": "g"} for i in range(count)]


def test_query_holds_one_snapshot_while_publish_happens(service, monkeypatch):
    """查询执行中途发生发布：本次查询必须始终看到开始时的快照。"""
    import semantic_query.service as svc_mod

    entered = threading.Event()
    release = threading.Event()
    real_execute = svc_mod.execute

    def blocking_execute(version, query):
        entered.set()
        assert release.wait(timeout=5)
        return real_execute(version, query)

    monkeypatch.setattr(svc_mod, "execute", blocking_execute)

    outcome = {}

    def run_query():
        outcome["result"] = service.run(parse_query({"table": "orders"}))

    t = threading.Thread(target=run_query)
    t.start()
    assert entered.wait(timeout=5)  # 查询已取到快照并进入执行
    # 查询执行期间发布新版本
    service.publish("v2", {"orders": service_v2_table()})
    release.set()
    t.join(timeout=5)

    result = outcome["result"]
    assert result["version_id"] == "v1"
    assert len(result["rows"]) == 4  # 仍是 v1 的 4 行，不是 v2 的数据
    assert service.current_version_id() == "v2"
    log_decision("snapshot_isolation", "select * during publish", result["version_id"],
                 "执行期间发布 v2，本次查询仍完整看到 v1 快照")


def service_v2_table() -> Table:
    schema = TableSchema(
        "orders",
        (Column("id", ColumnType.INT), Column("status", ColumnType.STR), Column("amount", ColumnType.FLOAT)),
    )
    return Table(schema, [{"id": 9, "status": "paid", "amount": 1.0}])


def test_concurrent_publish_and_query_never_torn():
    """多线程发布 + 查询：每次查询结果必须完整对应某一个已发布版本。"""
    svc = QueryService()
    expected: dict[str, list[dict]] = {}
    for k in range(1, 9):
        vid = f"v{k}"
        expected[vid] = _rows(k, k)
    svc.publish("v1", {"t": Table(_schema(), expected["v1"])})

    errors: list[str] = []
    stop = threading.Event()

    def publisher():
        for k in range(2, 9):
            svc.publish(f"v{k}", {"t": Table(_schema(), expected[f"v{k}"])})
        stop.set()

    def querier():
        q = parse_query({"table": "t", "order_by": ["id"]})
        while not stop.is_set():
            r = svc.run(q)
            want = expected.get(r["version_id"])
            if want is None or r["rows"] != want:
                errors.append(f"torn read: version={r['version_id']} rows={r['rows'][:3]}...")

    threads = [threading.Thread(target=publisher)] + [
        threading.Thread(target=querier) for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert svc.current_version_id() == "v8"
    log_decision("concurrent_publish_query", "select * order by id (loop)", "v1..v8",
                 "4 查询线程与发布线程并发，未观察到半更新状态")


def test_concurrent_duplicate_publish_is_deterministic():
    """两个线程同时发布同版本同内容：都成功且指向同一版本；内容不同则恰有一方冲突。"""
    from semantic_query.errors import VersionConflictError

    svc = QueryService()
    results: list[str] = []
    lock = threading.Lock()

    def pub(vid, rows):
        try:
            svc.publish(vid, {"t": Table(_schema(), rows)})
            with lock:
                results.append("ok")
        except VersionConflictError:
            with lock:
                results.append("conflict")

    # 同内容并发发布：幂等，全部 ok
    threads = [threading.Thread(target=pub, args=("v1", _rows(1, 3))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["ok"] * 8

    # 不同内容并发发布同一版本号：恰好一个 ok，其余 conflict
    results.clear()
    barrier = threading.Barrier(4)

    def pub2(rows):
        barrier.wait()
        pub("v2", rows)

    threads = [threading.Thread(target=pub2, args=(_rows(i, 3),)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["conflict", "conflict", "conflict", "ok"]
    log_decision("concurrent_duplicate_publish", "publish v1 x8 same / v2 x4 different", "v2",
                 "同内容幂等全 ok；不同内容恰好一个成功其余 version_conflict")
