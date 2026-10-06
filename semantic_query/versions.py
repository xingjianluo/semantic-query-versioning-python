"""版本存储：发布、回滚/定向回退、引用计数、保留清理与快照隔离。

一致性保证：
- publish 先在锁外/锁内完整构建并校验不可变 DatasetVersion，再一次原子切换
  当前指针；构建或校验中途失败时，存储状态完全不变，查询不会看到半更新状态。
- 查询通过 pin()/pin_current() 取版本并登记引用，执行全程持有同一不可变快照，
  finally 中 unpin；被 pin 的版本不会被保留清理删除。
- 重复发布同一 version_id：内容指纹一致 -> 幂等成功（返回既有版本，确定可解释）；
  内容不一致 -> VersionConflictError。同名版本在被清理后可重新发布（移出墓碑）。
- rollback 弹出当前版本，回到上一个已发布版本；rollback_to 切到任意仍可用版本，
  已被清理 -> VersionUnavailableError，从未发布 -> VersionNotFoundError。
- purge(keep) 在单把锁内原子删除保留窗口外、非当前、未被引用的老版本并更新清单；
  被引用版本在 skipped_in_use 中返回。被删版本进入墓碑集合，get() 响亮区分
  从未发布（version_not_found）与曾发布但不可用（version_unavailable）。
"""

from __future__ import annotations

import hashlib
import json
import threading

from .errors import (
    NoActiveVersionError,
    QuerySyntaxError,
    TypeMismatchError,
    VersionConflictError,
    VersionNotFoundError,
    VersionUnavailableError,
)
from .models import ColumnType, DatasetVersion, Table


def _value_ok(column_type: ColumnType, value) -> bool:
    if isinstance(value, bool):
        return column_type is ColumnType.BOOL
    if isinstance(value, int):
        return column_type in (ColumnType.INT, ColumnType.FLOAT)
    if isinstance(value, float):
        return column_type is ColumnType.FLOAT
    if isinstance(value, str):
        return column_type is ColumnType.STR
    return False


def _validate_table(table: Table) -> None:
    """发布前校验：行键必须是已声明列的子集，值类型必须与结构一致。"""
    col_map = table.schema.column_map()
    for i, row in enumerate(table.rows):
        for key, value in row.items():
            col = col_map.get(key)
            if col is None:
                raise QuerySyntaxError(
                    f"表 {table.schema.name} 第 {i} 行包含结构未声明的列 {key!r}",
                    detail={"table": table.schema.name, "row": i, "column": key},
                )
            if value is not None and not _value_ok(col.type, value):
                raise TypeMismatchError(
                    f"表 {table.schema.name} 第 {i} 行列 {key!r} 声明为 "
                    f"{col.type.value}，实际值为 {value!r}",
                    detail={"table": table.schema.name, "row": i, "column": key, "value": value},
                )


def _freeze(tables: dict[str, Table]) -> dict[str, Table]:
    """深拷贝行数据，使发布后的版本不可变。"""
    return {
        name: Table(schema=t.schema, rows=[dict(r) for r in t.rows])
        for name, t in tables.items()
    }


