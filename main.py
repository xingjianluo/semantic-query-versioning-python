"""FastAPI 入口：暴露查询与版本发布/回滚接口。

POST /query              执行受控查询，失败返回可区分的 error code
POST /versions/publish   发布版本 {"version_id": "...", "tables": {...}}
POST /versions/rollback  回滚到上一个版本
GET  /versions           查看版本历史与当前版本
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
    query = parse_query(spec)
    return service.run(query)


@app.post("/versions/publish")
def publish(spec: dict) -> dict:
    version_id = spec.get("version_id") if isinstance(spec, dict) else None
    tables = _parse_tables(spec.get("tables") if isinstance(spec, dict) else None)
    published = service.publish(version_id, tables)
    return {"published": published, "current_version": service.current_version_id()}


@app.post("/versions/rollback")
def rollback() -> dict:
    return {"current_version": service.rollback()}


@app.get("/versions")
def versions() -> dict:
    return {"current_version": service.current_version_id(), "history": service.history()}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
