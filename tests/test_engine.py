"""查询语义正确性：选列、过滤、分组聚合、排序、limit。"""

from __future__ import annotations

from semantic_query.query import parse_query

from .conftest import log_decision


def test_select_and_filter(service):
    q = parse_query({
        "table": "orders",
        "select": ["id", "amount"],
        "where": [{"column": "status", "op": "eq", "value": "paid"},
                  {"column": "amount", "op": "gt", "value": 50}],
        "order_by": [{"column": "amount", "desc": True}],
    })
    r = service.run(q)
    assert r["rows"] == [{"id": 2, "amount": 250.0}, {"id": 1, "amount": 100.0}]
    log_decision("select_filter_sort", "paid & amount>50 desc", r["version_id"],
                 "过滤后按 amount 降序，命中 id=2,1")


def test_group_by_aggregates(service):
    q = parse_query({
        "table": "orders",
        "select": ["status",
                   {"agg": "count", "as": "n"},
                   {"agg": "sum", "column": "amount", "as": "total"},
                   {"agg": "avg", "column": "amount", "as": "avg_amount"},
                   {"agg": "max", "column": "amount", "as": "peak"}],
        "group_by": ["status"],
        "order_by": ["status"],
    })
    r = service.run(q)
    assert r["rows"] == [
        {"status": "paid", "n": 3, "total": 390.0, "avg_amount": 130.0, "peak": 250.0},
        {"status": "void", "n": 1, "total": 75.0, "avg_amount": 75.0, "peak": 75.0},
    ]
    log_decision("group_agg", "count/sum/avg/max by status", r["version_id"],
                 "paid: n=3 total=390 avg=130 max=250；void: n=1")


def test_order_by_alias_and_limit(service):
    q = parse_query({
        "table": "orders",
        "select": ["status", {"agg": "sum", "column": "amount", "as": "total"}],
        "group_by": ["status"],
        "order_by": [{"column": "total", "desc": True}],
        "limit": 1,
    })
    r = service.run(q)
    assert r["rows"] == [{"status": "paid", "total": 390.0}]
    log_decision("order_alias_limit", "top1 by total", r["version_id"],
                 "按聚合别名 total 排序并 limit 1")


def test_deterministic_repeatable_results(service):
    q = parse_query({"table": "orders", "order_by": ["id"]})
    results = [service.run(q)["rows"] for _ in range(5)]
    assert all(r == results[0] for r in results)
    log_decision("deterministic", "select * order by id x5", service.current_version_id(),
                 "同一版本同一查询 5 次结果完全一致")
