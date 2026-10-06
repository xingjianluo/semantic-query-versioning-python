"""FastAPI 入口：暴露查询、历史版本查询/对比、版本发布/回滚/保留清理接口。

POST /query                       执行受控查询（可用 {"version_id": ...} 指定历史版本）
POST /versions/publish            发布版本 {"version_id": "...", "tables": {...}}
POST /versions/rollback           回滚到上一个版本
POST /versions/diff               对比两个版本 {"old": "v1", "new": "v2", "match_keys": {...}}
POST /versions/purge              保留清理 {"keep": 2}
GET  /versions                    查看版本历史与当前版本
"""

from __future__ import annotations

from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from semantic_query.errors import QuerySyntaxError, SemanticQueryError
from semantic_query.models import Column, ColumnType, Table, TableSchema
from semantic_query.query import parse_query
from semantic_query.service import QueryService

app = FastAPI(title="semantic-query-versioning")
service = QueryService()


@app.exception_handler(SemanticQueryError)
async def semantic_error_handler(_: Request, exc: SemanticQueryError) -> JSONResponse:
    return JSONResponse(status_code=400, content=exc.to_dict())


def _parse_tables(spec: Any) -> dict[str, Table]:
    if not isinstance(spec, dict):
        raise QuerySyntaxError(f"tables 必须是对象, 得到: {spec!r}")
    tables: dict[str, Table] = {}
    for name, t in spec.items():
        if not isinstance(t, dict) or "columns" not in t:
            raise QuerySyntaxError(f"表 {name!r} 需要 columns 定义")
        columns = []
        for c in t["columns"]:
            try:
                columns.append(Column(name=c["name"], type=ColumnType(c["type"])))
            except (KeyError, ValueError, TypeError) as e:
                raise QuerySyntaxError(f"表 {name!r} 的列定义非法: {c!r} ({e})")
        rows = t.get("rows", [])
        if not isinstance(rows, list):
            raise QuerySyntaxError(f"表 {name!r} 的 rows 必须是数组")
        tables[name] = Table(schema=TableSchema(name=name, columns=tuple(columns)), rows=rows)
    return tables


def _parse_match_keys(spec: Any) -> dict[str, tuple[str, ...]]:
    if spec is None:
        return {}
    if not isinstance(spec, dict):
        raise QuerySyntaxError(f"match_keys 必须是对象, 得到: {spec!r}")
    keys: dict[str, tuple[str, ...]] = {}
    for table, cols in spec.items():
        if not isinstance(cols, list) or not cols or not all(
            isinstance(c, str) and c for c in cols
        ):
            raise QuerySyntaxError(
                f"match_keys[{table!r}] 必须是非空字符串数组, 得到: {cols!r}"
            )
        keys[table] = tuple(cols)
    return keys


@app.get("/")
def read_root() -> dict:
    return {"service": "semantic-query-versioning", "current_version": service.current_version_id()}


@app.post("/query")
def run_query(spec: dict) -> dict:
    body = spec if isinstance(spec, dict) else {}
    version_id = body.get("version_id")
    if version_id is not None and (not isinstance(version_id, str) or not version_id):
        raise QuerySyntaxError(f"version_id 必须是非空字符串, 得到: {version_id!r}")
    query_spec = {k: v for k, v in body.items() if k != "version_id"}
    query = parse_query(query_spec)
    return service.run(query, version_id=version_id)


@app.post("/versions/publish")
def publish(spec: dict) -> dict:
    version_id = spec.get("version_id") if isinstance(spec, dict) else None
    tables = _parse_tables(spec.get("tables") if isinstance(spec, dict) else None)
    published = service.publish(version_id, tables)
    return {"published": published, "current_version": service.current_version_id()}


@app.post("/versions/rollback")
def rollback(spec: dict | None = None) -> dict:
    # 无请求体或 {}：回退到上一个版本；{"version_id": "vX"}：回退到指定版本
    target = spec.get("version_id") if isinstance(spec, dict) else None
    if target is None:
        return {"current_version": service.rollback()}
    if not isinstance(target, str) or not target:
        raise QuerySyntaxError(f"version_id 必须是非空字符串, 得到: {target!r}")
    return {"current_version": service.rollback_to(target)}


@app.post("/versions/diff")
def diff(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise QuerySyntaxError(f"diff 请求体必须是对象, 得到: {spec!r}")
    old_id, new_id = spec.get("old"), spec.get("new")
    for key, val in (("old", old_id), ("new", new_id)):
        if not isinstance(val, str) or not val:
            raise QuerySyntaxError(f"diff 缺少非空字符串 {key!r}, 得到: {val!r}")
    match_keys = _parse_match_keys(spec.get("match_keys"))
    return service.diff(old_id, new_id, match_keys).to_dict()


@app.post("/versions/purge")
def purge(spec: dict) -> dict:
    if not isinstance(spec, dict) or "keep" not in spec:
        raise QuerySyntaxError("purge 请求体必须包含 keep 字段")
    return service.purge(spec["keep"])


@app.get("/versions")
def versions() -> dict:
    return {"current_version": service.current_version_id(), "history": service.history()}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
