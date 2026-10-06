# semantic-query-versioning

本地可运行的数据语义查询与版本发布服务：数据集持续演进时，查询结果、版本可见性与失败原因保持一致且可解释。

## 架构

```
main.py                     FastAPI 入口（HTTP 接口）
semantic_query/
  errors.py                 可区分的错误类型体系（每类错误有稳定 code）
  models.py                 列类型 / 表结构 / 表数据 / 不可变数据集版本
  query.py                  查询 JSON 的解析与语法级校验，规范化串（缓存键）
  engine.py                 针对单个不可变版本快照的语义校验与执行
  versions.py               版本仓库：发布 / 回滚 / 原子切换 / 快照隔离 / 引用钉 / 保留清理
  diff.py                   版本对比：结构差异 + 行增删改统计
  service.py                门面：版本存储 + 执行 + 按版本隔离的结果缓存
tests/                      单测（日志打印查询输入、命中版本与判定依据）
```

## 查询语义

`POST /query`，请求体为 JSON。顶层可额外带 `"version": "v1"` 指定查询某个已发布的历史版本（省略则查当前生效版本）：

```json
{
  "version": "v1",
  "table": "orders",
  "select": ["status", {"agg": "sum", "column": "amount", "as": "total"}],
  "where": [{"column": "amount", "op": "ge", "value": 100}],
  "group_by": ["status"],
  "order_by": [{"column": "total", "desc": true}],
  "limit": 10
}
```

- `select`：列名或聚合对象；聚合支持 `count`（可省略 `column` 表示 count(*)）、`sum`、`avg`、`min`、`max`；`as` 指定别名。省略 `select` 表示选全部列。
- `where`：条件数组（AND 语义），操作符 `eq/ne/lt/le/gt/ge`；常量只支持布尔/数值/字符串/null。
- `group_by`：分组列；存在聚合时普通 select 列必须全部出现在 `group_by` 中。
- `order_by`：列名或 `{"column", "desc"}`，可引用 select 别名；null 排在最后。
- `limit`：正整数。

### 错误原因（可区分，绝不静默返回错误结果）

| code | 含义 |
|---|---|
| `query_syntax_error` | 不支持的键/操作符/聚合函数、非法 limit 等语法问题 |
| `unknown_table` | 表在当前版本中不存在 |
| `unknown_field` | 字段不存在（含已改名/删除），detail 中列出可用字段 |
| `type_mismatch` | 常量类型与列类型不匹配、行数据与结构声明不一致 |
| `invalid_aggregation` | 对非数值列 sum/avg、聚合与普通列混用但未分组 |
| `version_conflict` | 同一版本号重复发布但内容不同 |
| `no_active_version` | 无已发布版本可查询/回滚 |
| `version_not_found` | 指定版本不存在（从未发布，或已不在可回滚范围内） |
| `version_retired` | 指定版本曾发布过，但已被保留策略清理 |
| `diff_incompatible` | 两个版本无法对比（表集合不一致），detail 列出各自独有的表 |

## 历史版本查询

`POST /query` 带 `"version": "<version_id>"` 即针对该已发布版本执行：

- 指定版本一旦开始执行，整个查询期间持有该版本的不可变快照并加引用钉——并发的新版本发布、回滚甚至保留清理都不会影响本次查询看到的数据。
- 版本从未发布过 -> `version_not_found`；曾发布但已被清理 -> `version_retired`。两种原因可机器区分，绝不静默拿最新版本结果顶上。
- 缓存键仍含 version_id，同一查询串在不同版本上的结果各自缓存、互不污染。

## 版本对比

`POST /versions/diff`，请求体 `{"from": "v1", "to": "v2"}`，返回：

```json
{
  "from_version": "v1", "to_version": "v2",
  "tables": [{
    "table": "orders",
    "added_columns": ["currency"],
    "removed_columns": ["name"],
    "type_changes": [{"column": "amount", "from": "int", "to": "float"}],
    "rows": {"added": 3, "removed": 1, "modified": 2},
    "key_column": "id"
  }]
}
```

