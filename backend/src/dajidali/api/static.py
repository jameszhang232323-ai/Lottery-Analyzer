"""静态资源挂载：本机单进程模式下由 API 直接提供八页界面。

服务器模式由 nginx 同源提供静态资源，不需要挂载；只有本机模式会设置
``DAJIDALI_STATIC_DIR`` 指向 ``web/dist``。未设置或目录不存在时不做任何挂载，
API 仍可单独运行（例如容器里只跑 worker）。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.staticfiles import StaticFiles

STATIC_DIR_ENV = "DAJIDALI_STATIC_DIR"


def resolve_static_dir(value: str | None = None) -> Path | None:
    """解析静态目录；未配置或缺少 index.html 时返回 None。"""
    raw = value if value is not None else os.environ.get(STATIC_DIR_ENV)
    if not raw:
        return None
    root = Path(raw).expanduser()
    if not root.is_dir() or not (root / "index.html").is_file():
        return None
    return root.resolve()


def mount_spa(app: FastAPI, directory: str | Path) -> bool:
    """把构建产物挂到根路径，未命中的前端路由回退到 index.html。

    返回是否挂载成功。``/api`` 前缀永不回退，避免把 API 404 变成 HTML。
    """
    root = Path(directory).resolve()
    index = root / "index.html"
    if not index.is_file():
        return False

    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> Response:
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if full_path:
            candidate = (root / full_path).resolve()
            # 只允许返回静态目录内部的文件，防止 ../ 逃逸。
            if root in candidate.parents and candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(index)

    return True


def mount_static_from_env(app: FastAPI) -> bool:
    """按环境变量挂载；供 ``create_app()`` 调用。"""
    root = resolve_static_dir()
    if root is None:
        return False
    return mount_spa(app, root)
