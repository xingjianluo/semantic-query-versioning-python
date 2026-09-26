"""受控查询的语义校验：各类非法查询必须被拒绝并给出可区分的错误原因。"""
from __future__ import annotations

import logging

import pytest

from sqv.errors import (
    InvalidAggregationError,
    QuerySyntaxError,
    TypeMismatchError,
    UnknownFieldError,
)
from sqv.query import Aggregate, AggFunc, CompareOp, Filter, OrderKey, Query, validate
from sqv.schema import Column, ColumnType, TableSchema

logger = logging.getLogger("test.query_validation")


@pytest.fixture
def schema() -> TableSchema:
    return TableSchema(
        "sales",
        (
            Column("id", ColumnType.INT),
            Column("region", ColumnType.STRING),
            Column("active", ColumnType.BOOL),
            Column("amount", ColumnType.FLOAT),
        ),
    )


def _expect(query: Query, schema: TableSchema, error: type[Exception], basis: str) -> None:
    logger.info("查询输入=%s | 命中版本=N/A | 判定依据=%s", query, basis)
    with pytest.raises(error) as excinfo:
        validate(query, schema)
    logger.info("拒绝原因=%s: %s", error.__name__, excinfo.value)


def _expect_ok(query: Query, schema: TableSchema, basis: str) -> None:
    logger.info("查询输入=%s | 命中版本=N/A | 判定依据=%s", query, basis)
    validate(query, schema)
    logger.info("判定结果=合法，通过校验")


def test_select_valid_and_filter_valid(schema: TableSchema) -> None:
    _expect_ok(
        Query(
            table="sales",
            select=("id", "region"),
            filters=(Filter("amount", CompareOp.GE, 10.5),),
            order_by=(OrderKey("id"),),
        ),
        schema,
        "字段均存在，int 字面量可匹配 float，排序列为已选列",
    )


def test_unknown_field_in_select(schema: TableSchema) -> None:
    _expect(
        Query(table="sales", select=("price",)),
        schema,
        UnknownFieldError,
        "select 字段 price 不在结构中",
    )


def test_unknown_field_in_filter(schema: TableSchema) -> None:
    _expect(
        Query(table="sales", select=("id",), filters=(Filter("price", CompareOp.EQ, 1),)),
        schema,
        UnknownFieldError,
        "filter 字段 price 不存在",
    )


def test_unknown_field_in_group_by(schema: TableSchema) -> None:
    _expect(
        Query(table="sales", group_by=("city",), aggregates=(Aggregate(AggFunc.COUNT, None, "n"),)),
        schema,
        UnknownFieldError,
        "group_by 字段 city 不存在",
    )


def test_type_mismatch_string_literal_for_int(schema: TableSchema) -> None:
    _expect(
        Query(table="sales", select=("id",), filters=(Filter("id", CompareOp.EQ, "1"),)),
        schema,
        TypeMismatchError,
        "int 列与字符串字面量比较",
    )


def test_type_mismatch_bool_is_not_int(schema: TableSchema) -> None:
    _expect(
        Query(table="sales", select=("id",), filters=(Filter("id", CompareOp.EQ, True),)),
        schema,
        TypeMismatchError,
        "bool 字面量不得隐式当作 int",
    )


def test_type_mismatch_int_literal_for_string(schema: TableSchema) -> None:
    _expect(
        Query(table="sales", select=("id",), filters=(Filter("region", CompareOp.EQ, 1),)),
        schema,
        TypeMismatchError,
        "string 列与 int 字面量比较",
    )


def test_sum_on_string_column_rejected(schema: TableSchema) -> None:
    _expect(
        Query(table="sales", aggregates=(Aggregate(AggFunc.SUM, "region", "s"),)),
        schema,
        InvalidAggregationError,
        "sum 只允许数值字段",
    )


def test_avg_on_string_column_rejected(schema: TableSchema) -> None:
    _expect(
        Query(table="sales", aggregates=(Aggregate(AggFunc.AVG, "region", "a"),)),
        schema,
        InvalidAggregationError,
        "avg 只允许数值字段",
    )


def test_min_on_bool_column_rejected(schema: TableSchema) -> None:
    _expect(
        Query(table="sales", aggregates=(Aggregate(AggFunc.MIN, "active", "m"),)),
        schema,
        InvalidAggregationError,
        "min/max 不支持布尔列",
    )


def test_count_star_is_valid(schema: TableSchema) -> None:
    _expect_ok(
        Query(table="sales", aggregates=(Aggregate(AggFunc.COUNT, None, "n"),)),
        schema,
        "count(*) 不需要目标字段",
    )


def test_empty_select_and_aggregates_rejected(schema: TableSchema) -> None:
    _expect(
        Query(table="sales"),
        schema,
        QuerySyntaxError,
        "select 与 aggregates 同时为空",
    )


def test_select_column_not_in_group_by_rejected(schema: TableSchema) -> None:
    _expect(
        Query(
            table="sales",
            select=("region", "amount"),
            group_by=("region",),
            aggregates=(Aggregate(AggFunc.SUM, "id", "s"),),
        ),
        schema,
        QuerySyntaxError,
        "含聚合时 select 字段必须在 group_by 中",
    )


def test_duplicate_aggregate_alias_rejected(schema: TableSchema) -> None:
    _expect(
        Query(
            table="sales",
            aggregates=(
                Aggregate(AggFunc.COUNT, None, "n"),
                Aggregate(AggFunc.SUM, "id", "n"),
            ),
        ),
        schema,
        QuerySyntaxError,
        "聚合别名必须唯一",
    )


def test_order_by_unknown_output_column_rejected(schema: TableSchema) -> None:
    _expect(
        Query(table="sales", select=("id",), order_by=(OrderKey("region"),)),
        schema,
        UnknownFieldError,
        "order_by 只能引用输出列（select 列或聚合别名），region 未被选出",
    )
