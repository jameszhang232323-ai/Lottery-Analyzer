"""静态资源挂载测试：本机单进程模式提供八页界面，但不吞掉 API 404。

测试目录放在 ``backend/.test-tmp``：本机沙箱禁止 pytest 默认的
系统临时目录（0700 临时目录无法写入），与集成测试保持同一约定。
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dajidali.api.static import mount_spa, mount_static_from_env, resolve_static_dir

TEST_TMP = Path(__file__).resolve().parents[2] / ".test-tmp"


@pytest.fixture()
def workdir() -> Iterator[Path]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    root = TEST_TMP / f"static-{uuid.uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def build_site(root: Path) -> Path:
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<!doctype html><title>大吉大利</title>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log('ok')", encoding="utf-8")
    return root


def test_resolve_returns_none_without_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAJIDALI_STATIC_DIR", raising=False)
    assert resolve_static_dir() is None


def test_resolve_rejects_directory_without_index(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = workdir / "empty"
    empty.mkdir()
    monkeypatch.setenv("DAJIDALI_STATIC_DIR", str(empty))
    assert resolve_static_dir() is None


def test_spa_fallback_and_assets(workdir: Path) -> None:
    site = build_site(workdir / "dist")
    app = FastAPI()

    @app.get("/api/v1/health/live")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    assert mount_spa(app, site) is True
    client = TestClient(app)

    index = client.get("/")
    assert index.status_code == 200
    assert "大吉大利" in index.text

    # 前端路由（history 模式）回退到 index.html
    deep = client.get("/draws")
    assert deep.status_code == 200
    assert "大吉大利" in deep.text

    asset = client.get("/assets/app.js")
    assert asset.status_code == 200
    assert "console.log" in asset.text

    # API 未命中不能被回退成 HTML
    missing = client.get("/api/v1/nope")
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/json")

    # API 正常路由不受影响
    assert client.get("/api/v1/health/live").json() == {"status": "ok"}


def test_spa_never_serves_files_outside_root(workdir: Path) -> None:
    site = build_site(workdir / "dist")
    secret = workdir / "secret.txt"
    secret.write_text("应该读不到", encoding="utf-8")
    app = FastAPI()
    assert mount_spa(app, site) is True

    response = TestClient(app).get("/%2e%2e/secret.txt")
    assert response.status_code == 200
    assert "应该读不到" not in response.text


def test_env_mount_is_optional(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site = build_site(workdir / "dist")
    monkeypatch.setenv("DAJIDALI_STATIC_DIR", str(site))
    assert mount_static_from_env(FastAPI()) is True

    monkeypatch.delenv("DAJIDALI_STATIC_DIR")
    assert mount_static_from_env(FastAPI()) is False
