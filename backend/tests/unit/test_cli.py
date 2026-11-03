"""CLI 单元测试：参数解析、路径校验、迁移调用与循环入口。

本文件不启动真实 HTTP 服务、不访问网络；数据库只使用临时 SQLite 文件或
注入的假会话工厂。
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from dajidali import cli
from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import SchedulerLease
from dajidali.jobs.scheduler import run_scheduler_forever

TEST_TMP = Path(__file__).resolve().parents[2] / ".test-tmp"


@pytest.fixture
def work_dir() -> Iterator[Path]:
    """在 backend/.test-tmp 下创建独立目录，避开系统临时目录的沙箱限制。"""
    path = TEST_TMP / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class FakeAlembicCommand:
    """记录 upgrade 调用，不连接数据库。"""

    def __init__(self) -> None:
        self.upgrade_calls: list[tuple[Any, str]] = []

    def upgrade(self, config: Any, revision: str) -> None:
        self.upgrade_calls.append((config, revision))


class FakeConfig:
    def __init__(self) -> None:
        self.options: dict[str, str] = {}

    def set_main_option(self, key: str, value: str) -> None:
        self.options[key] = value


class FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeWorker:
    def __init__(self, iterations: int = 1) -> None:
        self.calls: list[dict[str, Any]] = []
        self._iterations = iterations

    def run_forever(self, **kwargs: Any) -> int:
        self.calls.append(kwargs)
        return self._iterations


class FakeSchedulerRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def __call__(self, session_factory: Any, **kwargs: Any) -> int:
        self.calls.append((session_factory, kwargs))
        return 1


def test_serve_api_parses_host_and_port() -> None:
    args = cli.parse_args(["serve-api", "--port", "9123"])
    assert args.command == "serve-api"
    assert args.host == "127.0.0.1"
    assert args.port == 9123
    assert args.data_dir is None


def test_init_db_parses_data_dir() -> None:
    args = cli.parse_args(["init-db", "--data-dir", "custom-data"])
    assert args.command == "init-db"
    assert args.data_dir == "custom-data"


@pytest.mark.parametrize(
    "argv",
    [
        ["create-admin"],
        ["create-admin", "--username", "admin"],
        ["create-admin", "--password", "password123"],
    ],
)
def test_create_admin_requires_username_and_password(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.parse_args(argv)
    assert excinfo.value.code == 2
    error_text = capsys.readouterr().err
    assert "required" in error_text or "必须" in error_text


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_invalid_port_is_rejected(port: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.parse_args(["serve-api", "--port", port])
    assert excinfo.value.code == 2


def test_resolve_data_dir_absolutizes_relative_path(work_dir: Path) -> None:
    resolved = cli.resolve_data_dir("var/data", cwd=work_dir, environ={})
    assert resolved.is_absolute()
    assert resolved == (work_dir / "var" / "data").resolve()


def test_resolve_data_dir_prefers_cli_over_environment(work_dir: Path) -> None:
    resolved = cli.resolve_data_dir(
        str(work_dir / "cli-data"),
        cwd=work_dir,
        environ={cli.DATA_DIR_ENV: str(work_dir / "env-data")},
    )
    assert resolved == (work_dir / "cli-data").resolve()


def test_resolve_data_dir_uses_environment(work_dir: Path) -> None:
    resolved = cli.resolve_data_dir(
        None,
        cwd=work_dir,
        environ={cli.DATA_DIR_ENV: str(work_dir / "env-data")},
    )
    assert resolved == (work_dir / "env-data").resolve()


def test_resolve_data_dir_rejects_existing_file(work_dir: Path) -> None:
    target = work_dir / "not-a-dir"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(cli.CliError, match="不是目录"):
        cli.resolve_data_dir(str(target), cwd=work_dir, environ={})


def test_resolve_data_path_blocks_escape(work_dir: Path) -> None:
    data_dir = cli.resolve_data_dir(str(work_dir / "data"), cwd=work_dir, environ={})
    assert cli.resolve_data_path(data_dir, "run", "api.pid") == data_dir / "run" / "api.pid"
    with pytest.raises(cli.CliError, match="越界"):
        cli.resolve_data_path(data_dir, "..", "escape.txt")


def test_normalize_database_url_makes_relative_sqlite_absolute(work_dir: Path) -> None:
    url = cli.normalize_database_url("sqlite:///./local.sqlite3", cwd=work_dir)
    assert url == make_sqlite_url((work_dir / "local.sqlite3").resolve())


def test_init_db_runs_upgrade_head_once(work_dir: Path) -> None:
    data_dir = work_dir / "data"
    args = cli.parse_args(["init-db", "--data-dir", str(data_dir)])
    fake_command = FakeAlembicCommand()
    config = FakeConfig()

    code = cli.cmd_init_db(
        args,
        environ={},
        cwd=work_dir,
        alembic_command=fake_command,
        config_factory=lambda: config,
        migration_script_dir=work_dir,
    )

    assert code == 0
    assert len(fake_command.upgrade_calls) == 1
    upgraded_config, revision = fake_command.upgrade_calls[0]
    assert upgraded_config is config
    assert revision == "head"
    assert config.options["script_location"] == str(work_dir.resolve())
    assert config.options["sqlalchemy.url"] == make_sqlite_url(
        (data_dir / "dajidali.sqlite3").resolve()
    )
    assert data_dir.is_dir()


def test_init_db_uses_database_url_from_environment(work_dir: Path) -> None:
    fake_command = FakeAlembicCommand()
    config = FakeConfig()
    args = cli.parse_args(["init-db", "--data-dir", str(work_dir / "data")])

    code = cli.cmd_init_db(
        args,
        environ={"DATABASE_URL": "sqlite:///./explicit.sqlite3"},
        cwd=work_dir,
        alembic_command=fake_command,
        config_factory=lambda: config,
        migration_script_dir=work_dir,
    )

    assert code == 0
    assert config.options["sqlalchemy.url"] == make_sqlite_url(
        (work_dir / "explicit.sqlite3").resolve()
    )


def test_serve_api_invokes_uvicorn_without_importing_app(
    work_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[Any, dict[str, Any]]] = []

    def fake_uvicorn(target: Any, **kwargs: Any) -> None:
        calls.append((target, kwargs))

    args = cli.parse_args(
        ["serve-api", "--host", "127.0.0.1", "--port", "9123", "--data-dir", str(work_dir / "data")]
    )
    code = cli.cmd_serve_api(args, environ={}, cwd=work_dir, uvicorn_run=fake_uvicorn)

    assert code == 0
    assert calls == [
        (
            "dajidali.api.app:app",
            {"host": "127.0.0.1", "port": 9123, "log_level": "info"},
        )
    ]
    output = capsys.readouterr().out
    assert "部署模式：本机模式" in output
    assert f"数据目录：{(work_dir / 'data').resolve()}" in output


def test_serve_worker_once_uses_single_iteration(work_dir: Path) -> None:
    worker = FakeWorker()
    args = cli.parse_args(["serve-worker", "--once", "--data-dir", str(work_dir / "data")])

    code = cli.cmd_serve_worker(args, environ={}, cwd=work_dir, worker=worker)

    assert code == 0
    assert worker.calls == [{"poll_interval": 2.0, "max_iterations": 1}]


def test_serve_worker_without_once_runs_forever(work_dir: Path) -> None:
    worker = FakeWorker(iterations=7)
    args = cli.parse_args(["serve-worker", "--data-dir", str(work_dir / "data")])

    code = cli.cmd_serve_worker(args, environ={}, cwd=work_dir, worker=worker)

    assert code == 0
    assert worker.calls[0]["max_iterations"] is None


def test_serve_scheduler_once_uses_max_iterations(work_dir: Path) -> None:
    runner = FakeSchedulerRunner()
    factory = object()
    args = cli.parse_args(["serve-scheduler", "--once", "--data-dir", str(work_dir / "data")])

    code = cli.cmd_serve_scheduler(
        args,
        environ={},
        cwd=work_dir,
        session_factory=factory,  # type: ignore[arg-type]
        scheduler_runner=runner,
    )

    assert code == 0
    assert len(runner.calls) == 1
    used_factory, kwargs = runner.calls[0]
    assert used_factory is factory
    assert kwargs["max_iterations"] == 1
    assert kwargs["interval"] == 30.0
    assert kwargs["tick"] is None


def test_create_admin_repeat_call_reports_existing_account(work_dir: Path) -> None:
    engine = create_engine_from_url(make_sqlite_url(work_dir / "admin.sqlite3"))
    create_schema(engine)
    factory = create_session_factory(engine)
    args = cli.parse_args(
        [
            "create-admin",
            "--username",
            "admin",
            "--password",
            "password123",
            "--data-dir",
            str(work_dir / "data"),
        ]
    )
    try:
        assert cli.cmd_create_admin(args, environ={}, cwd=work_dir, session_factory=factory) == 0
        with pytest.raises(cli.CliError, match="已存在"):
            cli.cmd_create_admin(args, environ={}, cwd=work_dir, session_factory=factory)
    finally:
        engine.dispose()


def test_healthcheck_succeeds_on_ready_response(work_dir: Path) -> None:
    response = FakeResponse(200, {"status": "ready"})
    args = cli.parse_args(
        ["healthcheck", "--url", "http://127.0.0.1:8000/api/v1/health/ready", "--timeout", "0"]
    )

    code = cli.cmd_healthcheck(
        args,
        http_get=lambda *args, **kwargs: response,
        sleep=lambda _: None,
        clock=lambda: 0.0,
    )

    assert code == 0


def test_healthcheck_fails_on_unready_response() -> None:
    response = FakeResponse(503, {"status": "starting"})
    args = cli.parse_args(
        ["healthcheck", "--url", "http://127.0.0.1:8000/api/v1/health/ready", "--timeout", "0"]
    )

    code = cli.cmd_healthcheck(
        args,
        http_get=lambda *args, **kwargs: response,
        sleep=lambda _: None,
        clock=lambda: 0.0,
    )

    assert code == 1


def test_probe_health_accepts_live_and_ready_statuses() -> None:
    assert cli.probe_health(
        "http://127.0.0.1:8000/api/v1/health/ready",
        http_get=lambda *args, **kwargs: FakeResponse(200, {"status": "ready"}),
    ) == (True, "HTTP 200，status=ready")
    assert cli.probe_health(
        "http://127.0.0.1:8000/api/v1/health/live",
        http_get=lambda *args, **kwargs: FakeResponse(200, {"status": "ok"}),
    )[0]


def test_probe_health_rejects_invalid_payload_and_network_error() -> None:
    ok, detail = cli.probe_health(
        "http://127.0.0.1:8000/api/v1/health/ready",
        http_get=lambda *args, **kwargs: FakeResponse(200, {"status": "bad"}),
    )
    assert ok is False
    assert "status" in detail

    ok, detail = cli.probe_health(
        "http://127.0.0.1:8000/api/v1/health/ready",
        http_get=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("down")),
    )
    assert ok is False
    assert "请求失败" in detail


def test_wait_for_health_retries_until_ready() -> None:
    responses = iter(
        [
            FakeResponse(503, {"status": "starting"}),
            FakeResponse(200, {"status": "ready"}),
        ]
    )
    sleeps: list[float] = []

    ok, _, attempts = cli.wait_for_health(
        "http://127.0.0.1:8000/api/v1/health/ready",
        timeout=5.0,
        interval=0.25,
        http_get=lambda *args, **kwargs: next(responses),
        sleep=sleeps.append,
        clock=lambda: 0.0,
    )

    assert ok is True
    assert attempts == 2
    assert sleeps == [0.25]


@pytest.mark.parametrize(
    "url",
    ["ftp://127.0.0.1/health", "http:///health", "http://user:pass@127.0.0.1/health"],
)
def test_health_url_validation_rejects_unsafe_targets(url: str) -> None:
    with pytest.raises(cli.CliError):
        cli.validate_health_url(url)


def test_scheduler_loop_once_ticks_and_releases_lease(work_dir: Path) -> None:
    engine = create_engine_from_url(make_sqlite_url(work_dir / "scheduler.sqlite3"))
    create_schema(engine)
    factory = create_session_factory(engine)
    ticks: list[Any] = []
    try:
        iterations = run_scheduler_forever(
            factory,
            schedule_id="unit-test",
            interval=0.01,
            max_iterations=1,
            tick=lambda repository, now: ticks.append((repository, now)),
        )
        assert iterations == 1
        assert len(ticks) == 1
        with factory() as session:
            assert session.get(SchedulerLease, "unit-test") is None
    finally:
        engine.dispose()
