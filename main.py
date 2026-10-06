"""FastAPI 入口：暴露查询与版本发布/回滚/对比/清理接口。

POST /query              执行受控查询（可带 "version" 指定历史版本），失败返回可区分的 error code
POST /versions/publish   发布版本 {"version_id": "...", "tables": {...}}
POST /versions/rollback  回滚到上一个版本；带 {"version_id": "..."} 则回滚到指定版本
POST /versions/diff      对比两个版本 {"from": "v1", "to": "v2"}
POST /versions/prune     按保留范围清理老版本 {"keep_last": 3}
GET  /versions           查看版本历史、当前版本与已清理版本
"""

from __future__ import annotations

from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
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


@app.get("/")
def read_root() -> dict:
    return {"service": "semantic-query-versioning", "current_version": service.current_version_id()}


@app.post("/query")
def run_query(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise QuerySyntaxError(f"查询必须是 JSON 对象, 得到: {spec!r}")
    version_id = spec.get("version")
    if version_id is not None and not isinstance(version_id, str):
        raise QuerySyntaxError(f"version 必须是字符串, 得到: {version_id!r}")
    body = {k: v for k, v in spec.items() if k != "version"}
    query = parse_query(body)
    return service.run(query, version_id=version_id)


@app.post("/versions/publish")
def publish(spec: dict) -> dict:
    version_id = spec.get("version_id") if isinstance(spec, dict) else None
    tables = _parse_tables(spec.get("tables") if isinstance(spec, dict) else None)
    published = service.publish(version_id, tables)
    return {"published": published, "current_version": service.current_version_id()}


@app.post("/versions/rollback")
def rollback(spec: dict | None = None) -> dict:
    if isinstance(spec, dict) and spec.get("version_id") is not None:
        version_id = spec["version_id"]
        if not isinstance(version_id, str):
            raise QuerySyntaxError(f"version_id 必须是字符串, 得到: {version_id!r}")
        return {"current_version": service.rollback_to(version_id)}
    return {"current_version": service.rollback()}


@app.post("/versions/diff")
def diff(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise QuerySyntaxError(f"diff 请求必须是 JSON 对象, 得到: {spec!r}")
    from_version, to_version = spec.get("from"), spec.get("to")
    if not isinstance(from_version, str) or not isinstance(to_version, str):
        raise QuerySyntaxError(
            f"diff 需要字符串 from/to, 得到: from={from_version!r} to={to_version!r}"
        )
    return service.diff(from_version, to_version)


@app.post("/versions/prune")
def prune(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise QuerySyntaxError(f"prune 请求必须是 JSON 对象, 得到: {spec!r}")
    return service.prune(spec.get("keep_last"))


@app.get("/versions")
def versions() -> dict:
    return {
        "current_version": service.current_version_id(),
        "history": service.history(),
        "retired": service.retired(),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
