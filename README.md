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
  diff.py                   两个已发布版本的结构/数据对比
  versions.py               版本仓库：发布 / 回滚 / 定向回退 / 引用计数 / 保留清理 / 快照隔离
  service.py                门面：版本存储 + 执行 + 历史查询 + 对比 + 清理 + 按版本隔离缓存
tests/                      单测（日志打印查询输入、命中版本与判定依据）
```

## 查询语义

`POST /query`，请求体为 JSON：

```json
{
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
| `version_not_found` | 指定版本从未发布 |
| `version_unavailable` | 指定版本曾发布但已被保留策略清理 |
| `versions_incomparable` | 两版本表集合不一致等无法对比的情况 |
| `version_in_use` | 保留清理候选版本正被查询引用（保留码，当前实现为跳过而非报错） |

## 历史版本查询

`POST /query` 在查询体中可选带上 `"version_id"`：

```json
{ "table": "orders", "version_id": "v2026-09" }
```

- 不带 `version_id`：查当前生效版本（行为与旧版完全一致）。
- 带上 `version_id`：针对那个已发布版本取结果，响应中的 `version_id` 回显实际命中的版本。
- **失败原因可区分，绝不静默拿最新版本顶替**：
  - 从未发布 → `version_not_found`，detail 列出已知版本；
  - 曾发布但已被保留策略清理 → `version_unavailable`，detail 给出 `available` 与 `current`。
- **一致性**：指定版本查询在开始时通过引用计数 pin 住该版本快照，整个执行期间无论外面并发发布新版本、回滚/切换版本还是执行保留清理，本次查询都看到自己那一份完整、一致的数据；pin 期间该版本也不会被清掉（查询在 `finally` 中释放引用）。

Python 接口：`service.run(query, version_id="v1")`。

## 版本对比

`POST /versions/diff`：

```json
{
  "old": "v1",
  "new": "v2",
  "match_keys": {"orders": ["id"], "order_items": ["order_id", "sku"]}
}
```

返回结构：

```json
{
  "old_version": "v1",
  "new_version": "v2",
  "tables": {"added": [], "removed": []},
  "fields": [
    {"table": "orders", "field": "channel", "kind": "added", "old_type": null, "new_type": "str"},
    {"table": "orders", "field": "amount", "kind": "type_changed", "old_type": "float", "new_type": "int"}
  ],
  "rows": [
    {"table": "orders", "added": 3, "removed": 1, "updated": 8, "unchanged": 42, "matched_by": ["id"]}
  ]
}
```

- **字段级**：`added` / `removed` / `type_changed`（含旧/新类型）。
- **行级**：`added`/`removed` 按匹配键识别新行/消失行；匹配上的行比较**两侧公共列**，分为 `updated` / `unchanged`——新增列不会把所有行误判为"已更新"。
- **无匹配键时的兼容程度**：不给某张表的 `match_keys` 时，只做整行内容集合对比，只报告 `added`/`removed`/`unchanged`，`updated` 恒为 0，不臆测行级修改。
- 匹配键列在任一版本结构中不存在 → `unknown_field`。
- **无法对比时响亮失败、不给半截结果**：任一版本未发布 → `version_not_found`；已清理 → `version_unavailable`；某张表只在其中一个版本里 → `versions_incomparable`，detail 中给出 `tables_only_in_old` / `tables_only_in_new`。

Python 接口：`service.diff("v1", "v2", {"orders": ("id",)})`。

## 保留清理

`POST /versions/purge`，请求体 `{"keep": 2}`：保留最近 2 个发布版本，清理更老的、当前未在用的版本。

返回：

```json
{"keep": 2, "purged": ["v1"], "kept": ["v2", "v3"], "skipped_in_use": [], "current": "v3"}
```

- **保留窗口**：按发布先后的最后 `keep` 个版本始终保留（`keep` 必须为 ≥1 的整数，否则 `query_syntax_error`）。
- **当前生效版本**：无论是否落在窗口内都不会被清掉。
- **查询引用保护**：正被查询 pin 住的版本出现在 `skipped_in_use` 中，本次跳过；查询结束、引用释放后，下一次清理自然生效。
- **原子性与一致性**：删除版本、更新清单在同一把锁内一次完成；中途异常不会留下"版本列表与实际可查版本对不上"的状态。被清版本的结果缓存同步删除。
- **清理后的行为**：
  - 被清版本进入"墓碑"，显式查询/对比它返回 `version_unavailable`，不会回退到最新版本；
  - 同名版本号可以重新发布（重新发布即移出墓碑，作为新版本生效）；
  - 回退范围收窄：`POST /versions/rollback` 无请求体时回退到栈内上一个版本；
    带 `{"version_id": "v1"}` 可定向回退到任意仍可用版本，退到已清理版本时返回
    `version_unavailable`，退到从未发布的版本返回 `version_not_found`。

Python 接口：`service.purge(2)`、`service.rollback_to("v1")`。

## 版本发布与回滚

- `POST /versions/publish`：`{"version_id": "v1", "tables": {"t": {"columns": [{"name": "id", "type": "int"}], "rows": [{"id": 1}]}}}`。列类型：`int/float/str/bool`。
- `POST /versions/rollback`：无请求体时回滚到上一个版本；`{"version_id": "v1"}` 定向回退到指定的可用历史版本。
- `POST /versions/diff`：对比两个版本（见"版本对比"）。
- `POST /versions/purge`：`{"keep": n}` 按保留范围清理老版本（见"保留清理"）。
- `GET /versions`：查看历史与当前版本（只含仍可用、未清理的版本）。

**发布语义**：先完整校验（行键必须是已声明列、值类型匹配）并构建不可变快照，再一次原子切换当前指针——发布中途失败不会留下半更新状态。重复发布同一版本号：内容指纹一致则幂等成功；不一致则 `version_conflict`，状态不变。

## 结构演进兼容规则

- **新增列**：向后兼容，针对旧结构的显式查询结果不变。
- **改名/删除列**：旧查询引用时 `unknown_field`，并列出当前可用字段——不会静默取到错误数据。
- **列类型变化**：类型与数据必须一起演进；类型与数据不一致的发布在发布时即被拒绝（`type_mismatch`）；旧类型常量的查询在新版本上同样 `type_mismatch`。
- **行缺少声明列**：按 null 处理；null 只匹配 `eq/ne`，其余比较为假。
- **行包含未声明列**：发布时拒绝（`query_syntax_error`），避免残留字段被静默忽略。
- **跨版本结果复用**：缓存键 = `(version_id, 查询规范化串)`，发布/回滚后绝不复用旧版本结果；回到旧版本时命中该版本自己的缓存。

## 并发一致性保证

- 查询在开始时取一次版本快照引用（pin），整个执行期间看到同一份不可变快照；并发的发布/回滚/版本切换不影响本次查询——当前版本查询与指定历史版本查询一视同仁。
- 版本指针的切换在锁内原子完成；查询只会看到某个完整版本，绝不会看到半更新状态。
- 并发重复发布：同内容全部幂等成功；不同内容恰好一个成功，其余 `version_conflict`。
- 保留清理与查询并发：被 pin 的版本进入 `skipped_in_use` 跳过本轮清理；清理本身在单锁内原子完成，并发发布/清理下版本清单始终等于实际可查版本集合（有专门的并发审计用例）。

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q -s     # 运行全部测试（-s 查看每条用例的查询输入/命中版本/判定依据日志）
uv run python main.py   # 启动本地服务（127.0.0.1，随机端口）
```

手工验证一条典型链路：

```bash
# 发布两个版本
curl -s -XPOST localhost:8000/versions/publish -d '{"version_id":"v1","tables":{"orders":{"columns":[{"name":"id","type":"int"}],"rows":[{"id":1}]}}}' -H 'Content-Type: application/json'
curl -s -XPOST localhost:8000/versions/publish -d '{"version_id":"v2","tables":{"orders":{"columns":[{"name":"id","type":"int"},{"name":"v","type":"str"}],"rows":[{"id":1,"v":"a"},{"id":2,"v":"b"}]}}}' -H 'Content-Type: application/json'
# 指定历史版本查询
curl -s -XPOST localhost:8000/query -d '{"table":"orders","version_id":"v1"}' -H 'Content-Type: application/json'
# 版本对比（按 id 识别行）
curl -s -XPOST localhost:8000/versions/diff -d '{"old":"v1","new":"v2","match_keys":{"orders":["id"]}}' -H 'Content-Type: application/json'
# 只保留最近 1 个版本，再查 v1 应得到 version_unavailable
curl -s -XPOST localhost:8000/versions/purge -d '{"keep":1}' -H 'Content-Type: application/json'
curl -s -XPOST localhost:8000/query -d '{"table":"orders","version_id":"v1"}' -H 'Content-Type: application/json'
```

测试覆盖：非法查询分类（`test_query_validation.py`）、查询语义正确性（`test_engine.py`）、发布/回滚/缓存隔离（`test_versioning.py`）、并发发布与查询及快照隔离（`test_concurrency.py`）、结构演进兼容（`test_schema_evolution.py`）、HTTP 接口（`test_api.py`），以及新增的历史版本查询与查询期间版本切换（`test_historical_query.py`）、版本对比（`test_diff.py`）、保留清理含清理/查询/发布并发（`test_retention.py`）、新能力的 HTTP 接口（`test_api_extensions.py`）。
