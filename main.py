"""本地数据语义查询与版本发布服务的 HTTP 入口。"""
from __future__ import annotations

from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sqv.dataset import TableData
from sqv.errors import SQVError
from sqv.query import Aggregate, AggFunc, CompareOp, Filter, OrderKey, Query
from sqv.schema import Column, ColumnType, TableSchema
from sqv.service import QueryService
from sqv.versioning import DatasetVersion, VersionStore

app = FastAPI(title="semantic-query-versioning")
service = QueryService(VersionStore())


@app.exception_handler(SQVError)
async def sqv_error_handler(_: Request, exc: SQVError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": exc.reason, "detail": exc.message})


def _parse_query(body: dict[str, Any]) -> Query:
    try:
        return Query(
            table=body["table"],
            select=tuple(body.get("select", ())),
            filters=tuple(
                Filter(f["column"], CompareOp(f["op"]), f["value"])
                for f in body.get("filters", ())
            ),
            group_by=tuple(body.get("group_by", ())),
            aggregates=tuple(
                Aggregate(AggFunc(a["func"]), a.get("column"), a["alias"])
                for a in body.get("aggregates", ())
            ),
            order_by=tuple(
                OrderKey(o["column"], bool(o.get("descending", False)))
                for o in body.get("order_by", ())
            ),
        )
    except (KeyError, ValueError, TypeError) as exc:
        from sqv.errors import QuerySyntaxError

        raise QuerySyntaxError(f"查询请求体无法解析: {exc}") from exc


@app.post("/query")
def submit_query(body: dict[str, Any]) -> dict[str, Any]:
    result = service.submit(_parse_query(body))
    return {
        "version_id": result.version_id,
        "columns": list(result.columns),
        "rows": [list(r) for r in result.rows],
    }


@app.post("/versions")
def publish_version(body: dict[str, Any]) -> dict[str, Any]:
    tables = {
        name: TableData(
            schema=TableSchema(
                table=name,
                columns=tuple(
                    Column(c["name"], ColumnType(c["type"])) for c in t["columns"]
                ),
            ),
            rows=tuple(t.get("rows", ())),
        )
        for name, t in body["tables"].items()
    }
    version = service.publish(DatasetVersion(version_id=body["version_id"], tables=tables))
    return {"version_id": version.version_id, "status": version.status.value}


@app.post("/versions/{version_id}/rollback")
def rollback_version(version_id: str) -> dict[str, Any]:
    version = service.rollback(version_id)
    return {"version_id": version.version_id, "status": version.status.value}


@app.get("/versions/current")
def current_version() -> dict[str, Any]:
    version = service._store.current()
    return {"version_id": version.version_id, "status": version.status.value}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
