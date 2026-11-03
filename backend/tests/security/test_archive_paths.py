"""归档路径安全、损坏包与哈希校验测试（设计文档 12.1）。

覆盖：``../``、绝对路径、盘符、符号链接成员在解包前被拒绝；未登记成员、
数据库哈希不符、清单行数不符与非法 SQLite 文件被拒绝；失败不会改动当前业务库。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from dajidali.data.database import (
    create_engine_from_url,
    create_schema,
    create_session_factory,
    make_sqlite_url,
)
from dajidali.data.models import User
from dajidali.security.passwords import hash_password
from dajidali.services import backup as backup_module
from dajidali.services.artifacts import PathTraversalError, resolve_data_path
from dajidali.services.backup import (
    ArchivePathError,
    BackupIntegrityError,
    BackupSizeError,
    BackupVersionError,
    create_backup,
    extract_backup,
    validate_backup,
)
from dajidali.services.restore import (
    RestoreVerificationError,
    clear_restore_previews,
    preview_restore,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
TEST_TMP = BACKEND_ROOT / ".test-tmp"
PASSWORD_HASH = hash_password("archive-password-123")


class BackupEnv:
    def __init__(
        self,
        root: Path,
        data_dir: Path,
        database_path: Path,
        engine,
        factory: sessionmaker[Session],
        backup_id: str,
    ) -> None:
        self.root = root
        self.data_dir = data_dir
        self.database_path = database_path
        self.engine = engine
        self.factory = factory
        self.backup_id = backup_id


@contextmanager
def temporary_backup() -> Iterator[BackupEnv]:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    root = TEST_TMP / f"archive-{uuid.uuid4().hex}"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    previous = os.environ.get("DAJIDALI_DATA_DIR")
    os.environ["DAJIDALI_DATA_DIR"] = str(data_dir)
    database_path = root / "live.sqlite3"
    engine = create_engine_from_url(make_sqlite_url(database_path))
    create_schema(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        session.add(
            User(
                username="marker",
                password_hash=PASSWORD_HASH,
                display_name="Marker",
                role="admin",
                is_active=True,
            )
        )
        session.commit()
        result = create_backup(session)
    try:
        yield BackupEnv(root, data_dir, database_path, engine, factory, result.backup_id)
    finally:
        engine.dispose()
        clear_restore_previews()
        if previous is None:
            os.environ.pop("DAJIDALI_DATA_DIR", None)
        else:
            os.environ["DAJIDALI_DATA_DIR"] = previous
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def backup_env() -> Iterator[BackupEnv]:
    with temporary_backup() as value:
        yield value


def _read_entries(archive: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(archive) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def _write_entries(archive: Path, entries: dict[str, bytes]) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def _write_malicious_zip(path: Path, member: str, *, symlink: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo(member)
        if symlink:
            info.external_attr = 0o120777 << 16
        zf.writestr(info, b"payload")


def test_traversal_absolute_and_symlink_members_are_rejected_before_extract(
    backup_env: BackupEnv,
) -> None:
    cases = [
        ("../escape.txt", False),
        ("../../escape.txt", False),
        ("/absolute.txt", False),
        ("C:/windows/evil.txt", False),
        ("files/link", True),
    ]
    for index, (member, symlink) in enumerate(cases):
        archive = backup_env.data_dir / "backups" / f"evil-{index}.zip"
        _write_malicious_zip(archive, member, symlink=symlink)
        destination = backup_env.data_dir / f"extract-{index}"
        with pytest.raises(ArchivePathError):
            extract_backup(
                archive,
                destination,
                allowed_root=backup_env.data_dir,
            )
        assert not destination.exists()
        assert not (backup_env.root / "escape.txt").exists()


def test_unregistered_member_is_rejected(backup_env: BackupEnv) -> None:
    valid = backup_env.data_dir / "backups" / backup_env.backup_id
    entries = _read_entries(valid)
    entries["evil.txt"] = b"unexpected"
    tampered = backup_env.data_dir / "backups" / "unregistered.zip"
    _write_entries(tampered, entries)

    with pytest.raises(BackupIntegrityError):
        validate_backup(tampered, allowed_root=backup_env.data_dir)


def test_unsupported_backup_version_is_rejected(backup_env: BackupEnv) -> None:
    valid = backup_env.data_dir / "backups" / backup_env.backup_id
    entries = _read_entries(valid)
    manifest = json.loads(entries["manifest.json"].decode("utf-8"))
    manifest["backup_schema"] = "dajidali-backup-v99"
    entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    tampered = backup_env.data_dir / "backups" / "future-version.zip"
    _write_entries(tampered, entries)

    with pytest.raises(BackupVersionError):
        validate_backup(tampered, allowed_root=backup_env.data_dir)


def test_database_hash_mismatch_is_rejected(backup_env: BackupEnv) -> None:
    valid = backup_env.data_dir / "backups" / backup_env.backup_id
    entries = _read_entries(valid)
    entries["database.sqlite3"] = entries["database.sqlite3"] + b"tampered"
    tampered = backup_env.data_dir / "backups" / "hash-mismatch.zip"
    _write_entries(tampered, entries)

    with pytest.raises(BackupIntegrityError):
        validate_backup(tampered, allowed_root=backup_env.data_dir)


def test_expected_sha256_mismatch_and_oversize_are_rejected(
    backup_env: BackupEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    valid = backup_env.data_dir / "backups" / backup_env.backup_id
    with pytest.raises(BackupIntegrityError):
        validate_backup(
            valid,
            expected_sha256="0" * 64,
            allowed_root=backup_env.data_dir,
        )

    monkeypatch.setattr(backup_module, "MAX_BACKUP_BYTES", 1)
    with pytest.raises(BackupSizeError):
        validate_backup(valid, allowed_root=backup_env.data_dir)


def test_manifest_row_count_mismatch_rejected_and_live_database_intact(
    backup_env: BackupEnv,
) -> None:
    valid = backup_env.data_dir / "backups" / backup_env.backup_id
    entries = _read_entries(valid)
    manifest = json.loads(entries["manifest.json"].decode("utf-8"))
    manifest["table_row_counts"]["user"] = int(manifest["table_row_counts"]["user"]) + 1
    entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    tampered = backup_env.data_dir / "backups" / "bad-count.zip"
    _write_entries(tampered, entries)

    with pytest.raises(RestoreVerificationError):
        preview_restore("bad-count.zip", data_dir=backup_env.data_dir)

    with backup_env.factory() as session:
        assert session.query(User).count() == 1


def test_corrupt_sqlite_rejected_and_live_database_intact(backup_env: BackupEnv) -> None:
    valid = backup_env.data_dir / "backups" / backup_env.backup_id
    entries = _read_entries(valid)
    manifest = json.loads(entries["manifest.json"].decode("utf-8"))
    broken = b"this is not a sqlite database"
    entries["database.sqlite3"] = broken
    manifest["database"]["sha256"] = hashlib.sha256(broken).hexdigest()
    manifest["database"]["size_bytes"] = len(broken)
    entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    tampered = backup_env.data_dir / "backups" / "broken-db.zip"
    _write_entries(tampered, entries)

    # 校验阶段通过（哈希与清单一致），隔离试恢复阶段发现不是 SQLite。
    validate_backup(tampered, allowed_root=backup_env.data_dir)
    with pytest.raises(RestoreVerificationError):
        preview_restore("broken-db.zip", data_dir=backup_env.data_dir)

    with backup_env.factory() as session:
        assert session.query(User).count() == 1


def test_symlink_escape_in_data_directory_is_rejected(
    backup_env: BackupEnv,
) -> None:
    outside = backup_env.root / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    link = backup_env.data_dir / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    with pytest.raises(PathTraversalError):
        resolve_data_path("link/secret.txt", data_dir=backup_env.data_dir)


def test_backup_skips_evidence_that_points_outside_data_dir(backup_env: BackupEnv) -> None:
    outside = backup_env.root / "outside-evidence.txt"
    outside.write_text("outside-evidence", encoding="utf-8")
    with backup_env.factory() as session:
        from sqlalchemy import text

        session.execute(
            text(
                "INSERT INTO artifact "
                "(owner_id, kind, filename, content_type, size_bytes, sha256, storage_path, created_at) "
                "VALUES (NULL, 'ai_response', 'evil.txt', 'text/plain', :size, :sha, "
                "'../outside-evidence.txt', CURRENT_TIMESTAMP)"
            ),
            {"size": outside.stat().st_size, "sha": hashlib.sha256(outside.read_bytes()).hexdigest()},
        )
        session.commit()
        result = create_backup(session)

    with zipfile.ZipFile(result.path) as zf:
        names = zf.namelist()
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    assert not any("outside-evidence" in name for name in names)
    assert any(
        item.get("reason") == "unsafe_path" for item in manifest["skipped_files"]
    )
