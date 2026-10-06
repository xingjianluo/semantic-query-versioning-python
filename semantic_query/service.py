"""服务门面：组合版本存储、查询执行与按版本隔离的结果缓存。

缓存键 = (version_id, 查询规范化串)，因此：
- 同一版本内相同查询直接复用结果；
- 发布新版本或回滚后，键中的 version_id 变化，绝不会复用到过期/错位数据。
"""

from __future__ import annotations

import threading
from typing import Any

from .diff import diff_versions
from .engine import execute
from .models import Table
from .query import Query, canonical
from .versions import VersionStore


class QueryService:
    def __init__(self) -> None:
        self._store = VersionStore()
        self._cache_lock = threading.Lock()
        self._cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def publish(self, version_id: str, tables: dict[str, Table]) -> str:
        return self._store.publish(version_id, tables).version_id

    def rollback(self) -> str:
        return self._store.rollback().version_id

    def run(self, query: Query, version_id: str | None = None) -> dict[str, Any]:
        """执行查询，返回 {version_id, rows, cache_hit}。

        version_id 为 None 时查询当前生效版本；指定时查询该历史版本。
        先取快照（并加引用钉）再执行：整个执行期间看到同一份版本，
        并发的发布/回滚/清理不会影响本次查询。
        """
        version = self._store.acquire(version_id)
        try:
            key = (version.version_id, canonical(query))
            with self._cache_lock:
                cached = self._cache.get(key)
            if cached is not None:
                return {"version_id": version.version_id, "rows": cached, "cache_hit": True}
            rows = execute(version, query)
            with self._cache_lock:
                self._cache[key] = rows
            return {"version_id": version.version_id, "rows": rows, "cache_hit": False}
        finally:
            self._store.release(version.version_id)

    def diff(self, from_version: str, to_version: str) -> dict[str, Any]:
        """对比两个已发布版本的结构与数据差异。

        任一版本不存在/已清理、或两版本表集合不一致时抛出可区分错误，
        绝不返回半截结果。
        """
        old = self._store.get(from_version)
        new = self._store.get(to_version)
        return diff_versions(old, new).to_dict()

    def prune(self, keep_last: int) -> dict[str, Any]:
        """按保留范围清理老版本，返回清理报告；同时失效被清版本的缓存。"""
        report = self._store.prune(keep_last)
        pruned = set(report["pruned"])
        if pruned:
            with self._cache_lock:
                for key in [k for k in self._cache if k[0] in pruned]:
                    del self._cache[key]
        return report

    def rollback_to(self, version_id: str) -> str:
        return self._store.rollback_to(version_id).version_id

    def retired(self) -> list[str]:
        return self._store.retired()

    def current_version_id(self) -> str | None:
        return self._store.current_version_id()

    def history(self) -> list[str]:
        return self._store.history()
