"""FastAPI 应用装配。

本模块只做装配：中间件、异常映射、健康探针与路由注册。
业务逻辑属于 ``services`` 与 ``engine``，不写在这里。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from sqlalchemy.engine import Engine

from dajidali import __version__
from dajidali.api.deps import install_session_factory
from dajidali.api.errors import ErrorBody, error_response
from dajidali.api.routes import (
    admin_router,
    auth_router,
    resource_router,
)
from dajidali.api.routes import (
    ai_jobs as ai_jobs_routes,
)
from dajidali.api.routes import (
    analysis_jobs as analysis_jobs_routes,
)
from dajidali.api.routes import (
    artifacts as artifacts_routes,
)
from dajidali.api.routes import (
    backtest_jobs as backtest_jobs_routes,
)
from dajidali.api.routes import (
    backups as backups_routes,
)
from dajidali.api.routes import (
    candidate_jobs as candidate_jobs_routes,
)
from dajidali.api.routes import (
    draws as draws_routes,
)
from dajidali.api.routes import (
    snapshots as snapshots_routes,
)
from dajidali.api.routes import (
    sync_jobs as sync_jobs_routes,
)
from dajidali.api.routes.candidates import router as candidates_router
from dajidali.api.static import mount_static_from_env
from dajidali.domain.errors import DomainError
from dajidali.security.csrf import verify_csrf

API_PREFIX = "/api/v1"
REQUEST_ID_HEADER = "X-Request-ID"


def create_app(engine: Engine | None = None) -> FastAPI:
    """创建应用实例。测试与部署共用同一工厂，避免导入期副作用。

    ``engine`` 允许测试注入临时 SQLite 引擎；未注入时按 ``DATABASE_URL``
    创建，与迁移/worker 使用同一配置入口。
    """
    app = FastAPI(
        title="大吉大利 API",
        version=__version__,
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
        openapi_url=f"{API_PREFIX}/openapi.json",
        # 所有非安全方法统一做会话绑定 CSRF 校验；登录接口在未登录时不受影响。
        dependencies=[Depends(verify_csrf)],
    )
    install_session_factory(app, engine)

    @app.middleware("http")
    async def attach_request_id(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """为每个请求分配可追踪的 request_id，并回写到响应头。"""
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request.state.request_id = incoming or uuid.uuid4().hex
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request.state.request_id
        return response

    @app.exception_handler(DomainError)
    async def handle_domain_error(request: Request, exc: DomainError) -> Response:
        body = ErrorBody(
            code=exc.code,
            message=exc.message,
            details=exc.details,
            request_id=getattr(request.state, "request_id", ""),
        )
        return error_response(exc.http_status, body)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> Response:
        body = ErrorBody(
            code="VALIDATION_ERROR",
            message="请求参数不合法",
            details={"errors": jsonable_encoder(exc.errors())},
            request_id=getattr(request.state, "request_id", ""),
        )
        return error_response(422, body)

    @app.get(f"{API_PREFIX}/health/live", tags=["health"])
    def health_live() -> dict[str, str]:
        """存活探针：进程可响应即通过，不暴露依赖细节。"""
        return {"status": "ok"}

    @app.get(f"{API_PREFIX}/health/ready", tags=["health"])
    def health_ready() -> dict[str, Any]:
        """就绪探针：首版未接数据库时只报告 API 自身状态。"""
        return {"status": "ready", "checks": {"api": "ok"}}

    @app.get(f"{API_PREFIX}/games", tags=["games"])
    def list_games() -> dict[str, Any]:
        """彩种清单。首版仅启用双色球，福彩 3D 预留。"""
        return {
            "items": [
                {
                    "code": "ssq",
                    "name": "双色球",
                    "enabled": True,
                    "red": {"count": 6, "min": 1, "max": 33},
                    "blue": {"count": 1, "min": 1, "max": 16},
                },
                {
                    "code": "fc3d",
                    "name": "福彩3D",
                    "enabled": False,
                    "note": "类型与接口预留，产品 V1.1 启用",
                },
            ]
        }

    app.include_router(auth_router, prefix=API_PREFIX)
    app.include_router(admin_router, prefix=API_PREFIX)
    app.include_router(resource_router, prefix=API_PREFIX)
    app.include_router(candidates_router, prefix=API_PREFIX)

    # 任务 04/05/06/08/09/10 的路由由各自模块提供 register()；
    # 重复调用会跳过，便于测试与装配复用。
    draws_routes.register(app, prefix=API_PREFIX)
    snapshots_routes.register(app, prefix=API_PREFIX)
    sync_jobs_routes.register(app, prefix=API_PREFIX)
    analysis_jobs_routes.register(app, prefix=API_PREFIX)
    candidate_jobs_routes.register(app, prefix=API_PREFIX)
    backtest_jobs_routes.register(app, prefix=API_PREFIX)
    ai_jobs_routes.register(app, prefix=API_PREFIX)
    artifacts_routes.register(app)
    backups_routes.register(app)

    # 本机单进程模式：设置了 DAJIDALI_STATIC_DIR 且存在 web/dist 时，
    # 由 API 直接提供八页界面（服务器模式交给 nginx）。必须放在所有 API 路由之后。
    mount_static_from_env(app)

    return app


app = create_app()
