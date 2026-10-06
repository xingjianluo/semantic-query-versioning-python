"""两个已发布版本的结构与数据对比。

规则（响亮、完整，绝不给半截结果）：
- 任一侧版本不存在/已清理 -> 由存储层抛 version_not_found / version_unavailable。
- 两侧表集合不一致（某张表只在其中一个版本里）-> versions_incomparable，
  detail 中列出 added/removed 表，不返回任何部分对比结果。
- 公共表对比字段：added / removed / type_changed。
- 行级变化：默认整行内容对比，只统计 added/removed；
  通过 match_keys 为表指定业务匹配键后，可区分 updated / unchanged。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import UnknownFieldError, VersionsIncomparableError
from .models import DatasetVersion, Table


@dataclass(frozen=True)
class FieldChange:
    table: str
    field: str
    kind: str  # added / removed / type_changed
    old_type: str | None = None
    new_type: str | None = None


@dataclass(frozen=True)
class RowChangeSummary:
    table: str
    added: int
    removed: int
    updated: int
    unchanged: int
    matched_by: tuple[str, ...] = ()  # 用于识别同一行的匹配键；为空时只能统计整行增删

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "added": self.added,
            "removed": self.removed,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "matched_by": list(self.matched_by),
        }


@dataclass
class VersionDiff:
    old_version: str
    new_version: str
    added_tables: list[str] = field(default_factory=list)
    removed_tables: list[str] = field(default_factory=list)
    field_changes: list[FieldChange] = field(default_factory=list)
    row_changes: list[RowChangeSummary] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "old_version": self.old_version,
            "new_version": self.new_version,
            "tables": {
                "added": self.added_tables,
                "removed": self.removed_tables,
            },
            "fields": [
                {
                    "table": fc.table,
                    "field": fc.field,
                    "kind": fc.kind,
                    "old_type": fc.old_type,
                    "new_type": fc.new_type,
                }
                for fc in self.field_changes
            ],
            "rows": [rc.to_dict() for rc in self.row_changes],
        }


def _diff_fields(table_name: str, old: Table, new: Table) -> list[FieldChange]:
    old_cols = old.schema.column_map()
    new_cols = new.schema.column_map()
    changes: list[FieldChange] = []
    for name in sorted(set(old_cols) | set(new_cols)):
        oc, nc = old_cols.get(name), new_cols.get(name)
        if oc is None:
            changes.append(FieldChange(table_name, name, "added", None, nc.type.value))
        elif nc is None:
            changes.append(FieldChange(table_name, name, "removed", oc.type.value, None))
        elif oc.type is not nc.type:
            changes.append(
                FieldChange(table_name, name, "type_changed", oc.type.value, nc.type.value)
            )
    return changes


def _project(row: dict[str, Any], columns: tuple[str, ...]) -> dict[str, Any]:
    """只取结构声明列；行中残留的未声明键不参与对比。"""
    return {c: row.get(c) for c in columns}


def _rows_by_key(rows: list[dict], keys: tuple[str, ...]) -> dict[tuple, dict]:
    """按匹配键分组；重复键 last-wins（数据集的匹配键应唯一）。"""
    return {tuple(r.get(k) for k in keys): r for r in rows}


def _diff_rows(table_name: str, old: Table, new: Table,
               keys: tuple[str, ...] | None) -> RowChangeSummary:
    old_cols = tuple(c.name for c in old.schema.columns)
    new_cols = tuple(c.name for c in new.schema.columns)
    old_rows = [_project(r, old_cols) for r in old.rows]
    new_rows = [_project(r, new_cols) for r in new.rows]

    if not keys:
        # 无匹配键：只能做整行内容集合对比
        old_set = {tuple(sorted(r.items())) for r in old_rows}
        new_set = {tuple(sorted(r.items())) for r in new_rows}
        return RowChangeSummary(
            table=table_name,
            added=len(new_set - old_set),
            removed=len(old_set - new_set),
            updated=0,
            unchanged=len(old_set & new_set),
        )

    old_by_key = _rows_by_key(old_rows, keys)
    new_by_key = _rows_by_key(new_rows, keys)
    added = len(set(new_by_key) - set(old_by_key))
    removed = len(set(old_by_key) - set(new_by_key))
    # 结构可能演进：匹配行是否“未变”只按两侧公共列判断；
    # 新增/删除列本身已在 field_changes 中报告。
    common = tuple(c for c in old_cols if c in set(new_cols))
    updated = unchanged = 0
    for k in set(old_by_key) & set(new_by_key):
        old_view = {c: old_by_key[k].get(c) for c in common}
        new_view = {c: new_by_key[k].get(c) for c in common}
        if old_view == new_view:
            unchanged += 1
        else:
            updated += 1
    return RowChangeSummary(
        table=table_name,
        added=added,
        removed=removed,
        updated=updated,
        unchanged=unchanged,
        matched_by=keys,
    )


def diff_versions(
    old: DatasetVersion,
    new: DatasetVersion,
    match_keys: dict[str, tuple[str, ...]] | None = None,
) -> VersionDiff:
    """对比两个版本；表集合不一致时抛 VersionsIncomparableError，不返回半截结果。

    match_keys: {表名: (匹配键列名, ...)}；匹配键列在任一公共表中不存在 -> UnknownFieldError。
    """
    match_keys = match_keys or {}
    old_tables = set(old.tables)
    new_tables = set(new.tables)
    added = sorted(new_tables - old_tables)
    removed = sorted(old_tables - new_tables)
    if added or removed:
        raise VersionsIncomparableError(
            f"版本 {old.version_id!r} 与 {new.version_id!r} 表集合不一致，无法对比",
            detail={
                "old_version": old.version_id,
                "new_version": new.version_id,
                "tables_only_in_new": added,
                "tables_only_in_old": removed,
            },
        )

    result = VersionDiff(old_version=old.version_id, new_version=new.version_id)
    for name in sorted(old_tables):
        ot, nt = old.tables[name], new.tables[name]
        result.field_changes.extend(_diff_fields(name, ot, nt))
        keys = match_keys.get(name)
        if keys:
            for side, tbl in (("old", ot), ("new", nt)):
                available = {c.name for c in tbl.schema.columns}
                missing = [k for k in keys if k not in available]
                if missing:
                    side_vid = old.version_id if side == "old" else new.version_id
                    raise UnknownFieldError(
                        f"表 {name!r} 在{side}版本 {side_vid} 中缺少匹配键 {missing}",
                        detail={"table": name, "missing": missing, "side": side},
                    )
        result.row_changes.append(_diff_rows(name, ot, nt, tuple(keys) if keys else None))
    return result
