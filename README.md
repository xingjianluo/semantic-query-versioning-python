# 数据语义查询与版本发布服务

一个可在本地运行的服务：对本地定义的数据表提交**受控查询**，并支持数据集版本的
**发布与回滚**。数据集持续演进时，查询结果、版本可见性与失败原因保持一致且可解释。

## 运行环境

- Python ≥ 3.14，依赖管理使用 [uv](https://docs.astral.sh/uv/)
- 安装依赖：`uv sync`

## 支持的查询语义

查询通过 `sqv.query.Query` 表达（HTTP 接口为 `POST /query` 的 JSON），支持：

| 能力 | 说明 |
| --- | --- |
| 选列 `select` | 指定输出列；含聚合时 select 列必须出现在 `group_by` 中 |
| 过滤 `filters` | 比较操作：`eq / ne / lt / le / gt / ge`；缺失值（null）不满足任何比较条件 |
| 分组聚合 `group_by` + `aggregates` | 聚合函数：`count`（支持 `count(*)`）、`sum`、`avg`、`min`、`max`；聚合别名必须非空且唯一 |
| 排序 `order_by` | 只能引用输出列（select 列或聚合别名），支持升/降序；null 恒排最后 |

### 错误分类（绝不静默返回错误结果）

所有失败都抛出具体异常，HTTP 层返回 `{"error": <reason>, "detail": <说明>}`：

| reason | 触发场景 |
| --- | --- |
| `query_syntax_error` | 语法不支持或结构非法（select 与 aggregates 全空、聚合别名重复/为空、含聚合时 select 列不在 group_by 中） |
| `unknown_field` | 查询引用了不存在的字段、表或不可排序的输出列 |
| `type_mismatch` | 过滤字面量与列类型不匹配（bool 不隐式当作 int） |
| `invalid_aggregation` | 对非数值列做 `sum/avg`、对布尔列做 `min/max`、聚合缺少目标字段 |
| `version_conflict` | 重复发布、回滚不存在/非当前/已回滚的版本、无已发布版本时查询 |
| `incompatible_schema` | 查询针对的结构与数据实际结构不兼容 |

## 版本兼容规则

针对"查询编写时的结构"（`assumed_schema`）与"数据实际结构"逐列判定：

- **新增字段**：兼容。数据侧多出字段不影响旧查询；
- **字段删除或改名**：不兼容。拒绝执行并抛出 `incompatible_schema`，
  绝不静默映射到其他列取错位数据；不携带旧结构时按当前快照结构校验，
  引用不存在字段报 `unknown_field`；
- **类型变化**：仅 `int → float` 的加宽兼容；`float → int` 及与
  `string/bool` 的互转一律不兼容；
- **结果复用**：查询结果缓存以版本号为命中条件，跨版本绝不复用，
  不会返回过期数据。

## 并发一致性保证

- 版本仓库（`VersionStore`）内所有状态变更在同一把锁内完成，发布提交是
  指针级原子操作：查询要么看到完整的旧快照，要么看到完整的新快照，
  绝不会看到半更新状态；
- 快照（`DatasetVersion` / `TableData`）为不可变对象，`submit` 全程持有
  同一份快照引用，已开始的查询在执行期间不受并发发布/回滚影响；
- 重复发布同一版本号：恰好一次成功，其余得到确定的 `version_conflict`；
- 回滚仅允许针对当前版本，回滚后当前指针回退到上一个仍为已发布状态的版本。

## 本地验证方法

```bash
uv sync                 # 安装依赖
uv run pytest           # 运行全部测试（日志会打印查询输入、命中版本与判定依据）
uv run pytest tests/test_concurrency.py -q   # 仅并发场景
uv run python main.py   # 启动本地 HTTP 服务
```

HTTP 接口示例：

```bash
# 发布版本
curl -X POST localhost:8000/versions -H 'content-type: application/json' -d '{
  "version_id": "v1",
  "tables": {"t": {"columns": [{"name": "id", "type": "int"}],
                   "rows": [{"id": 1}, {"id": 2}]}}}'
# 提交查询
curl -X POST localhost:8000/query -H 'content-type: application/json' -d '{
  "table": "t", "select": ["id"],
  "filters": [{"column": "id", "op": "gt", "value": 1}]}'
# 回滚
curl -X POST localhost:8000/versions/v1/rollback
```

## 代码结构

```
sqv/
  errors.py      # 可区分的错误类型（reason 字段）
  schema.py      # 列类型与表结构定义
  query.py       # 查询 AST 与语义校验
  dataset.py     # 不可变表数据快照
  versioning.py  # 版本发布/回滚/快照仓库（线程安全）
  compat.py      # 跨版本结构兼容规则
  executor.py    # 单快照查询执行器
  service.py     # 服务门面（校验 + 兼容检查 + 快照 + 缓存）
main.py          # FastAPI HTTP 入口
tests/           # 查询校验、版本回滚、结构兼容、并发一致性测试
```
