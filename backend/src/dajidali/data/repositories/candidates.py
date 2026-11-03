"""候选批次与候选行仓储（设计文档 6.1、10.1、10.2）。

与开奖仓储不同，本模块**不提交事务**：锁定必须把候选核心字段、``lock_hash`` 与
审计事件放在同一个事务里，由服务层控制提交点。``mark_locked`` / ``update_note``
使用条件 ``UPDATE``（状态 + ``expected_revision``）实现乐观并发，两个过期版本
只有一个能成功。

候选行内容不可覆盖：ORM 事件在 :mod:`dajidali.data.models` 中拒绝更新/删除；
行哈希只由号码与蓝球决定，用于批内去重。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dajidali.data.models import (
    CANDIDATE_BATCH_STATUS_ARCHIVED,
    CANDIDATE_BATCH_STATUS_DRAFT,
    CANDIDATE_BATCH_STATUS_LOCKED,
    CandidateBatch,
    CandidateLine,
    StrategyVersion,
    as_utc,
    utc_now,
)
from dajidali.domain.canonical import canonical_json, sha256_hex
from dajidali.domain.draw import SsqNumbers
from dajidali.domain.errors import DomainError
from dajidali.domain.strategy import StrategyConfig

CANDIDATE_LINE_SCHEMA = "candidate-line-v1"


def candidate_line_hash(line: SsqNumbers) -> str:
    """候选行哈希：只覆盖号码与蓝球，与批次无关（批内唯一由约束保证）。"""
    return sha256_hex(
        {
            "schema": CANDIDATE_LINE_SCHEMA,
            "reds": list(line.reds),
            "blue": line.blue,
        }
    )


def encode_reds(reds: Sequence[int]) -> str:
    """红球以升序整数数组保存为规范化 JSON。"""
    return canonical_json(list(reds))


def decode_reds(payload: str) -> tuple[int, ...]:
    """还原红球数组；数据库内容损坏时给出明确领域错误。"""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:  # pragma: no cover - 正常写入不会出现
        raise DomainError(
            "候选行红球 JSON 不合法",
            code="INVALID_CANDIDATE_LINE",
            payload=payload,
        ) from exc
    if not isinstance(data, list):
        raise DomainError(
            "候选行红球必须是数组",
            code="INVALID_CANDIDATE_LINE",
            payload=payload,
        )
    return tuple(int(value) for value in data)


class StrategyVersionRepository:
    """策略版本的保存与复用：同一参数与条件只保留一个版本行。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def ensure(self, strategy: StrategyConfig, *, now: datetime | None = None) -> StrategyVersion:
        """按 ``version_hash`` 复用已有版本；不存在时追加一行（不提交）。"""
        if not isinstance(strategy, StrategyConfig):
            raise DomainError("策略必须是 StrategyConfig", code="INVALID_STRATEGY")
        version_hash = strategy.version_hash()
        existing = self.get_by_hash(version_hash)
        if existing is not None:
            return existing
        row = StrategyVersion(
            strategy_type=strategy.strategy_type.value,
            params_json=canonical_json(strategy.to_canonical()),
            conditions_json=canonical_json(strategy.conditions.to_canonical()),
            version_hash=version_hash,
            created_at=as_utc(now or utc_now()),
        )
        self._session.add(row)
        try:
            # savepoint：并发写入同一策略版本时只回退本次插入，不影响外层批次事务。
            with self._session.begin_nested():
                self._session.flush()
        except IntegrityError:
            existing = self.get_by_hash(version_hash)
            if existing is None:
                raise
            return existing
        return row

    def get_by_hash(self, version_hash: str) -> StrategyVersion | None:
        return self._session.scalar(
            select(StrategyVersion).where(StrategyVersion.version_hash == version_hash)
        )

    def get(self, version_id: int) -> StrategyVersion | None:
        return self._session.get(StrategyVersion, version_id)


class CandidateRepository:
    """候选批次与候选行的读写；写方法只 flush，不 commit。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_batch(self, batch_id: int) -> CandidateBatch | None:
        return self._session.get(CandidateBatch, batch_id)

    def list_lines(self, batch_id: int) -> list[CandidateLine]:
        return list(
            self._session.scalars(
                select(CandidateLine)
                .where(CandidateLine.batch_id == batch_id)
                .order_by(CandidateLine.line_no)
            )
        )

    def create_batch(
        self,
        *,
        owner_id: int,
        target_issue: str,
        snapshot_id: int,
        strategy_version_id: int,
        seed_hex: str,
        rng_version: str,
        candidate_count: int,
        note: str | None = None,
        now: datetime | None = None,
    ) -> CandidateBatch:
        """插入草稿批次（``revision=1``、``status=draft``），返回未提交的行。"""
        batch = CandidateBatch(
            owner_id=owner_id,
            target_issue=target_issue,
            snapshot_id=snapshot_id,
            strategy_version_id=strategy_version_id,
            seed_hex=seed_hex,
            rng_version=rng_version,
            candidate_count=candidate_count,
            revision=1,
            status=CANDIDATE_BATCH_STATUS_DRAFT,
            created_at=as_utc(now or utc_now()),
            note=note,
        )
        self._session.add(batch)
        self._session.flush()
        return batch

    def add_line(self, batch_id: int, line_no: int, line: SsqNumbers) -> CandidateLine:
        """追加一注候选行；批内行哈希重复由唯一约束兜底。"""
        row = CandidateLine(
            batch_id=batch_id,
            line_no=line_no,
            reds_json=encode_reds(line.reds),
            blue=line.blue,
            line_hash=candidate_line_hash(line),
        )
        self._session.add(row)
        return row

    def mark_locked(
        self,
        *,
        batch_id: int,
        expected_revision: int,
        locked_at: datetime,
        lock_hash: str,
    ) -> int:
        """条件更新草稿为锁定态；返回受影响行数（0 表示版本或状态已过期）。"""
        result = self._session.execute(
            update(CandidateBatch)
            .where(
                CandidateBatch.id == batch_id,
                CandidateBatch.status == CANDIDATE_BATCH_STATUS_DRAFT,
                CandidateBatch.revision == expected_revision,
                CandidateBatch.locked_at.is_(None),
            )
            .values(
                status=CANDIDATE_BATCH_STATUS_LOCKED,
                locked_at=as_utc(locked_at),
                lock_hash=lock_hash,
                revision=expected_revision + 1,
            )
        )
        return int(result.rowcount or 0)

    def update_note(
        self,
        *,
        batch_id: int,
        expected_revision: int,
        note: str | None,
    ) -> int:
        """追加备注并递增版本号（草稿与已锁定批次可用，归档批次拒绝）。

        备注不属于锁定核心内容，因此 ORM 层允许修改；``revision`` 随之递增，
        让并发的过期界面写入得到 ``REVISION_CONFLICT``。
        """
        result = self._session.execute(
            update(CandidateBatch)
            .where(
                CandidateBatch.id == batch_id,
                CandidateBatch.status != CANDIDATE_BATCH_STATUS_ARCHIVED,
                CandidateBatch.revision == expected_revision,
            )
            .values(note=note, revision=expected_revision + 1)
        )
        return int(result.rowcount or 0)


__all__ = [
    "CANDIDATE_LINE_SCHEMA",
    "CandidateRepository",
    "StrategyVersionRepository",
    "candidate_line_hash",
    "decode_reds",
    "encode_reds",
]
