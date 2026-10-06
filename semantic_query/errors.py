"""可区分的错误类型体系：所有查询/版本失败的根因分类。"""

from __future__ import annotations


class SemanticQueryError(Exception):
    """所有服务错误的基类。code 用于机器可区分的错误原因。"""

    code: str = "internal_error"

    def __init__(self, message: str, *, detail: object = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "detail": self.detail}


class QuerySyntaxError(SemanticQueryError):
    """查询语法不支持或结构非法。"""

    code = "query_syntax_error"


class UnknownTableError(SemanticQueryError):
    """查询引用了不存在的表。"""

    code = "unknown_table"


class UnknownFieldError(SemanticQueryError):
    """查询引用了不存在（或已改名/删除）的字段。"""

    code = "unknown_field"


class TypeMismatchError(SemanticQueryError):
    """常量、比较或排序操作的类型与字段类型不匹配。"""

    code = "type_mismatch"


class InvalidAggregationError(SemanticQueryError):
    """聚合函数非法（不支持的聚合、对非数值列做 sum/avg、聚合与分组混用错误）。"""

    code = "invalid_aggregation"


class VersionNotFoundError(SemanticQueryError):
    """指定的版本不存在。"""

    code = "version_not_found"


class VersionConflictError(SemanticQueryError):
    """重复发布同一版本号但内容不一致。"""

    code = "version_conflict"


class NoActiveVersionError(SemanticQueryError):
    """尚无任何已发布版本，无法查询或回滚。"""

    code = "no_active_version"


class VersionRetiredError(SemanticQueryError):
    """指定的版本曾经发布过，但已被保留策略清理，不再可用。"""

    code = "version_retired"


class DiffIncompatibleError(SemanticQueryError):
    """两个版本无法对比（例如表集合不一致），并说明原因。"""

    code = "diff_incompatible"
