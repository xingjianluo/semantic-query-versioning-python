"""HTTP 接口：历史版本查询、版本对比、保留清理、定向回退的冒烟测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import app

from .conftest import log_decision


def _c() -> TestClient:
    return TestClient(app)


def test_historical_query_and_distinct_errors_http():
    c = _c()
    base = "hist-x"
    for i, amount in enumerate((10, 20, 30), start=1):
        r = c.post("/versions/publish", json={
            "version_id": f"{base}-v{i}",
            "tables": {"t": {"columns": [
                {"name": "id", "type": "int"}, {"name": "amount", "type": "int"}],
                "rows": [{"id": 1, "amount": amount}]}},
        })
        assert r.status_code == 200

    r = c.post("/query", json={"table": "t", "version_id": f"{base}-v1"})
    assert r.status_code == 200
    body = r.json()
    assert body["version_id"] == f"{base}-v1" and body["rows"] == [{"id": 1, "amount": 10}]

    r = c.post("/query", json={"table": "t", "version_id": f"{base}-missing"})
    assert r.status_code == 400 and r.json()["error"] == "version_not_found"

    r = c.post("/versions/purge", json={"keep": 1})
    assert r.status_code == 200 and f"{base}-v3" not in r.json()["purged"]

    r = c.post("/query", json={"table": "t", "version_id": f"{base}-v1"})
    assert r.status_code == 400 and r.json()["error"] == "version_unavailable"
    log_decision("api_historical", "query @ explicit / missing / purged", f"{base}-v3",
                 "历史版本查询成功；未发布与已清理返回不同错误码，未用最新顶替")


def test_diff_and_rollback_http():
    c = _c()
    base = "diff-x"
    c.post("/versions/publish", json={
        "version_id": f"{base}-v1",
        "tables": {"t": {"columns": [{"name": "id", "type": "int"}, {"name": "v", "type": "str"}],
                         "rows": [{"id": 1, "v": "a"}]}},
    })
    c.post("/versions/publish", json={
        "version_id": f"{base}-v2",
        "tables": {"t": {"columns": [{"name": "id", "type": "int"}, {"name": "v", "type": "str"}],
                         "rows": [{"id": 1, "v": "b"}, {"id": 2, "v": "c"}]}},
    })
    r = c.post("/versions/diff", json={
        "old": f"{base}-v1", "new": f"{base}-v2", "match_keys": {"t": ["id"]}})
    assert r.status_code == 200
    body = r.json()
    assert body["rows"][0]["added"] == 1 and body["rows"][0]["updated"] == 1

    r = c.post("/versions/diff", json={"old": f"{base}-v1", "new": "nope"})
    assert r.status_code == 400 and r.json()["error"] == "version_not_found"

    r = c.post("/versions/rollback", json={"version_id": f"{base}-v1"})
    assert r.status_code == 200 and r.json()["current_version"] == f"{base}-v1"

    # 再发布 v3，使 v2 既不是当前版本、也落在 keep=1 窗口之外
    c.post("/versions/publish", json={
        "version_id": f"{base}-v3",
        "tables": {"t": {"columns": [{"name": "id", "type": "int"}, {"name": "v", "type": "str"}],
                         "rows": [{"id": 9, "v": "z"}]}},
    })
    r = c.post("/versions/purge", json={"keep": 1})
    assert r.status_code == 200 and f"{base}-v2" in r.json()["purged"]
    r = c.post("/versions/rollback", json={"version_id": f"{base}-v2"})
    assert r.status_code == 400 and r.json()["error"] == "version_unavailable"
    log_decision("api_diff_rollback", "diff + rollback_to + rollback purged", f"{base}-v1",
                 "对比给出增改行；定向回退可用；回退到已清理版本明确 unavailable")
