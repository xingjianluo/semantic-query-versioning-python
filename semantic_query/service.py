"""服务门面：组合版本存储、查询执行与按版本隔离的结果缓存。

缓存键 = (version_id, 查询规范化串)，因此：
- 同一版本内相同查询直接复用结果；
- 发布新版本或回滚后，键中的 version_id 变化，绝不会复用到过期/错位数据。

历史版本查询：run(query, version_id=...) 在整个执行期间通过引用计数 pin 住
该版本快照，并发的发布/回滚/保留清理都不会让本次查询看到变动；版本从未发布
（version_not_found）或已被清理（version_unavailable）响亮失败，绝不静默
拿最新版本顶替。
"""

from __future__ import annotations

import threading
from typing import Any

from .diff import VersionDiff, diff_versions
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

    def rollback_to(self, version_id: str) -> str:
        return self._store.rollback_to(version_id).version_id

    def run(self, query: Query, version_id: str | None = None) -> dict[str, Any]:
        """执行查询，返回 {version_id, rows, cache_hit}。

        version_id 为 None 时查当前生效版本；否则查指定的已发布历史版本。
        先 pin 快照再执行，finally 中 unpin：整个执行期间看到同一份版本，
        并发的发布/回滚/清理不会影响本次查询，也不会把被引用版本清掉。
        """
        pinned_id = version_id
        version = self._store.pin(pinned_id) if pinned_id else self._store.pin_current()
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
            self._store.unpin(version.version_id)

    def diff(
        self,
        old_version_id: str,
        new_version_id: str,
        match_keys: dict[str, tuple[str, ...]] | None = None,
    ) -> VersionDiff:
        """对比两个已发布版本。任一侧不存在/已清理 -> 响亮报错；
        对比本身是只读快照操作，各自 pin 住，执行期间不会被清理。
        """
        old = self._store.pin(old_version_id)
        try:
            new = self._store.pin(new_version_id)
            try:
                return diff_versions(old, new, match_keys)
            finally:
                self._store.unpin(new_version_id)
        finally:
            self._store.unpin(old_version_id)

    def purge(self, keep: int) -> dict[str, Any]:
        """按保留范围清理老版本；连带删除被清版本的缓存项。

        存储层在单把锁内原子完成版本删除与列表更新；缓存清理紧随其后，
        失败也不会影响“版本列表与可查版本一致”（缓存只可能残留、永不指向
        不存在的版本，因为后续查询按版本取快照）。
        """
        result = self._store.purge(keep)
        purged = set(result["purged"])
        if purged:
            with self._cache_lock:
                self._cache = {
                    key: rows for key, rows in self._cache.items() if key[0] not in purged
                }
        return result

    def current_version_id(self) -> str | None:
        return self._store.current_version_id()

    def history(self) -> list[str]:
        return self._store.history()
