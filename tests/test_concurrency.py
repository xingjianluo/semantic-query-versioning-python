"""并发发布与查询：任何时刻查询都必须看到完整一致的快照。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from sqv.dataset import TableData
from sqv.errors import SQVError, VersionConflictError
from sqv.query import Query
from sqv.schema import Column, ColumnType, TableSchema
from sqv.service import QueryService
from sqv.versioning import DatasetVersion, VersionStore

logger = logging.getLogger("test.concurrency")

SCHEMA = TableSchema("t", (Column("id", ColumnType.INT),))
QUERY = Query(table="t", select=("id",))


def _version(vid: str, marker: int, row_count: int = 50) -> DatasetVersion:
    """构造一个版本：所有行都等于 marker，便于检测是否看到半更新状态。"""
    return DatasetVersion(
        vid, {"t": TableData(SCHEMA, tuple({"id": marker} for _ in range(row_count)))}
    )


def test_concurrent_duplicate_publish_exactly_one_wins() -> None:
    svc = QueryService(VersionStore())
    results: list[str] = []
    errors: list[VersionConflictError] = []
    lock = threading.Lock()

    def publish() -> None:
        try:
            svc.publish(_version("v1", marker=1))
            with lock:
                results.append("ok")
        except VersionConflictError as exc:
            with lock:
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda _: publish(), range(64)))

    logger.info(
        "判定依据=并发重复发布同一版本必须恰好一次成功 | 成功=%d 冲突=%d",
        len(results), len(errors),
    )
    assert len(results) == 1
    assert len(errors) == 63
    assert all(e.reason == "version_conflict" for e in errors)


def test_queries_during_rolling_publish_always_see_consistent_snapshot() -> None:
    """发布持续推进时，并发查询的每行结果都必须与命中版本完全一致。"""
    svc = QueryService(VersionStore())
    svc.publish(_version("v0", marker=0))
    expected: dict[str, int] = {"v0": 0}
    stop = threading.Event()
    violations: list[str] = []
    hits: dict[str, int] = {}
    hits_lock = threading.Lock()

    def reader() -> None:
        while not stop.is_set():
            try:
                result = svc.submit(QUERY)
            except SQVError as exc:  # 不允许出现非受控异常
                violations.append(f"受控之外的失败: {exc!r}")
                continue
            marker = expected.get(result.version_id)
            if marker is None:
                violations.append(f"命中了不存在的版本 {result.version_id}")
                continue
            if any(row != (marker,) for row in result.rows):
                violations.append(
                    f"版本 {result.version_id} 的数据出现半更新: {result.rows[:3]}"
                )
            with hits_lock:
                hits[result.version_id] = hits.get(result.version_id, 0) + 1

    def publisher() -> None:
        for i in range(1, 21):
            vid = f"v{i}"
            expected[vid] = i
            svc.publish(_version(vid, marker=i))
        stop.set()

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    publisher()
    for t in threads:
        t.join()

    logger.info(
        "判定依据=每次查询的所有行都必须等于其命中版本的 marker | 命中分布=%s | 违规=%s",
        hits, violations[:3],
    )
    assert not violations
    assert sum(hits.values()) > 0


def test_queries_during_rollback_see_old_or_new_never_torn() -> None:
    svc = QueryService(VersionStore())
    svc.publish(_version("v1", marker=1))
    svc.publish(_version("v2", marker=2))
    expected = {"v1": 1, "v2": 2}
    violations: list[str] = []
    done = threading.Event()

    def reader() -> None:
        while not done.is_set():
            result = svc.submit(QUERY)
            marker = expected[result.version_id]
            if any(row != (marker,) for row in result.rows):
                violations.append(f"{result.version_id} 数据撕裂")

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    svc.rollback("v2")
    done.set()
    for t in threads:
        t.join()

    final = svc.submit(QUERY)
    logger.info(
        "判定依据=回滚期间查询只能看到 v1 或 v2 的完整快照 | 回滚后命中版本=%s | 违规=%s",
        final.version_id, violations[:3],
    )
    assert not violations
    assert final.version_id == "v1"
    assert final.rows == ((1,),) * 50


def test_concurrent_publish_and_rollback_serialized() -> None:
    """并发发布与回滚交替进行，最终状态必须确定且可解释。"""
    svc = QueryService(VersionStore())
    svc.publish(_version("base", marker=0))
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        vid = f"w{i}"
        try:
            svc.publish(_version(vid, marker=i))
            outcome = f"publish:{vid}"
            try:
                svc.rollback(vid)
                outcome = f"publish+rollback:{vid}"
            except VersionConflictError:
                pass  # 已被别的线程的版本顶掉，当前版本不是自己，回滚被拒
        except VersionConflictError:
            outcome = f"conflict:{vid}"
        with lock:
            outcomes.append(outcome)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(32)))

    current = svc._store.current()
    logger.info(
        "判定依据=所有操作串行化后当前版本必须处于 PUBLISHED 且可查询 | 当前版本=%s | 操作样本=%s",
        current.version_id, outcomes[:5],
    )
    result = svc.submit(QUERY)
    assert result.version_id == current.version_id
    marker = int(current.version_id.removeprefix("w")) if current.version_id != "base" else 0
    assert all(row == (marker,) for row in result.rows)