- 结构差异：每张表新增/删除的列、类型变化的列（含前后类型）。
- 行差异：两版本该表都有唯一非空的 `id` 列时按 id 对齐，给出增/删/改数量（共有列内容变化的同 id 行计为 modified；int->float 演进中 10 与 10.0 视为同值）；否则退化为行多重集比较，只报增/删（`key_column` 为 null）。
- 不可对比时响亮失败、不给半截结果：任一版本不存在 -> `version_not_found`；已清理 -> `version_retired`；两版本表集合不一致 -> `diff_incompatible`，detail 中列出各自独有的表。

## 保留清理

`POST /versions/prune`，请求体 `{"keep_last": 3}`：只保留最近 3 个历史版本（含当前生效版本），更老的版本清理掉。返回清理报告：

```json
{"keep_last": 3, "pruned": ["v1"], "kept_pinned": ["v2"],
 "remaining_history": ["v2", "v3", "v4"], "current_version": "v4"}
```

安全保证：

- **当前生效版本绝不清除**（keep_last >= 1 必然包含它）；`keep_last` 非法（非整数/< 1）-> `query_syntax_error`，状态不变。
- **正被查询引用的版本不被清除**：清理时仍被钉住的版本跳过并列入 `kept_pinned`，查询结束后再清理即可生效。
- **原子性**：新状态先算好再在同一把锁内整体替换，清理失败/中断不会留下"版本列表与可查集合不一致"的半成品；被清版本的查询缓存一并失效。
- **回滚范围跟着收窄**：`POST /versions/rollback` 可带 `{"version_id": "v2"}` 回滚到指定版本；目标已被清理 -> `version_retired`，从未发布或不在可回滚范围 -> `version_not_found`（detail 给出当前可回滚范围）。
- `GET /versions` 返回 `history`（可回滚范围）、`current_version` 与 `retired`（已清理版本列表）。

## 版本发布与回滚

- `POST /versions/publish`：`{"version_id": "v1", "tables": {"t": {"columns": [{"name": "id", "type": "int"}], "rows": [{"id": 1}]}}}`。列类型：`int/float/str/bool`。
- `POST /versions/rollback`：回滚到上一个已发布版本。
- `GET /versions`：查看历史与当前版本。

**发布语义**：先完整校验（行键必须是已声明列、值类型匹配）并构建不可变快照，再一次原子切换当前指针——发布中途失败不会留下半更新状态。重复发布同一版本号：内容指纹一致则幂等成功；不一致则 `version_conflict`，状态不变。

## 结构演进兼容规则

- **新增列**：向后兼容，针对旧结构的显式查询结果不变。
- **改名/删除列**：旧查询引用时 `unknown_field`，并列出当前可用字段——不会静默取到错误数据。
- **列类型变化**：类型与数据必须一起演进；类型与数据不一致的发布在发布时即被拒绝（`type_mismatch`）；旧类型常量的查询在新版本上同样 `type_mismatch`。
- **行缺少声明列**：按 null 处理；null 只匹配 `eq/ne`，其余比较为假。
- **行包含未声明列**：发布时拒绝（`query_syntax_error`），避免残留字段被静默忽略。
- **跨版本结果复用**：缓存键 = `(version_id, 查询规范化串)`，发布/回滚后绝不复用旧版本结果；回到旧版本时命中该版本自己的缓存。

## 并发一致性保证

- 查询在开始时取一次版本快照引用并加引用钉，整个执行期间看到同一份不可变快照；并发的发布/回滚/清理不影响本次查询。
- 版本指针的切换在锁内原子完成；查询只会看到某个完整版本，绝不会看到半更新状态。
- 保留清理与查询并发：被钉住的版本跳过清理；清理的状态替换是原子的，版本列表与可查集合始终一致。
- 并发重复发布：同内容全部幂等成功；不同内容恰好一个成功，其余 `version_conflict`。

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q -s     # 运行全部测试（-s 查看每条用例的查询输入/命中版本/判定依据日志）
uv run python main.py   # 启动本地服务（127.0.0.1，随机端口）
```

测试覆盖：非法查询分类（`test_query_validation.py`）、查询语义正确性（`test_engine.py`）、发布/回滚/缓存隔离（`test_versioning.py`）、并发发布与查询及快照隔离（`test_concurrency.py`）、结构演进兼容（`test_schema_evolution.py`）、历史版本指定查询与版本切换期间的一致性（`test_history_query.py`）、版本对比（`test_diff.py`）、保留清理及其与查询的并发（`test_prune.py`）、HTTP 接口（`test_api.py`）。
