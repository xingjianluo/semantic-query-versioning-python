"""数据集版本发布、回滚与快照可见性管理。"""
from __future__ import annotations

import enum
import threading
from dataclasses import dataclass, replace

from .dataset import TableData
from .errors import VersionConflictError


class VersionStatus(enum.Enum):
    PUBLISHED = "published"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class DatasetVersion:
    version_id: str
    tables: dict[str, TableData]
    status: VersionStatus = VersionStatus.PUBLISHED


class VersionStore:
    """线程安全的版本仓库：发布、回滚、获取当前快照。

    一致性保证：
    - 所有状态变更都在同一把锁内完成，提交动作是指针级原子操作，
      查询要么看到旧快照、要么看到完整新快照，绝不会看到半更新状态。
    - current() 返回的是不可变 DatasetVersion 对象，已开始的查询
      在整个执行期间持有同一份快照，不受后续发布/回滚影响。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._versions: dict[str, DatasetVersion] = {}
        self._history: list[str] = []  # 仍为 PUBLISHED 的版本，按发布顺序
        self._current_id: str | None = None

    def publish(self, version: DatasetVersion) -> DatasetVersion:
        with self._lock:
            if version.version_id in self._versions:
                existing = self._versions[version.version_id]
                raise VersionConflictError(
                    f"版本 {version.version_id} 已存在（状态 {existing.status.value}），"
                    "重复发布被拒绝"
                )
            self._versions[version.version_id] = version
            self._history.append(version.version_id)
            self._current_id = version.version_id
            return version

    def rollback(self, version_id: str) -> DatasetVersion:
        """撤销当前版本，当前指针回退到上一个仍处于 PUBLISHED 的版本。"""
        with self._lock:
            version = self._versions.get(version_id)
            if version is None:
                raise VersionConflictError(f"版本 {version_id} 不存在，无法回滚")
            if version.status is VersionStatus.ROLLED_BACK:
                raise VersionConflictError(f"版本 {version_id} 已被回滚，不能重复回滚")
            if version_id != self._current_id:
                raise VersionConflictError(
                    f"版本 {version_id} 不是当前版本，仅允许回滚当前版本"
                )
            rolled = replace(version, status=VersionStatus.ROLLED_BACK)
            self._versions[version_id] = rolled
            self._history.remove(version_id)
            self._current_id = self._history[-1] if self._history else None
            return rolled

    def current(self) -> DatasetVersion:
        with self._lock:
            if self._current_id is None:
                raise VersionConflictError("当前没有任何已发布版本")
            return self._versions[self._current_id]

    def get(self, version_id: str) -> DatasetVersion:
        with self._lock:
            version = self._versions.get(version_id)
            if version is None:
                raise VersionConflictError(f"版本 {version_id} 不存在")
            return version
