"""版本存储：发布、回滚、当前版本指针的原子切换（快照隔离）。

一致性保证：
- publish 先在锁外/锁内完整构建并校验不可变 DatasetVersion，再一次原子切换
  当前指针；构建或校验中途失败时，存储状态完全不变，查询不会看到半更新状态。
- 查询通过 snapshot() 取一次版本对象引用，执行全程持有同一不可变快照。
- 重复发布同一 version_id：内容指纹一致 -> 幂等成功（返回既有版本，确定可解释）；
  内容不一致 -> VersionConflictError。
- rollback 弹出当前版本，回到上一个已发布版本；已弹出的版本仍保留在仓库中，
  重新发布同内容版本是幂等的。
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
    VersionRetiredError,
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
        self._history: list[str] = []  # 栈顶为当前版本
        self._retired: set[str] = set()  # 曾被发布但已被保留策略清理的版本
        self._pins: dict[str, int] = {}  # version_id -> 进行中的查询引用数

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
            self._history.append(version_id)
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

    def get(self, version_id: str) -> DatasetVersion:
        with self._lock:
            version = self._published.get(version_id)
            if version is None:
                if version_id in self._retired:
                    raise VersionRetiredError(
                        f"版本 {version_id!r} 已被保留策略清理，不再可用",
                        detail={"version_id": version_id, "retired": True},
                    )
                raise VersionNotFoundError(
                    f"版本 {version_id!r} 不存在",
                    detail={"version_id": version_id, "known": sorted(self._published)},
                )
            return version

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

    def retired(self) -> list[str]:
        with self._lock:
            return sorted(self._retired)

    def acquire(self, version_id: str | None = None) -> DatasetVersion:
        """取版本快照并加引用钉（pin）：清理策略不得清掉被钉住的版本。

        version_id 为 None 时取当前生效版本。调用方必须配对调用 release()。
        指定版本从未发布 -> VersionNotFoundError；已被清理 -> VersionRetiredError。
        """
        with self._lock:
            if version_id is None:
                if not self._history:
                    raise NoActiveVersionError("尚未发布任何版本，无法执行查询")
                version_id = self._history[-1]
            version = self._published.get(version_id)
            if version is None:
                if version_id in self._retired:
                    raise VersionRetiredError(
                        f"版本 {version_id!r} 已被保留策略清理，不再可用",
                        detail={"version_id": version_id, "retired": True},
                    )
                raise VersionNotFoundError(
                    f"版本 {version_id!r} 不存在",
                    detail={"version_id": version_id, "known": sorted(self._published)},
                )
            self._pins[version_id] = self._pins.get(version_id, 0) + 1
            return version

    def release(self, version_id: str) -> None:
        """解除 acquire 加的引用钉。"""
        with self._lock:
            count = self._pins.get(version_id, 0)
            if count <= 1:
                self._pins.pop(version_id, None)
            else:
                self._pins[version_id] = count - 1

    def prune(self, keep_last: int) -> dict:
        """按保留范围清理老版本：只保留最近 keep_last 个历史版本。

        安全保证：
        - 当前生效版本（历史栈顶）永远在保留范围内，绝不清除；
        - 正被查询钉住（pin）的版本跳过清除，并在报告中列出；
        - 整个清理在同一把锁内一次性完成：先算好新状态再整体替换，
          中途失败/异常不会留下"列表与可查版本不一致"的半成品状态。
        """
        if isinstance(keep_last, bool) or not isinstance(keep_last, int) or keep_last < 1:
            raise QuerySyntaxError(
                f"keep_last 必须是 >= 1 的整数, 得到: {keep_last!r}",
                detail={"keep_last": keep_last},
            )
        with self._lock:
            keep_ids = set(self._history[-keep_last:])  # 含当前版本
            pruned: list[str] = []
            kept_pinned: list[str] = []
            new_published: dict[str, DatasetVersion] = {}
            for vid, version in self._published.items():
                if vid in keep_ids:
                    new_published[vid] = version
                elif self._pins.get(vid, 0) > 0:
                    new_published[vid] = version  # 被查询引用，跳过
                    kept_pinned.append(vid)
                else:
                    pruned.append(vid)
            new_history = [vid for vid in self._history if vid in new_published]
            # 原子替换：版本列表（history）与可查集合（published）来自同一次替换
            self._published = new_published
            self._history = new_history
            self._retired.update(pruned)
            return {
                "keep_last": keep_last,
                "pruned": sorted(pruned),
                "kept_pinned": sorted(kept_pinned),
                "remaining_history": list(self._history),
                "current_version": self._history[-1] if self._history else None,
            }

    def rollback_to(self, version_id: str) -> DatasetVersion:
        """回滚到指定的已发布版本；已清理的版本报 VersionRetiredError。"""
        with self._lock:
            if version_id in self._retired:
                raise VersionRetiredError(
                    f"版本 {version_id!r} 已被保留策略清理，无法回滚",
                    detail={"version_id": version_id, "retired": True},
                )
            if version_id not in self._history:
                reason = (
                    "never_published"
                    if version_id not in self._published
                    else "not_in_rollback_range"
                )
                raise VersionNotFoundError(
                    f"版本 {version_id!r} 不在当前可回滚范围内"
                    f"（可回滚范围: {self._history}）",
                    detail={
                        "version_id": version_id,
                        "reason": reason,
                        "rollback_range": list(self._history),
                    },
                )
            while self._history and self._history[-1] != version_id:
                self._history.pop()
            return self._published[version_id]
