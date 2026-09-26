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
    assert r.status_code == 200 and "history" in r.json()
