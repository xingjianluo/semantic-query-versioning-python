"""公共 fixture 与判定日志辅助。

每个用例都会打印：查询输入、命中的版本、判定依据（成功或错误 code）。
"""

from __future__ import annotations

import logging

import pytest

from semantic_query.models import Column, ColumnType, Table, TableSchema
from semantic_query.service import QueryService

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("semantic-query-test")


def log_decision(case: str, query: object, version: object, rationale: str) -> None:
    log.info("[case=%s] query=%s hit_version=%s rationale=%s", case, query, version, rationale)


def orders_schema_v1() -> TableSchema:
    return TableSchema(
        "orders",
        (
            Column("id", ColumnType.INT),
            Column("status", ColumnType.STR),
            Column("amount", ColumnType.FLOAT),
        ),
    )


def orders_rows_v1() -> list[dict]:
    return [
        {"id": 1, "status": "paid", "amount": 100.0},
        {"id": 2, "status": "paid", "amount": 250.0},
        {"id": 3, "status": "void", "amount": 75.0},
        {"id": 4, "status": "paid", "amount": 40.0},
    ]


@pytest.fixture
def service() -> QueryService:
    svc = QueryService()
    svc.publish("v1", {"orders": Table(orders_schema_v1(), orders_rows_v1())})
    return svc
