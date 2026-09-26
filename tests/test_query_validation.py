"""非法查询必须被拒绝，且错误原因可区分。"""

from __future__ import annotations

import pytest

from semantic_query.errors import (
    InvalidAggregationError,
    QuerySyntaxError,
    TypeMismatchError,
    UnknownFieldError,
    UnknownTableError,
)
from semantic_query.query import parse_query

from .conftest import log_decision


def test_syntax_error_unsupported_key(service):
    with pytest.raises(QuerySyntaxError) as exc:
        parse_query({"table": "orders", "join": "other"})
    log_decision("syntax_unknown_key", {"join": "other"}, None, f"rejected code={exc.value.code}")


def test_syntax_error_bad_operator(service):
    with pytest.raises(QuerySyntaxError) as exc:
        parse_query({"table": "orders", "where": [{"column": "id", "op": "like", "value": 1}]})
    log_decision("syntax_bad_op", "op=like", None, f"rejected code={exc.value.code}")


def test_unknown_table(service):
    q = parse_query({"table": "customers"})
    with pytest.raises(UnknownTableError) as exc:
        service.run(q)
    log_decision("unknown_table", "table=customers", service.current_version_id(),
                 f"rejected code={exc.value.code}")


def test_unknown_field(service):
    q = parse_query({"table": "orders", "select": ["customer_name"]})
    with pytest.raises(UnknownFieldError) as exc:
        service.run(q)
    log_decision("unknown_field", "select=customer_name", service.current_version_id(),
                 f"rejected code={exc.value.code} detail={exc.value.detail}")


def test_type_mismatch_constant(service):
    q = parse_query({"table": "orders", "where": [{"column": "amount", "op": "eq", "value": "abc"}]})
    with pytest.raises(TypeMismatchError) as exc:
        service.run(q)
    log_decision("type_mismatch", "amount eq 'abc'", service.current_version_id(),
                 f"rejected code={exc.value.code}")


def test_invalid_aggregation_on_string(service):
    q = parse_query({"table": "orders", "select": [{"agg": "sum", "column": "status"}]})
    with pytest.raises(InvalidAggregationError) as exc:
        service.run(q)
    log_decision("invalid_agg_sum_str", "sum(status)", service.current_version_id(),
                 f"rejected code={exc.value.code}")


def test_invalid_aggregation_plain_column_without_group_by(service):
    q = parse_query({"table": "orders", "select": ["status", {"agg": "count"}]})
    with pytest.raises(InvalidAggregationError) as exc:
        service.run(q)
    log_decision("invalid_agg_mixed", "select status,count(*)", service.current_version_id(),
                 f"rejected code={exc.value.code}")


def test_error_codes_are_distinct():
    codes = {
        QuerySyntaxError.code,
        UnknownTableError.code,
        UnknownFieldError.code,
        TypeMismatchError.code,
        InvalidAggregationError.code,
    }
    assert len(codes) == 5
    log_decision("error_codes_distinct", None, None, f"codes={sorted(codes)}")
