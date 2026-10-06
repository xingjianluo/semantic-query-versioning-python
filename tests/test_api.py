"""HTTP 接口冒烟测试：发布、查询、错误码、回滚。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import app

from .conftest import log_decision


def _client() -> TestClient:
    return TestClient(app)


def test_publish_query_rollback_flow():
    c = _client()
    r = c.post("/versions/publish", json={
        "version_id": "api-v1",
        "tables": {"t": {"columns": [{"name": "id", "type": "int"}], "rows": [{"id": 1}]}},
    })
    assert r.status_code == 200 and r.json()["current_version"] == "api-v1"

    r = c.post("/query", json={"table": "t"})
    body = r.json()
    assert r.status_code == 200 and body["rows"] == [{"id": 1}] and body["version_id"] == "api-v1"

    r = c.post("/query", json={"table": "t", "select": ["nope"]})
    assert r.status_code == 400 and r.json()["error"] == "unknown_field"

    r = c.post("/versions/rollback")
    assert r.status_code == 400 and r.json()["error"] == "no_active_version"  # 唯一版本被弹出
    log_decision("api_flow", "publish/query/bad-query/rollback", "api-v1",
                 "HTTP 层错误以可区分 code 返回")


def test_versions_endpoint():
    c = _client()
    r = c.get("/versions")
    assert r.status_code == 200 and "history" in r.json() and "retired" in r.json()


def test_history_query_diff_prune_flow():
    """HTTP 层：指定历史版本查询、版本对比、保留清理、回滚到指定版本。"""
    c = _client()
    cols = [{"name": "id", "type": "int"}]
    for vid, row_id in (("api-h1", 1), ("api-h2", 2), ("api-h3", 3)):
        r = c.post("/versions/publish", json={
            "version_id": vid,
            "tables": {"ht": {"columns": cols, "rows": [{"id": row_id}]}},
        })
        assert r.status_code == 200

    # 指定历史版本查询
    r = c.post("/query", json={"table": "ht", "version": "api-h1"})
    assert r.status_code == 200 and r.json()["rows"] == [{"id": 1}]
    # 未发布过的版本
    r = c.post("/query", json={"table": "ht", "version": "nope"})
    assert r.status_code == 400 and r.json()["error"] == "version_not_found"

    # 版本对比
    r = c.post("/versions/diff", json={"from": "api-h1", "to": "api-h3"})
    assert r.status_code == 200
    (t,) = r.json()["tables"]
    assert t["rows"] == {"added": 1, "removed": 1, "modified": 0}
    r = c.post("/versions/diff", json={"from": "api-h1", "to": "nope"})
    assert r.status_code == 400 and r.json()["error"] == "version_not_found"

    # 保留清理：只留当前版本
    r = c.post("/versions/prune", json={"keep_last": 1})
    assert r.status_code == 200 and r.json()["current_version"] == "api-h3"
    assert set(r.json()["pruned"]) >= {"api-h1", "api-h2"}

    # 已清理版本：查询与回滚都报 version_retired
    r = c.post("/query", json={"table": "ht", "version": "api-h1"})
    assert r.status_code == 400 and r.json()["error"] == "version_retired"
    r = c.post("/versions/rollback", json={"version_id": "api-h1"})
    assert r.status_code == 400 and r.json()["error"] == "version_retired"

    r = c.get("/versions")
    assert "api-h1" in r.json()["retired"]
    log_decision("api_history_diff_prune", "query+version / diff / prune / rollback_to",
                 "api-h3", "HTTP 层新能力端到端可用，错误 code 可区分")
