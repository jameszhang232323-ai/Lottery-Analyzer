"""API 契约测试：健康探针、彩种清单与统一错误结构。

对应实施计划任务 01 的验收项：非法请求返回 422 且带 request_id。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dajidali.api.app import REQUEST_ID_HEADER, create_app
from dajidali.domain.errors import InvalidNumbersError


def _probe_app() -> FastAPI:
    """在真实应用上追加仅测试使用的探针路由。"""
    app = create_app()

    @app.get("/api/v1/_probe/echo")
    def echo(limit: int) -> dict[str, int]:
        return {"limit": limit}

    @app.get("/api/v1/_probe/domain-error")
    def domain_error() -> None:
        raise InvalidNumbersError(
            "红球不得重复",
            code="DUPLICATE_RED_BALLS",
            reds=[1, 1, 3, 4, 5, 6],
        )

    return app


def test_health_live() -> None:
    client = TestClient(create_app())
    response = client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers[REQUEST_ID_HEADER]


def test_health_ready() -> None:
    client = TestClient(create_app())
    body = client.get("/api/v1/health/ready").json()
    assert body["status"] == "ready"
    assert body["checks"]["api"] == "ok"


def test_request_id_is_echoed() -> None:
    client = TestClient(create_app())
    response = client.get("/api/v1/games", headers={REQUEST_ID_HEADER: "req-test-001"})
    assert response.headers[REQUEST_ID_HEADER] == "req-test-001"


def test_games_payload() -> None:
    client = TestClient(create_app())
    items = client.get("/api/v1/games").json()["items"]
    codes = {item["code"]: item for item in items}
    assert codes["ssq"]["enabled"] is True
    assert codes["ssq"]["red"] == {"count": 6, "min": 1, "max": 33}
    assert codes["fc3d"]["enabled"] is False


def test_validation_error_uses_unified_shape() -> None:
    client = TestClient(_probe_app())
    response = client.get("/api/v1/_probe/echo", params={"limit": "abc"})
    assert response.status_code == 422

    body = response.json()
    assert set(body) == {"code", "message", "details", "request_id"}
    assert body["code"] == "VALIDATION_ERROR"
    assert body["request_id"] == response.headers[REQUEST_ID_HEADER]
    assert body["details"]["errors"]


def test_domain_error_is_mapped() -> None:
    client = TestClient(_probe_app())
    response = client.get("/api/v1/_probe/domain-error")
    assert response.status_code == 422

    body = response.json()
    assert body["code"] == "DUPLICATE_RED_BALLS"
    assert body["details"] == {"reds": [1, 1, 3, 4, 5, 6]}


def test_openapi_is_served_under_prefix() -> None:
    client = TestClient(create_app())
    schema = client.get("/api/v1/openapi.json").json()
    assert schema["info"]["title"] == "大吉大利 API"
    assert "/api/v1/health/live" in schema["paths"]
