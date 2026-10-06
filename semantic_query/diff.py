"""版本对比：比较两个已发布版本的结构与数据差异。

- 结构差异：每张表新增/删除/类型变化的列。
- 数据差异：每张表行的增/删/改数量（有稳定 id 列时按 id 对齐识别修改，
  否则退化为多重集比较，只报增删）。
- 两个版本表集合不一致时抛 DiffIncompatibleError，绝不返回半截结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import DiffIncompatibleError
from .models import DatasetVersion


@dataclass(frozen=True)
class ColumnTypeChange:
    name: str
    old_type: str
    new_type: str


@dataclass(frozen=True)
class TableDiff:
    """单张表的结构与行差异。"""

    table: str
    added_columns: tuple[str, ...] = ()
    removed_columns: tuple[str, ...] = ()
    type_changes: tuple[ColumnTypeChange, ...] = ()
    rows_added: int = 0
    rows_removed: int = 0
    rows_modified: int = 0
    key_column: str | None = None  # 用于对齐行的键列；None 表示多重集比较

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "added_columns": list(self.added_columns),
            "removed_columns": list(self.removed_columns),
            "type_changes": [
                {"column": c.name, "from": c.old_type, "to": c.new_type}
                for c in self.type_changes
            ],
            "rows": {
                "added": self.rows_added,
                "removed": self.rows_removed,
                "modified": self.rows_modified,
            },
            "key_column": self.key_column,
        }


@dataclass(frozen=True)
class VersionDiff:
    """两个版本之间的完整差异。"""

    from_version: str
    to_version: str
    tables: tuple[TableDiff, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "tables": [t.to_dict() for t in self.tables],
        }


def _pick_key_column(old: DatasetVersion, new: DatasetVersion, table: str) -> str | None:
    """若两版本的该表都有 id 列且值唯一非空，则用作行对齐键；否则 None。"""
    old_table, new_table = old.tables[table], new.tables[table]
    for t in (old_table, new_table):
        if "id" not in t.schema.column_map():
            return None
        ids = [row.get("id") for row in t.rows]
        if any(v is None for v in ids) or len(set(ids)) != len(ids):
            return None
    return "id"


def _row_signature(row: dict, columns: tuple) -> tuple:
    """行在指定列集合上的规范化表示（用于多重集比较）。

    数值统一按 float 规范化：int->float 的类型演进中 10 与 10.0 视为同值，
    与引擎的类型兼容规则（int 可填入 float 列）一致。
    """
    import json

    def norm(v: Any) -> str:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = float(v)
        return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)

    return tuple(norm(row.get(c)) for c in columns)


def _diff_table(old: DatasetVersion, new: DatasetVersion, table: str) -> TableDiff:
    old_table, new_table = old.tables[table], new.tables[table]
    old_cols = old_table.schema.column_map()
    new_cols = new_table.schema.column_map()

    added = tuple(sorted(set(new_cols) - set(old_cols)))
    removed = tuple(sorted(set(old_cols) - set(new_cols)))
    type_changes = tuple(
        ColumnTypeChange(name=n, old_type=old_cols[n].type.value, new_type=new_cols[n].type.value)
        for n in sorted(set(old_cols) & set(new_cols))
        if old_cols[n].type is not new_cols[n].type
    )

    key = _pick_key_column(old, new, table)
    if key is not None:
        # 按 id 对齐：同 id 内容不同 -> 修改；否则增/删
        old_rows = {r["id"]: r for r in old_table.rows}
        new_rows = {r["id"]: r for r in new_table.rows}
        common_cols = tuple(sorted(set(old_cols) & set(new_cols)))
        rows_added = sum(1 for k in new_rows if k not in old_rows)
        rows_removed = sum(1 for k in old_rows if k not in new_rows)
        rows_modified = sum(
            1
            for k in old_rows.keys() & new_rows.keys()
            if _row_signature(old_rows[k], common_cols)
            != _row_signature(new_rows[k], common_cols)
        )
    else:
        # 无稳定键：退化为行多重集比较，只报增删
        old_cols_ordered = tuple(c.name for c in old_table.schema.columns)
        new_cols_ordered = tuple(c.name for c in new_table.schema.columns)
        old_sigs = sorted(_row_signature(r, old_cols_ordered) for r in old_table.rows)
        new_sigs = sorted(_row_signature(r, new_cols_ordered) for r in new_table.rows)
        from collections import Counter

        old_counts, new_counts = Counter(old_sigs), Counter(new_sigs)
        rows_removed = sum((old_counts - new_counts).values())
        rows_added = sum((new_counts - old_counts).values())
        rows_modified = 0

    return TableDiff(
        table=table,
        added_columns=added,
        removed_columns=removed,
        type_changes=type_changes,
        rows_added=rows_added,
        rows_removed=rows_removed,
        rows_modified=rows_modified,
        key_column=key,
    )


def diff_versions(old: DatasetVersion, new: DatasetVersion) -> VersionDiff:
    """比较两个版本；表集合不一致时抛 DiffIncompatibleError。"""
    old_tables = set(old.tables)
    new_tables = set(new.tables)
    if old_tables != new_tables:
        raise DiffIncompatibleError(
            f"版本 {old.version_id!r} 与 {new.version_id!r} 的表集合不一致，无法对比："
            f"仅 {old.version_id} 有 {sorted(old_tables - new_tables)}，"
            f"仅 {new.version_id} 有 {sorted(new_tables - old_tables)}",
            detail={
                "from_version": old.version_id,
                "to_version": new.version_id,
                "only_in_from": sorted(old_tables - new_tables),
                "only_in_to": sorted(new_tables - old_tables),
            },
        )
    return VersionDiff(
        from_version=old.version_id,
        to_version=new.version_id,
        tables=tuple(_diff_table(old, new, t) for t in sorted(old_tables)),
    )
