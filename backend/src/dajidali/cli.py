"""命令行入口：迁移、API、worker、scheduler、管理员初始化与健康检查。

统一约定（设计文档 6.4、12.2、12.3）：

- 所有命令通过 ``--data-dir`` 覆盖 ``DAJIDALI_DATA_DIR``，默认 ``./data``；
- 数据目录和迁移目录在输出中一律使用绝对路径；
- 未显式设置 ``DATABASE_URL`` 时，SQLite 文件放在数据目录下；
- API/worker/scheduler 各自独立进程；API 进程启动时绝不自动迁移；
- 本模块不导入 FastAPI 应用（``serve-api`` 需要时才由 uvicorn 导入），
  以保证迁移、管理员初始化等命令不产生 Web 框架副作用。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from dajidali.data.database import (
    create_engine_from_url,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import User
from dajidali.domain.errors import DomainError
from dajidali.jobs.scheduler import DEFAULT_SCHEDULER_LEASE_SECONDS, run_scheduler_forever
from dajidali.jobs.worker import JobHandler, JobWorker, registered_handlers
from dajidali.security.bootstrap import create_initial_admin, normalize_username

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_DATA_DIR = "data"
DEFAULT_SCHEDULE_ID = "dajidali-scheduler"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8000/api/v1/health/ready"

DATA_DIR_ENV = "DAJIDALI_DATA_DIR"
DATABASE_URL_ENV = "DATABASE_URL"
MIGRATIONS_DIR_ENV = "DAJIDALI_MIGRATIONS_DIR"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class CliError(RuntimeError):
    """可直接展示给使用者的命令行错误；消息不包含密码或密钥。"""


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """一次命令解析出的绝对数据目录与规范化数据库 URL。"""

    data_dir: Path
    database_url: str


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _mutable_env(environ: MutableMapping[str, str] | None) -> MutableMapping[str, str]:
    return os.environ if environ is None else environ


def resolve_data_dir(
    raw: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Path:
    """把 ``--data-dir``/环境变量/默认值统一解析为绝对目录。

    相对路径以当前工作目录为基准；符号链接与 ``..`` 会被规范化。这里只做
    解析和只读校验，不创建目录（创建由 :func:`ensure_data_dir` 负责）。
    """
    env = _env(environ)
    if raw is not None:
        value = raw
    elif DATA_DIR_ENV in env:
        value = env[DATA_DIR_ENV]
    else:
        value = DEFAULT_DATA_DIR
    if not isinstance(value, str) or not value.strip():
        raise CliError(f"数据目录不能为空（--data-dir 或 {DATA_DIR_ENV}）")

    try:
        candidate = Path(value).expanduser()
    except (OSError, ValueError) as exc:
        raise CliError(f"数据目录不合法：{exc}") from None
    base = (cwd or Path.cwd()).resolve()
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CliError(f"无法解析数据目录：{exc}") from None
    if resolved.parent == resolved:
        raise CliError("数据目录不能是文件系统根目录")
    if resolved.exists() and not resolved.is_dir():
        raise CliError(f"数据目录已存在但不是目录：{resolved}")
    return resolved


def ensure_data_dir(data_dir: Path) -> Path:
    """创建数据目录；失败时给出中文路径与原因。"""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CliError(f"无法创建数据目录 {data_dir}：{exc.strerror or exc}") from None
    if not data_dir.is_dir():
        raise CliError(f"数据目录不是目录：{data_dir}")
    return data_dir


def resolve_data_path(data_dir: Path, *parts: str) -> Path:
    """在数据目录内安全拼路径，禁止 ``..`` 或绝对路径逃逸。"""
    try:
        base = data_dir.resolve(strict=False)
        candidate = base.joinpath(*parts).resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CliError(f"数据路径不合法：{exc}") from None
    try:
        candidate.relative_to(base)
    except ValueError:
        raise CliError(f"数据路径越界：{candidate} 不在数据目录 {base} 内") from None
    return candidate


def normalize_database_url(raw: str, *, cwd: Path | None = None) -> str:
    """把 SQLite URL 中的相对文件路径绝对化；其他数据库 URL 原样返回。"""
    value = raw.strip()
    if not value:
        raise CliError(f"{DATABASE_URL_ENV} 不能为空")
    try:
        parsed = make_url(value)
    except Exception:  # SQLAlchemy 对畸形 URL 抛 ArgumentError；不回显可能含密码的原值
        raise CliError(f"{DATABASE_URL_ENV} 不合法：无法解析为 SQLAlchemy URL") from None
    if parsed.get_backend_name() != "sqlite":
        return value
    database = parsed.database
    if not database or database == ":memory:" or database.startswith("file:"):
        return value
    try:
        path = Path(database).expanduser()
    except (OSError, ValueError) as exc:
        raise CliError(f"{DATABASE_URL_ENV} 的 SQLite 路径不合法：{exc}") from None
    if not path.is_absolute():
        path = (cwd or Path.cwd()) / path
    try:
        path = path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CliError(f"无法解析 SQLite 路径：{exc}") from None
    # 保留查询参数（如 mode=ro）时避免手工拼接丢失；常规本地路径用统一构造器。
    if parsed.query:
        return parsed.set(database=str(path)).render_as_string(hide_password=False)
    return make_sqlite_url(path)


def database_url_from_env(
    data_dir: Path,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> str:
    """优先读取 ``DATABASE_URL``，否则使用数据目录下的 SQLite 文件。"""
    raw = _env(environ).get(DATABASE_URL_ENV)
    if raw:
        return normalize_database_url(raw, cwd=cwd)
    return make_sqlite_url(resolve_data_path(data_dir, "dajidali.sqlite3"))


def display_database_url(database_url: str) -> str:
    """用于终端展示的数据库 URL：SQLite 绝对路径，其他后端隐藏密码。"""
    try:
        parsed = make_url(database_url)
    except Exception:
        return "<无法解析的 DATABASE_URL>"
    if parsed.get_backend_name() == "sqlite" and parsed.database:
        if parsed.database == ":memory:":
            return "sqlite:///:memory:"
        return f"sqlite:///{Path(parsed.database).as_posix()}"
    return parsed.render_as_string(hide_password=True)


def deployment_mode(database_url: str, host: str | None = None) -> str:
    """按数据库后端与监听地址给出部署模式说明。"""
    try:
        backend = make_url(database_url).get_backend_name()
    except Exception:
        backend = "unknown"
    loopback = host is None or host in LOOPBACK_HOSTS
    if backend == "sqlite":
        return "本机模式（SQLite）" if loopback else "服务器模式（SQLite 非 loopback 监听）"
    return f"服务器模式（{backend}）"


def _prepare_context(
    args: argparse.Namespace,
    *,
    environ: MutableMapping[str, str] | None = None,
    cwd: Path | None = None,
) -> RuntimeContext:
    env = _mutable_env(environ)
    data_dir = ensure_data_dir(resolve_data_dir(getattr(args, "data_dir", None), environ=env, cwd=cwd))
    database_url = database_url_from_env(data_dir, environ=env, cwd=cwd)
    # worker/scheduler/未来的附件服务都从同一环境变量读取数据目录；统一写回绝对路径。
    env[DATA_DIR_ENV] = str(data_dir)
    env[DATABASE_URL_ENV] = database_url
    return RuntimeContext(data_dir=data_dir, database_url=database_url)


def _print_context(label: str, context: RuntimeContext, *, host: str | None = None) -> None:
    print(f"== 大吉大利 {label} ==")
    print(f"部署模式：{deployment_mode(context.database_url, host)}")
    print(f"数据目录：{context.data_dir}")
    print(f"数据库：{display_database_url(context.database_url)}")


def _qualified_url(host: str, port: int) -> str:
    formatted_host = f"[{host}]" if ":" in host else host
    return f"http://{formatted_host}:{port}"


def resolve_migrations_dir(
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Path:
    """定位 Alembic 迁移脚本目录；包内相对路径优先，其次项目/当前目录。"""
    env = _env(environ)
    candidates: list[Path] = []
    configured = env.get(MIGRATIONS_DIR_ENV)
    if configured:
        candidates.append(Path(configured))
    package_dir = Path(__file__).resolve().parent
    candidates.extend(
        [
            package_dir / "migrations",
            Path(__file__).resolve().parents[2] / "migrations",
            (cwd or Path.cwd()) / "migrations",
        ]
    )
    checked: list[str] = []
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        checked.append(str(resolved))
        if (resolved / "env.py").is_file():
            return resolved
    raise CliError(
        "未找到 Alembic 迁移目录（需要包含 env.py）。已检查：" + "；".join(checked)
    )


def _alembic_config(
    script_dir: Path,
    database_url: str,
    *,
    config_factory: Callable[[], Any],
) -> Any:
    config = config_factory()
    config.set_main_option("script_location", str(script_dir))
    config.set_main_option("sqlalchemy.url", database_url)
    src_dir = Path(__file__).resolve().parents[1]
    if src_dir.is_dir():
        config.set_main_option("prepend_sys_path", str(src_dir))
    return config


def cmd_init_db(
    args: argparse.Namespace,
    *,
    environ: MutableMapping[str, str] | None = None,
    cwd: Path | None = None,
    alembic_command: Any = alembic_command,
    config_factory: Callable[[], Any] = Config,
    migration_script_dir: Path | None = None,
) -> int:
    """执行一次 ``alembic upgrade head``；重复执行是幂等的空操作。"""
    context = _prepare_context(args, environ=environ, cwd=cwd)
    script_dir = (
        migration_script_dir.resolve()
        if migration_script_dir is not None
        else resolve_migrations_dir(environ=environ, cwd=cwd)
    )
    if not script_dir.is_dir():
        raise CliError(f"迁移目录不存在：{script_dir}")
    _print_context("数据库迁移", context)
    print(f"迁移目录：{script_dir}")
    config = _alembic_config(script_dir, context.database_url, config_factory=config_factory)
    try:
        alembic_command.upgrade(config, "head")
    except Exception as exc:  # Alembic/SQLAlchemy 的失败统一转为可读错误
        raise CliError(f"数据库迁移失败：{type(exc).__name__}: {exc}") from None
    print("数据库迁移完成（已是最新版本时为空操作）。")
    return 0


def cmd_serve_api(
    args: argparse.Namespace,
    *,
    environ: MutableMapping[str, str] | None = None,
    cwd: Path | None = None,
    uvicorn_run: Callable[..., Any] | None = None,
) -> int:
    """启动 uvicorn；默认只监听 127.0.0.1:8000。"""
    context = _prepare_context(args, environ=environ, cwd=cwd)
    _print_context("API", context, host=args.host)
    print(f"监听地址：{_qualified_url(args.host, args.port)}")
    # 本机静态资源由 dajidali.api.static 按 DAJIDALI_STATIC_DIR 统一挂载；
    # CLI 只负责启动标准 ASGI 应用字符串，不在此处 import FastAPI。
    if uvicorn_run is None:
        import uvicorn

        uvicorn_run = uvicorn.run
    uvicorn_run(
        "dajidali.api.app:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


def _register_builtin_handlers(
    factory: sessionmaker[Session],
    environ: Mapping[str, str] | None,
) -> list[str]:
    """把内置业务处理器注册到当前 worker 进程，返回已注册的 kind 列表。

    - ``analysis.run`` / ``candidate.generate`` / ``backtest.run``：总是注册；
    - ``ai.analyze``：AI 凭证齐全时注册（环境变量优先，数据库密文回退），
      否则相关任务明确失败为 ``AI_NOT_CONFIGURED``；
    - ``sync.run``：来源已配置时注册（当前仅 ``DAJIDALI_SOURCE=recorded``）。
    """
    registered: list[str] = []

    from dajidali.services.analysis import register_handlers as register_analysis
    from dajidali.services.backtests import register_handlers as register_backtests
    from dajidali.services.candidate_jobs import register_handlers as register_candidates

    register_analysis(factory)
    register_candidates(factory)
    register_backtests(factory)
    registered.extend(["analysis.run", "candidate.generate", "backtest.run"])

    from dajidali.services.ai.runtime import provider_from_env

    provider = provider_from_env(environ)
    provider_source = "环境变量"

    # 环境变量未配置时，尝试从数据库读取加密保存的 AI 凭证
    if provider is None:
        from dajidali.services.ai.provider_store import provider_from_db

        with factory() as session:
            provider = provider_from_db(session)
        if provider is not None:
            provider_source = "数据库"

    if provider is not None:
        from dajidali.api.routes.ai_jobs import register_ai_handler

        register_ai_handler(factory, provider)
        registered.append(f"ai.analyze（来源={provider_source}）")

    from dajidali.services.sync import register_handlers as register_sync
    from dajidali.services.sync import source_from_env

    source = source_from_env(environ)
    if source is not None:
        register_sync(factory, source)
        name = getattr(source, "name", source.__class__.__name__)
        registered.append(f"sync.run（来源={name}）")

    return registered


def cmd_serve_worker(
    args: argparse.Namespace,
    *,
    environ: MutableMapping[str, str] | None = None,
    cwd: Path | None = None,
    worker: Any | None = None,
    job_handlers: Mapping[str, JobHandler] | None = None,
) -> int:
    """启动单 worker；``--once`` 只跑一轮后退出。"""
    context = _prepare_context(args, environ=environ, cwd=cwd)
    _print_context("worker", context)
    engine = None
    if worker is None:
        engine = create_engine_from_url(context.database_url)
        factory = create_session_factory(engine)
        registered_kinds = _register_builtin_handlers(factory, environ)
        if registered_kinds:
            print("已注册内置处理器：" + "、".join(registered_kinds))
        handlers = dict(job_handlers or {})
        if not handlers and not registered_handlers():
            print("注意：当前进程尚未注册业务任务处理器；未知任务类型会被标记失败。")
        worker = JobWorker(factory, handlers)
    try:
        iterations = worker.run_forever(
            poll_interval=args.poll_interval,
            max_iterations=1 if args.once else None,
        )
    finally:
        if engine is not None:
            engine.dispose()
    if args.once:
        print(f"worker 已执行一轮（迭代 {iterations} 次）。")
    else:
        print(f"worker 已退出（迭代 {iterations} 次）。")
    return 0


def cmd_serve_scheduler(
    args: argparse.Namespace,
    *,
    environ: MutableMapping[str, str] | None = None,
    cwd: Path | None = None,
    session_factory: sessionmaker[Session] | None = None,
    scheduler_runner: Callable[..., int] = run_scheduler_forever,
    tick: Callable[..., Any] | None = None,
) -> int:
    """启动调度器租约循环；``--once`` 只跑一个调度周期。"""
    context = _prepare_context(args, environ=environ, cwd=cwd)
    _print_context("scheduler", context)
    engine = None
    if session_factory is None:
        engine = create_engine_from_url(context.database_url)
        session_factory = create_session_factory(engine)
    if tick is None:
        print("调度项：当前版本尚未注册业务调度 tick（仅维护调度租约）。")
    try:
        iterations = scheduler_runner(
            session_factory,
            schedule_id=args.schedule_id,
            interval=args.interval,
            lease_seconds=args.lease_seconds,
            max_iterations=1 if args.once else None,
            tick=tick,
        )
    finally:
        if engine is not None:
            engine.dispose()
    if args.once:
        print(f"scheduler 已执行一个周期（迭代 {iterations} 次）。")
    else:
        print(f"scheduler 已退出（迭代 {iterations} 次）。")
    return 0


def cmd_create_admin(
    args: argparse.Namespace,
    *,
    environ: MutableMapping[str, str] | None = None,
    cwd: Path | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> int:
    """调用安全模块创建首个管理员；重名时明确报错而不是静默覆盖。"""
    context = _prepare_context(args, environ=environ, cwd=cwd)
    _print_context("初始化管理员", context)
    engine = None
    if session_factory is None:
        engine = create_engine_from_url(context.database_url)
        session_factory = create_session_factory(engine)
    user_id: int | None = None
    username = ""
    try:
        with session_factory() as session:
            username = normalize_username(args.username)
            existing = session.scalar(select(User).where(User.username == username))
            if existing is not None:
                raise CliError(
                    f"用户名“{username}”已存在（角色：{existing.role}），未做任何修改。"
                    "本版本不提供静默重置密码，请使用其他管理员账号或后续管理命令。"
                )
            user = create_initial_admin(
                session,
                username,
                args.password,
                display_name=args.display_name,
            )
            session.commit()
            user_id = user.id
    except OperationalError:
        raise CliError(
            "无法访问数据库表。请先运行 init-db 完成迁移，例如："
            f"python -m dajidali init-db --data-dir {context.data_dir}"
        ) from None
    finally:
        if engine is not None:
            engine.dispose()
    print(f"管理员已创建：{username}（id={user_id}）。")
    print("请勿在终端历史或截图中保留明文密码。")
    return 0


def validate_health_url(url: str) -> str:
    """健康检查只允许 http/https，且 URL 中不能携带凭证。"""
    if not isinstance(url, str) or not url.strip():
        raise CliError("健康检查 URL 不能为空")
    value = url.strip()
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise CliError(f"健康检查 URL 不合法：{exc}") from None
    if parsed.scheme not in {"http", "https"}:
        raise CliError("健康检查 URL 只支持 http/https")
    if not parsed.hostname:
        raise CliError("健康检查 URL 缺少主机名")
    if parsed.username or parsed.password:
        raise CliError("健康检查 URL 不应包含用户名或密码")
    return value


def probe_health(
    url: str,
    *,
    http_get: Callable[..., Any] | None = None,
    request_timeout: float = 2.0,
) -> tuple[bool, str]:
    """请求一次健康探针，返回 ``(是否通过, 中文说明)``；不抛网络异常。"""
    getter = http_get or httpx.get
    try:
        # 健康探针通常访问 loopback；不让环境代理把本机请求转发出去。
        response = getter(
            url,
            timeout=request_timeout,
            follow_redirects=False,
            trust_env=False,
        )
    except Exception as exc:  # CLI 不打印可能含内部地址的完整堆栈
        return False, f"请求失败（{type(exc).__name__}）"
    status_code = getattr(response, "status_code", 0)
    if not 200 <= status_code < 300:
        return False, f"HTTP {status_code}"
    try:
        payload = response.json()
    except (ValueError, AttributeError):
        return False, "响应不是合法 JSON"
    if not isinstance(payload, dict):
        return False, "响应 JSON 不是对象"
    status = payload.get("status")
    if status not in {"ok", "ready"}:
        return False, f"status={status!r}"
    return True, f"HTTP {status_code}，status={status}"


def wait_for_health(
    url: str,
    *,
    timeout: float = 10.0,
    interval: float = 0.5,
    request_timeout: float = 2.0,
    http_get: Callable[..., Any] | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[bool, str, int]:
    """在 ``timeout`` 内轮询健康探针，返回 ``(是否通过, 说明, 尝试次数)``。"""
    if timeout < 0:
        raise CliError("健康检查超时时间不能为负数")
    if interval <= 0:
        raise CliError("健康检查轮询间隔必须为正数")
    deadline = clock() + timeout
    attempts = 0
    while True:
        ok, detail = probe_health(
            url,
            http_get=http_get,
            request_timeout=request_timeout,
        )
        attempts += 1
        if ok:
            return True, detail, attempts
        if clock() >= deadline:
            return False, detail, attempts
        sleep(interval)


def cmd_healthcheck(
    args: argparse.Namespace,
    *,
    http_get: Callable[..., Any] | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """轮询就绪探针；成功返回 0，超时返回 1。"""
    url = validate_health_url(args.url)
    ok, detail, attempts = wait_for_health(
        url,
        timeout=args.timeout,
        interval=args.interval,
        request_timeout=args.request_timeout,
        http_get=http_get,
        sleep=sleep,
        clock=clock,
    )
    if ok:
        print(f"健康检查通过：{url}（{detail}，尝试 {attempts} 次）")
        return 0
    print(
        f"健康检查失败：{url}（{detail}，尝试 {attempts} 次）",
        file=sys.stderr,
    )
    return 1


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("端口必须是整数") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须在 1—65535 之间")
    return port


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是数字") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def _non_negative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是数字") from None
    if number < 0:
        raise argparse.ArgumentTypeError("不能为负数")
    return number


def _add_data_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-dir",
        metavar="DIR",
        help=f"数据目录（默认读取 {DATA_DIR_ENV}，否则为 ./{DEFAULT_DATA_DIR}）",
    )


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器；供 ``main`` 与单元测试共用。"""
    parser = argparse.ArgumentParser(
        prog="dajidali",
        description="大吉大利 · 福彩数据分析工作台命令行",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="命令")

    init_db_parser = subparsers.add_parser(
        "init-db",
        help="执行一次数据库迁移（alembic upgrade head）",
    )
    _add_data_dir(init_db_parser)
    init_db_parser.set_defaults(handler=cmd_init_db)

    api_parser = subparsers.add_parser("serve-api", help="启动 FastAPI 服务")
    _add_data_dir(api_parser)
    api_parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址（默认 127.0.0.1）")
    api_parser.add_argument(
        "--port",
        type=_port,
        default=DEFAULT_PORT,
        help="监听端口（默认 8000）",
    )
    api_parser.add_argument(
        "--log-level",
        choices=("debug", "info", "warning", "error"),
        default="info",
        help="uvicorn 日志级别",
    )
    api_parser.set_defaults(handler=cmd_serve_api)

    worker_parser = subparsers.add_parser("serve-worker", help="启动任务 worker")
    _add_data_dir(worker_parser)
    worker_parser.add_argument("--once", action="store_true", help="只执行一轮后退出")
    worker_parser.add_argument(
        "--poll-interval",
        type=_positive_float,
        default=2.0,
        help="空闲轮询间隔秒数",
    )
    worker_parser.set_defaults(handler=cmd_serve_worker)

    scheduler_parser = subparsers.add_parser("serve-scheduler", help="启动调度器租约循环")
    _add_data_dir(scheduler_parser)
    scheduler_parser.add_argument("--once", action="store_true", help="只执行一个周期后退出")
    scheduler_parser.add_argument(
        "--interval",
        type=_positive_float,
        default=30.0,
        help="调度周期秒数",
    )
    scheduler_parser.add_argument(
        "--schedule-id",
        default=DEFAULT_SCHEDULE_ID,
        help="调度租约 ID",
    )
    scheduler_parser.add_argument(
        "--lease-seconds",
        type=int,
        default=DEFAULT_SCHEDULER_LEASE_SECONDS,
        help="调度租约秒数",
    )
    scheduler_parser.set_defaults(handler=cmd_serve_scheduler)

    admin_parser = subparsers.add_parser("create-admin", help="创建首个管理员账号")
    _add_data_dir(admin_parser)
    admin_parser.add_argument("--username", required=True, help="管理员用户名")
    admin_parser.add_argument("--password", required=True, help="管理员密码（至少 8 个字符）")
    admin_parser.add_argument("--display-name", default=None, help="显示名（默认与用户名相同）")
    admin_parser.set_defaults(handler=cmd_create_admin)

    health_parser = subparsers.add_parser("healthcheck", help="轮询健康探针")
    health_parser.add_argument("--url", default=DEFAULT_HEALTH_URL, help="健康探针 URL")
    health_parser.add_argument(
        "--timeout",
        type=_non_negative_float,
        default=10.0,
        help="总超时秒数（0 表示只尝试一次）",
    )
    health_parser.add_argument(
        "--interval",
        type=_positive_float,
        default=0.5,
        help="轮询间隔秒数",
    )
    health_parser.add_argument(
        "--request-timeout",
        type=_positive_float,
        default=2.0,
        help="单次请求超时秒数",
    )
    health_parser.set_defaults(handler=cmd_healthcheck)

    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数；测试与 ``main`` 共用同一解析器。"""
    return build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """命令行主入口；返回进程退出码，供 console_scripts 与 ``__main__`` 使用。"""
    parser = build_parser()
    args = parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    try:
        result = handler(args)
    except CliError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except DomainError as exc:
        print(f"错误：{exc}（错误码 {exc.code}）", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已停止。", file=sys.stderr)
        return 130
    return int(result)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