def _fingerprint(version_id: str, tables: dict[str, Table]) -> str:
    obj = {
        "version_id": version_id,
        "tables": {
            name: {
                "columns": [(c.name, c.type.value) for c in t.schema.columns],
                "rows": sorted(t.rows, key=lambda r: json.dumps(r, sort_keys=True, default=str)),
            }
            for name, t in sorted(tables.items())
        },
    }
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class VersionStore:
    """线程安全的版本仓库。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._published: dict[str, DatasetVersion] = {}
        self._history: list[str] = []  # 栈顶为当前版本；已清理版本不在其中
        self._refcounts: dict[str, int] = {}  # version_id -> 进行中查询数
        self._tombstones: set[str] = set()  # 曾经发布但已被保留策略清理的版本
        self._order: list[str] = []  # 所有版本的发布先后（含已被回滚弹出的；已清理的会被移除）

    def publish(self, version_id: str, tables: dict[str, Table]) -> DatasetVersion:
        if not isinstance(version_id, str) or not version_id:
            raise QuerySyntaxError(f"version_id 必须是非空字符串, 得到: {version_id!r}")
        # 先完整校验与构建；任何一步失败都不会触碰仓库状态。
        for table in tables.values():
            _validate_table(table)
        frozen = _freeze(tables)
        fingerprint = _fingerprint(version_id, frozen)

        with self._lock:
            existing = self._published.get(version_id)
            if existing is not None:
                if existing.fingerprint == fingerprint:
                    return existing  # 幂等：同版本同内容，结果确定
                raise VersionConflictError(
                    f"版本 {version_id!r} 已发布且内容不同，拒绝重复发布",
                    detail={"version_id": version_id},
                )
            version = DatasetVersion(version_id=version_id, tables=frozen, fingerprint=fingerprint)
            self._published[version_id] = version
            self._tombstones.discard(version_id)
            # 同名版本重新发布（之前可能已被清理）：先剔除残留位置，再追加到末尾
            if version_id in self._order:
                self._order.remove(version_id)
            self._history.append(version_id)
            self._order.append(version_id)
            return version

    def rollback(self) -> DatasetVersion:
        with self._lock:
            if not self._history:
                raise NoActiveVersionError("没有任何已发布版本，无法回滚")
            popped = self._history.pop()
            if not self._history:
                raise NoActiveVersionError(
                    f"已回滚唯一的版本 {popped!r}，当前没有可用版本",
                    detail={"rolled_back": popped},
                )
            return self._published[self._history[-1]]

    def rollback_to(self, version_id: str) -> DatasetVersion:
        """把当前指针切回指定的可用历史版本。

        - 已被保留策略清理 -> VersionUnavailableError（明确提示，不静默重建/顶替）；
        - 从未发布 -> VersionNotFoundError；
        - 在回退栈中 -> 截断到该版本（更新的版本仍保留在仓库中，可再次发布/切换）；
        - 不在栈中但仍可用（此前被回滚弹出）-> 重新作为当前指针。
        """
        with self._lock:
            target = self.get(version_id)  # 响亮区分 not_found / unavailable
            if not self._history:
                raise NoActiveVersionError("没有任何已发布版本，无法回退")
            if self._history[-1] == version_id:
                return target
            if version_id in self._history:
                idx = self._history.index(version_id)
                del self._history[idx + 1:]
            else:
                self._history.append(version_id)
            return target

    def get(self, version_id: str) -> DatasetVersion:
        """按 id 取用版本：从未发布 -> VersionNotFoundError；
        曾发布但已被保留策略清理 -> VersionUnavailableError（绝不静默回退最新版本）。
        """
        with self._lock:
            version = self._published.get(version_id)
            if version is None:
                if version_id in self._tombstones:
                    raise VersionUnavailableError(
                        f"版本 {version_id!r} 曾经发布，但已被保留策略清理，无法再查询",
                        detail={
                            "version_id": version_id,
                            "available": list(self._history),
                            "current": self._history[-1] if self._history else None,
                        },
                    )
                raise VersionNotFoundError(
                    f"版本 {version_id!r} 从未发布",
                    detail={"version_id": version_id, "known": sorted(self._published)},
                )
            return version

    def pin(self, version_id: str) -> DatasetVersion:
        """取用版本并登记一次进行中引用；被引用版本不会被保留清理删除。"""
        with self._lock:
            version = self.get(version_id)
            self._refcounts[version_id] = self._refcounts.get(version_id, 0) + 1
            return version

    def pin_current(self) -> DatasetVersion:
        """取用当前生效版本并登记引用；尚无版本 -> NoActiveVersionError。"""
        with self._lock:
            if not self._history:
                raise NoActiveVersionError("尚未发布任何版本，无法执行查询")
            return self.pin(self._history[-1])

    def unpin(self, version_id: str) -> None:
        """释放一次引用。"""
        with self._lock:
            count = self._refcounts.get(version_id, 0)
            if count <= 1:
                self._refcounts.pop(version_id, None)
            else:
                self._refcounts[version_id] = count - 1

    def is_current(self, version_id: str) -> bool:
        with self._lock:
            return bool(self._history) and self._history[-1] == version_id

    def purge(self, keep: int) -> dict:
        """按保留范围清理老版本。在同一把锁内完成所有状态变更，保证原子性：

        保留规则：始终保留最近 keep 个仍可用版本；当前生效版本无论如何保留；
        正被查询引用（refcount > 0）的版本本次跳过；其余老版本一次性删除，
        并记录到墓碑集合（查询/对比时给出 unavailable 的明确原因）。

        返回 {keep, purged, kept, skipped_in_use, current}；
        任何中途异常都不会留下“列表与实际可查版本不一致”的状态。
        """
        if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
            raise QuerySyntaxError(f"keep 必须是 >= 1 的整数, 得到: {keep!r}")
        with self._lock:
            current = self._history[-1] if self._history else None
            # 保留窗口：按发布先后的最后 keep 个（_order 中含已回滚弹出但未清理的版本）
            protected_recent = set(self._order[-keep:]) if keep else set()
            purged: list[str] = []
            skipped_in_use: list[str] = []
            kept: list[str] = []
            for vid in list(self._order):
                if vid == current or vid in protected_recent:
                    kept.append(vid)
                    continue
                if self._refcounts.get(vid, 0) > 0:
                    skipped_in_use.append(vid)
                    continue
                self._published.pop(vid, None)
                self._refcounts.pop(vid, None)
                self._tombstones.add(vid)
                purged.append(vid)
            self._order = [v for v in self._order if v not in set(purged)]
            self._history = [v for v in self._history if v not in set(purged)]
            return {
                "keep": keep,
                "purged": purged,
                "kept": kept + skipped_in_use,
                "skipped_in_use": skipped_in_use,
                "current": current,
            }

    def snapshot(self) -> DatasetVersion:
        with self._lock:
            if not self._history:
                raise NoActiveVersionError("尚未发布任何版本，无法执行查询")
            return self._published[self._history[-1]]

    def current_version_id(self) -> str | None:
        with self._lock:
            return self._history[-1] if self._history else None

    def history(self) -> list[str]:
        with self._lock:
            return list(self._history)
