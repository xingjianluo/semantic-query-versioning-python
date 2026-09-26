"""可区分的错误类型定义。

所有受控查询与版本操作的失败都必须抛出下列具体异常之一，
调用方可依据异常类型与 reason 字段区分失败原因。
"""
from __future__ import annotations


class SQVError(Exception):
    """服务内所有受控错误的基类。"""

    reason: str = "internal_error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class QuerySyntaxError(SQVError):
    """查询语法不支持或结构非法。"""

    reason = "query_syntax_error"


class UnknownFieldError(SQVError):
    """查询引用了数据表中不存在的字段。"""

    reason = "unknown_field"


class TypeMismatchError(SQVError):
    """过滤条件或排序中字段类型与字面量类型不匹配。"""

    reason = "type_mismatch"


class InvalidAggregationError(SQVError):
    """聚合函数非法或聚合目标字段类型不支持。"""

    reason = "invalid_aggregation"


class VersionConflictError(SQVError):
    """发布/回滚与版本状态冲突（如重复发布、版本不存在）。"""

    reason = "version_conflict"


class IncompatibleSchemaError(SQVError):
    """查询针对的结构版本与数据不兼容，拒绝执行而非静默返回错误数据。"""

    reason = "incompatible_schema"
