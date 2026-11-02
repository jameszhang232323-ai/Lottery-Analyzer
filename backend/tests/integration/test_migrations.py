"""Alembic 迁移集成测试：upgrade head 建全表，downgrade base 清空。

迁移测试使用 ``alembic.config.Config`` + ``alembic.command`` 指向同一个临时库，
并通过 ``DATABASE_URL`` 验证环境变量覆盖 ini 的行为。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from dajidali.data.database import create_engine_from_url, make_sqlite_url
from dajidali.data.models import Base

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"

REQUIRED_TABLES = {
    "lottery_game",
    "draw_calendar",
    "source_observation",
    "draw_revision",
    "draw_current",
    "draw_prize",
    "verification_evidence",
    "dataset_snapshot",
    "snapshot_member",
    "user",
    "session",
    "job",
    "job_event",
    "scheduler_lease",
    "artifact",
}


@contextmanager
def temporary_database_path() -> Iterator[Path]:
    """每个迁移测试独立临时库：backend/.test-tmp/<uuid>.sqlite3。"""
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    db_path = TEST_TMP / f"{uuid.uuid4().hex}.sqlite3"
    try:
        yield db_path
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def make_alembic_config(db_path: Path) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", make_sqlite_url(db_path))
    return config


def table_names(db_path: Path) -> set[str]:
    engine = create_engine_from_url(make_sqlite_url(db_path))
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_head_creates_all_tables(monkeypatch) -> None:
    with temporary_database_path() as db_path:
        config = make_alembic_config(db_path)
        # 迁移环境优先读取 DATABASE_URL，这里验证它与 Config 指向同一临时库。
        monkeypatch.setenv("DATABASE_URL", make_sqlite_url(db_path))
        command.upgrade(config, "head")

        tables = table_names(db_path)
        assert tables - {"alembic_version"} == set(Base.metadata.tables)
        assert tables >= REQUIRED_TABLES

        engine = create_engine_from_url(make_sqlite_url(db_path))
        try:
            with engine.connect() as connection:
                version = connection.execute(
                    text("select version_num from alembic_version")
                ).scalar_one()
                assert version == "0001"
                games = connection.execute(
                    text("select code, enabled from lottery_game order by code")
                ).all()
            assert [row[0] for row in games] == ["fc3d", "ssq"]
            assert {row[0]: bool(row[1]) for row in games} == {
                "fc3d": False,
                "ssq": True,
            }

            inspector = inspect(engine)
            job_columns = {column["name"] for column in inspector.get_columns("job")}
            assert {
                "lease_token",
                "lease_expires_at",
                "idempotency_key",
                "checkpoint_json",
                "progress_json",
            } <= job_columns
            revision_uniques = {
                constraint["name"]
                for constraint in inspector.get_unique_constraints("draw_revision")
            }
            assert "uq_draw_revision_game_issue_revision" in revision_uniques
            indexes = {index["name"] for index in inspector.get_indexes("job")}
            assert {"ix_job_status_created", "ix_job_status_lease"} <= indexes
        finally:
            engine.dispose()

        # 重复 upgrade head 是安全的空操作。
        command.upgrade(config, "head")
        assert table_names(db_path) - {"alembic_version"} == set(Base.metadata.tables)


def test_downgrade_base_drops_main_tables(monkeypatch) -> None:
    with temporary_database_path() as db_path:
        config = make_alembic_config(db_path)
        monkeypatch.setenv("DATABASE_URL", make_sqlite_url(db_path))
        command.upgrade(config, "head")
        assert "draw_revision" in table_names(db_path)

        command.downgrade(config, "base")

        remaining = table_names(db_path)
        assert remaining <= {"alembic_version"}
        for table in ("draw_revision", "draw_current", "dataset_snapshot", "job", "user"):
            assert table not in remaining


def test_migration_targets_config_url_without_env(monkeypatch) -> None:
    """未设置 DATABASE_URL 时使用 Config 的 sqlalchemy.url（临时库）。"""
    with temporary_database_path() as db_path:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        config = make_alembic_config(db_path)
        command.upgrade(config, "head")
        assert "draw_revision" in table_names(db_path)
        command.downgrade(config, "base")
        assert table_names(db_path) <= {"alembic_version"}
