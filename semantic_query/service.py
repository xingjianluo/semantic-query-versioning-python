"""服务门面：组合版本存储、查询执行与按版本隔离的结果缓存。

缓存键 = (version_id, 查询规范化串)，因此：
- 同一版本内相同查询直接复用结果；
- 发布新版本或回滚后，键中的 version_id 变化，绝不会复用到过期/错位数据。
"""

from __future__ import annotations

import threading
from typing import Any

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

    def run(self, query: Query) -> dict[str, Any]:
        """执行查询，返回 {version_id, rows, cache_hit}。

        先取快照再执行：整个执行期间看到同一份版本，
        并发的发布/回滚不会影响本次查询。
        """
        version = self._store.snapshot()
        key = (version.version_id, canonical(query))
        with self._cache_lock:
            cached = self._cache.get(key)
        if cached is not None:
            return {"version_id": version.version_id, "rows": cached, "cache_hit": True}
        rows = execute(version, query)
        with self._cache_lock:
            self._cache[key] = rows
        return {"version_id": version.version_id, "rows": rows, "cache_hit": False}

    def current_version_id(self) -> str | None:
        return self._store.current_version_id()

    def history(self) -> list[str]:
        return self._store.history()
