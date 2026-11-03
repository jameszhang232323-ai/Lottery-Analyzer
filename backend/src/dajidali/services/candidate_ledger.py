"""候选批次的创建、锁定与备注追加（设计文档 10.1、10.2、11.1）。

锁定语义：

- 一切时间裁定使用服务端 UTC（开奖日期按 Asia/Shanghai 当地日期比较），
  客户端提交的时间字段不参与判断；
- 目标期已开奖、锁定截止未知或已过、目标日期已过、快照包含目标/未来期、
  批次已锁定/已归档、``expected_revision`` 不匹配，全部返回明确错误码；
- 通过后在一个事务内写入候选核心字段、``lock_hash`` 与审计事件；核心字段
  之后不可修改（服务层拒绝 + ORM 事件保护）。

``lock_hash`` 覆盖号码、目标期、快照、策略版本、算法版本、种子、创建时间与
行哈希，但不含锁定时间本身，因此可在锁定后独立复算校验（附录 B.1：哈希不含
生成时间）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dajidali.data.models import (
    CANDIDATE_BATCH_STATUS_ARCHIVED,
    CANDIDATE_BATCH_STATUS_DRAFT,
    CandidateBatch,
    CandidateLine,
    DatasetSnapshot,
    DrawCalendar,
    DrawCurrent,
    SnapshotMember,
    StrategyVersion,
    User,
    as_utc,
    utc_now,
)
from dajidali.data.repositories.candidates import (
    CandidateRepository,
    StrategyVersionRepository,
    candidate_line_hash,
    decode_reds,
)
from dajidali.data.repositories.draws import validate_issue
from dajidali.data.repositories.settlements import AuditEventRepository
from dajidali.domain.canonical import sha256_hex
from dajidali.domain.draw import SsqNumbers
from dajidali.domain.errors import DomainError
from dajidali.domain.strategy import StrategyConfig
from dajidali.engine.candidates import (
    MAX_CANDIDATES,
    MIN_CANDIDATES,
    RNG_VERSION,
    normalize_seed_hex,
)
from dajidali.engine.rule_resolver import issue_key
from dajidali.security.authorization import NOT_FOUND_MESSAGE, NotFoundError

CANDIDATE_LOCK_SCHEMA = "candidate-lock-v1"
SUPPORTED_GAME = "ssq"
CHINA_TZ = timezone(timedelta(hours=8))
"""开奖日期按中国当地时间比较；时间戳仍统一存 UTC。"""


class CandidateError(DomainError):
    """候选锁定/编辑的前置条件不满足（状态或并发冲突，映射 409）。"""

    code = "CANDIDATE_ERROR"
    http_status = 409


def server_now(now: datetime | None = None) -> datetime:
    """服务端当前时间（UTC）；显式传入的 ``now`` 也必须按 UTC 解释。"""
    return as_utc(now or utc_now())


def server_today(now: datetime | None = None) -> date:
    """服务端当天日期，按 Asia/Shanghai 当地日期判定“目标期日期已过”。"""
    return server_now(now).astimezone(CHINA_TZ).date()


def format_utc(value: datetime) -> str:
    """规范化 UTC 时间文本（``Z`` 结尾），用于锁定内容哈希。"""
    return as_utc(value).isoformat().replace("+00:00", "Z")


def create_candidate_batch(
    session: Session,
    *,
    owner_id: int,
    target_issue: str,
    snapshot_id: int,
    strategy: StrategyConfig,
    seed_hex: str,
    lines: Sequence[SsqNumbers],
    rng_version: str = RNG_VERSION,
    note: str | None = None,
    now: datetime | None = None,
) -> CandidateBatch:
    """创建草稿批次（含号码行与策略版本）；返回已提交的批次。

    草稿允许引用含目标期的快照作为探索记录；真正的锁定资格在
    :func:`lock_batch` 中裁定。
    """
    moment = server_now(now)
    if session.get(User, owner_id) is None:
        raise DomainError(
            "候选批次归属用户不存在",
            code="UNKNOWN_OWNER",
            owner_id=owner_id,
        )
    snapshot = session.get(DatasetSnapshot, snapshot_id)
    if snapshot is None:
        raise DomainError(
            "候选批次引用的快照不存在",
            code="UNKNOWN_SNAPSHOT",
            snapshot_id=snapshot_id,
        )
    if snapshot.game != SUPPORTED_GAME:
        raise DomainError(
            "候选功能首版只支持双色球",
            code="UNSUPPORTED_GAME",
            game=snapshot.game,
        )
    validate_issue(target_issue)
    if not rng_version:
        raise CandidateError("必须提供随机流算法版本", code="INVALID_RNG_VERSION")

    line_list = list(lines)
    if not MIN_CANDIDATES <= len(line_list) <= MAX_CANDIDATES:
        raise CandidateError(
            f"候选组数必须为 {MIN_CANDIDATES}—{MAX_CANDIDATES}",
            code="INVALID_CANDIDATE_COUNT",
            count=len(line_list),
        )
    seen_hashes: set[str] = set()
    for line in line_list:
        if not isinstance(line, SsqNumbers):
            raise CandidateError(
                "候选行必须是双色球号码",
                code="INVALID_CANDIDATE_LINE",
                type=type(line).__name__,
            )
        line_hash = candidate_line_hash(line)
        if line_hash in seen_hashes:
            raise CandidateError(
                "同一批次内候选号码重复",
                code="DUPLICATE_CANDIDATE_LINE",
                line=line.as_text(),
            )
        seen_hashes.add(line_hash)

    seed = normalize_seed_hex(seed_hex)
    strategy_version = StrategyVersionRepository(session).ensure(strategy, now=moment)
    repository = CandidateRepository(session)
    batch = repository.create_batch(
        owner_id=owner_id,
        target_issue=target_issue,
        snapshot_id=snapshot_id,
        strategy_version_id=strategy_version.id,
        seed_hex=seed.hex(),
        rng_version=rng_version,
        candidate_count=len(line_list),
        note=note,
        now=moment,
    )
    for line_no, line in enumerate(line_list, start=1):
        repository.add_line(batch.id, line_no, line)
    session.commit()
    return batch


def lock_batch(
    session: Session,
    *,
    batch_id: int,
    owner_id: int,
    expected_revision: int,
    now: datetime | None = None,
) -> CandidateBatch:
    """校验并锁定草稿批次；返回刷新后的已锁定批次。

    归属不符与不存在统一按 404 处理；其余拒绝原因使用明确错误码（409）。
    """
    moment = server_now(now)
    repository = CandidateRepository(session)
    batch = repository.get_batch(batch_id)
    if batch is None or batch.owner_id != owner_id:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    _require_draft(batch)
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
        raise CandidateError("expected_revision 必须为整数", code="INVALID_EXPECTED_REVISION")
    if batch.revision != expected_revision:
        raise CandidateError(
            "候选批次版本已变化，请刷新后重试",
            code="REVISION_CONFLICT",
            expected_revision=expected_revision,
            actual_revision=batch.revision,
        )

    snapshot = session.get(DatasetSnapshot, batch.snapshot_id)
    if snapshot is None:  # pragma: no cover - 外键保证存在
        raise DomainError(
            "候选批次引用的快照不存在",
            code="UNKNOWN_SNAPSHOT",
            snapshot_id=batch.snapshot_id,
        )
    _require_lock_eligible(session, batch, snapshot, moment)
    lines = repository.list_lines(batch.id)
    if not lines:
        raise CandidateError("候选批次没有号码，不能锁定", code="CANDIDATE_EMPTY")
    if len(lines) != batch.candidate_count:
        raise CandidateError(
            "候选行数与批次记录不一致",
            code="CANDIDATE_LINE_COUNT_MISMATCH",
            expected=batch.candidate_count,
            actual=len(lines),
        )

    lock_hash = compute_lock_hash(session, batch, snapshot=snapshot, lines=lines)
    changed = repository.mark_locked(
        batch_id=batch.id,
        expected_revision=expected_revision,
        locked_at=moment,
        lock_hash=lock_hash,
    )
    if changed != 1:
        session.rollback()
        _raise_stale_write(session, batch_id=batch.id, expected_revision=expected_revision)

    AuditEventRepository(session).append(
        event="candidate.locked",
        entity_type="candidate_batch",
        entity_id=batch.id,
        reason=f"锁定目标期 {batch.target_issue}",
        now=moment,
    )
    try:
        session.commit()
    except IntegrityError as exc:  # pragma: no cover - 审计序号/唯一约束兜底
        session.rollback()
        raise CandidateError(
            "锁定事务冲突，请重试",
            code="LOCK_CONFLICT",
            batch_id=batch.id,
        ) from exc
    session.refresh(batch)
    return batch


def append_note(
    session: Session,
    *,
    batch_id: int,
    owner_id: int,
    note: str | None,
    expected_revision: int,
    now: datetime | None = None,
) -> CandidateBatch:
    """追加备注并递增版本号；草稿与已锁定/已结算批次可用，归档批次拒绝。

    备注不属于锁定核心内容，不会改变 ``lock_hash``；但并发的过期写入会被
    ``expected_revision`` 拒绝。
    """
    repository = CandidateRepository(session)
    batch = repository.get_batch(batch_id)
    if batch is None or batch.owner_id != owner_id:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    if batch.status == CANDIDATE_BATCH_STATUS_ARCHIVED:
        raise CandidateError(
            "候选批次已归档，不能追加备注",
            code="BATCH_ARCHIVED",
            status=batch.status,
        )
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
        raise CandidateError("expected_revision 必须为整数", code="INVALID_EXPECTED_REVISION")
    if batch.revision != expected_revision:
        raise CandidateError(
            "候选批次版本已变化，请刷新后重试",
            code="REVISION_CONFLICT",
            expected_revision=expected_revision,
            actual_revision=batch.revision,
        )
    changed = repository.update_note(
        batch_id=batch.id,
        expected_revision=expected_revision,
        note=note,
    )
    if changed != 1:
        session.rollback()
        _raise_stale_write(session, batch_id=batch.id, expected_revision=expected_revision)
    session.commit()
    session.refresh(batch)
    return batch


def candidate_lock_content(
    session: Session,
    batch: CandidateBatch,
    *,
    snapshot: DatasetSnapshot | None = None,
    strategy_version: StrategyVersion | None = None,
    lines: Sequence[CandidateLine] | None = None,
) -> dict[str, Any]:
    """构造被哈希的规范化锁定内容（不含锁定时间与哈希本身）。"""
    snapshot = snapshot or session.get(DatasetSnapshot, batch.snapshot_id)
    if snapshot is None:  # pragma: no cover - 外键保证存在
        raise DomainError("候选批次引用的快照不存在", code="UNKNOWN_SNAPSHOT")
    strategy_version = strategy_version or session.get(
        StrategyVersion, batch.strategy_version_id
    )
    if strategy_version is None:  # pragma: no cover - 外键保证存在
        raise DomainError("候选批次引用的策略版本不存在", code="UNKNOWN_STRATEGY_VERSION")
    if lines is None:
        lines = CandidateRepository(session).list_lines(batch.id)
    if len(lines) != batch.candidate_count:
        raise CandidateError(
            "候选行数与批次记录不一致",
            code="CANDIDATE_LINE_COUNT_MISMATCH",
            expected=batch.candidate_count,
            actual=len(lines),
        )
    return {
        "schema": CANDIDATE_LOCK_SCHEMA,
        "owner_id": batch.owner_id,
        "game": snapshot.game,
        "target_issue": batch.target_issue,
        "snapshot": {
            "id": snapshot.id,
            "hash": snapshot.hash,
            "member_count": snapshot.member_count,
        },
        "strategy": {
            "id": strategy_version.id,
            "version_hash": strategy_version.version_hash,
        },
        "rng_version": batch.rng_version,
        "seed_hex": batch.seed_hex,
        "candidate_count": batch.candidate_count,
        "created_at": format_utc(batch.created_at),
        "lines": [
            {
                "line_no": line.line_no,
                "reds": list(decode_reds(line.reds_json)),
                "blue": line.blue,
                "line_hash": line.line_hash,
            }
            for line in lines
        ],
    }


def compute_lock_hash(
    session: Session,
    batch: CandidateBatch,
    *,
    snapshot: DatasetSnapshot | None = None,
    strategy_version: StrategyVersion | None = None,
    lines: Sequence[CandidateLine] | None = None,
) -> str:
    """复算候选批次的锁定哈希（规范化 JSON 的 SHA-256）。"""
    return sha256_hex(
        candidate_lock_content(
            session,
            batch,
            snapshot=snapshot,
            strategy_version=strategy_version,
            lines=lines,
        )
    )


def _require_draft(batch: CandidateBatch) -> None:
    if batch.status == CANDIDATE_BATCH_STATUS_ARCHIVED:
        raise CandidateError(
            "候选批次已归档，不能锁定",
            code="BATCH_ARCHIVED",
            status=batch.status,
        )
    if batch.status != CANDIDATE_BATCH_STATUS_DRAFT:
        raise CandidateError(
            "候选批次已锁定，不能重复锁定",
            code="BATCH_ALREADY_LOCKED",
            status=batch.status,
        )


def _require_lock_eligible(
    session: Session,
    batch: CandidateBatch,
    snapshot: DatasetSnapshot,
    moment: datetime,
) -> None:
    game = snapshot.game
    drawn = session.scalar(
        select(DrawCurrent.id).where(
            DrawCurrent.game == game,
            DrawCurrent.issue == batch.target_issue,
        )
    )
    if drawn is not None:
        raise CandidateError(
            "目标期已有开奖结果，不能锁定",
            code="TARGET_ALREADY_DRAWN",
            game=game,
            issue=batch.target_issue,
        )

    calendar = session.scalar(
        select(DrawCalendar).where(
            DrawCalendar.game == game,
            DrawCalendar.issue == batch.target_issue,
        )
    )
    if calendar is None or calendar.lock_deadline is None:
        raise CandidateError(
            "目标期锁定截止时间未知，禁止前瞻锁定",
            code="LOCK_DEADLINE_UNKNOWN",
            game=game,
            issue=batch.target_issue,
        )
    if calendar.status == "cancelled":
        raise CandidateError(
            "目标期已取消，不能锁定",
            code="TARGET_CANCELLED",
            game=game,
            issue=batch.target_issue,
        )
    if calendar.draw_date is None:
        raise CandidateError(
            "目标期开奖日期未知，禁止前瞻锁定",
            code="TARGET_DATE_UNKNOWN",
            game=game,
            issue=batch.target_issue,
        )
    today = server_today(moment)
    if calendar.draw_date < today:
        raise CandidateError(
            "目标期开奖日期已过，不能锁定",
            code="TARGET_DATE_PASSED",
            game=game,
            issue=batch.target_issue,
            draw_date=calendar.draw_date.isoformat(),
            server_today=today.isoformat(),
        )
    deadline = as_utc(calendar.lock_deadline)
    if deadline <= moment:
        raise CandidateError(
            "目标期锁定截止时间已过",
            code="LOCK_DEADLINE_PASSED",
            game=game,
            issue=batch.target_issue,
            lock_deadline=deadline.isoformat(),
        )

    leaked = [
        issue
        for issue in session.scalars(
            select(SnapshotMember.issue).where(
                SnapshotMember.snapshot_id == batch.snapshot_id
            )
        )
        if issue_key(issue) >= issue_key(batch.target_issue)
    ]
    if leaked:
        raise CandidateError(
            "输入快照包含目标期或未来期，禁止正式锁定",
            code="SNAPSHOT_LEAKS_FUTURE",
            target_issue=batch.target_issue,
            issues=sorted(leaked, key=issue_key),
        )


def _raise_stale_write(
    session: Session,
    *,
    batch_id: int,
    expected_revision: int,
) -> None:
    """条件更新失败后重新读取并给出精确原因。"""
    session.expire_all()
    current = session.get(CandidateBatch, batch_id)
    if current is None:  # pragma: no cover - 批次不可能在事务内被删
        raise NotFoundError(NOT_FOUND_MESSAGE)
    if current.status == CANDIDATE_BATCH_STATUS_ARCHIVED:
        raise CandidateError(
            "候选批次已归档，不能修改",
            code="BATCH_ARCHIVED",
            status=current.status,
        )
    if current.status != CANDIDATE_BATCH_STATUS_DRAFT:
        raise CandidateError(
            "候选批次已锁定，不能修改",
            code="BATCH_ALREADY_LOCKED",
            status=current.status,
        )
    if current.revision != expected_revision:
        raise CandidateError(
            "候选批次版本已变化，请刷新后重试",
            code="REVISION_CONFLICT",
            expected_revision=expected_revision,
            actual_revision=current.revision,
        )
    raise CandidateError(
        "并发写入冲突，请重试",
        code="LOCK_CONFLICT",
        batch_id=batch_id,
    )


__all__ = [
    "CANDIDATE_LOCK_SCHEMA",
    "CHINA_TZ",
    "CandidateError",
    "append_note",
    "candidate_lock_content",
    "compute_lock_hash",
    "create_candidate_batch",
    "format_utc",
    "lock_batch",
    "server_now",
    "server_today",
]
