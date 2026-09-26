"""服务门面：组合校验、兼容检查、快照获取与执行。"""
from __future__ import annotations

import threading

from .compat import check_query_compatible
from .errors import UnknownFieldError
from .executor import QueryResult, execute
from .query import Query, validate
from .schema import TableSchema
from .versioning import DatasetVersion, VersionStore


class QueryService:
    """受控查询与版本发布的统一入口。

    - submit 全程持有同一份不可变快照，执行期间不受并发发布/回滚影响；
    - 结果缓存以版本号为命中条件，跨版本绝不复用，避免返回过期数据。
    """

    def __init__(self, store: VersionStore) -> None:
        self._store = store
        self._cache: dict[Query, tuple[str, QueryResult]] = {}
        self._cache_lock = threading.Lock()

    def submit(self, query: Query, assumed_schema: TableSchema | None = None) -> QueryResult:
        snapshot = self._store.current()  # 整个执行期间持有同一份快照
        data = snapshot.tables.get(query.table)
        if data is None:
            raise UnknownFieldError(
                f"版本 {snapshot.version_id} 中不存在数据表: {query.table}"
            )

        validate(query, assumed_schema or data.schema)
        if assumed_schema is not None:
            check_query_compatible(assumed_schema, data.schema)

        with self._cache_lock:
            cached = self._cache.get(query)
            if cached is not None and cached[0] == snapshot.version_id:
                return cached[1]

        result = execute(query, data, snapshot.version_id)
        with self._cache_lock:
            self._cache[query] = (snapshot.version_id, result)
        return result

    def publish(self, version: DatasetVersion) -> DatasetVersion:
        return self._store.publish(version)

    def rollback(self, version_id: str) -> DatasetVersion:
        return self._store.rollback(version_id)
