"""采集同步用例：初始化、增量、复核、幂等导入、冲突与续传。

设计对应（5.3、5.4、5.5、11.2、11.3）：

- 初始化按来源分页推进；每页事务提交后写任务检查点，失败重跑从检查点继续；
- 增量只补齐最近窗口内已确认的期号，不按“期号加一”猜测跨年或休市后的下一期；
- 复核最近 30 期，并用检查点游标滚动复核更早历史；来源若返回明确的更正观察，
  追加新修订并保留旧修订；
- 同号不同号码且没有更正证据时只标记争议，不覆盖当前修订，冲突计数写进任务结果；
- 相同来源观察只写一次；重复导入不增加修订。

本模块不实现真实网络访问；来源由调用方注入 :class:`DrawSource`。真实供应商
准入未完成时只能使用录制源，任务结果会保留 ``synthetic`` 标记。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from dajidali.data.models import (
    JOB_STATUS_RUNNING,
    DrawCalendar,
    DrawCurrent,
    Job,
    SourceObservation,
    as_utc,
    require_game,
    utc_now,
)
from dajidali.data.models import DrawRevision as DrawRevisionRow
from dajidali.data.repositories.draws import DrawRepository, issue_sort_key, validate_issue
from dajidali.data.sources import source_from_env
from dajidali.data.sources.base import (
    SOURCE_AUTH_FAILED,
    SOURCE_BAD_REQUEST,
    SOURCE_CURSOR_LOOP,
    SOURCE_INVALID_PAYLOAD,
    SOURCE_RATE_LIMITED,
    SOURCE_TIMEOUT,
    SOURCE_UNAVAILABLE,
    DrawObservation,
    DrawSource,
    SourceError,
    observation_payload,
)
from dajidali.domain.canonical import canonical_json, sha256_hex
from dajidali.domain.draw import DrawRevision, sort_issues
from dajidali.domain.errors import DomainError
from dajidali.jobs.worker import (
    JobHandler,
    current_job,
    register_handler,
    unregister_handler,
)
from dajidali.services.reconciliation import (
    CONFLICT,
    CORRECTION,
    NEW_REVISION,
    classify_observation,
    mark_disputed,
    observation_numbers,
)

SYNC_JOB_KIND = "sync.run"
SYNC_MODES: tuple[str, ...] = ("initialize", "incremental", "review")
DEFAULT_PAGE_SIZE = 10
DEFAULT_REVIEW_WINDOW = 30
DEFAULT_HISTORY_BATCH = 30
DEFAULT_INCREMENTAL_WINDOW = 30
CHECKPOINT_SCHEMA_VERSION = 1

#: 来源错误码到任务错误码的显式映射；两者同名是为了让来源问题在任务上直接可辨。
SOURCE_ERROR_TO_JOB_CODE: dict[str, str] = {
    SOURCE_AUTH_FAILED: "SOURCE_AUTH_FAILED",
    SOURCE_RATE_LIMITED: "SOURCE_RATE_LIMITED",
    SOURCE_TIMEOUT: "SOURCE_TIMEOUT",
    SOURCE_INVALID_PAYLOAD: "SOURCE_INVALID_PAYLOAD",
    SOURCE_UNAVAILABLE: "SOURCE_UNAVAILABLE",
    SOURCE_BAD_REQUEST: "SOURCE_BAD_REQUEST",
    SOURCE_CURSOR_LOOP: "SOURCE_CURSOR_LOOP",
}

CheckpointCallback = Callable[[dict[str, Any]], None]


class SyncError(DomainError):
    """同步请求或检查点不合法（422）。"""

    code = "SYNC_REQUEST_INVALID"
    http_status = 422


@dataclass(frozen=True, slots=True)
class SyncRequest:
    """一次同步请求；可由 API 载荷或 worker 任务载荷构造。"""

    game: str = "ssq"
    mode: str = "incremental"
    start_issue: str | None = None
    end_issue: str | None = None
    review_window: int = DEFAULT_REVIEW_WINDOW
    history_batch: int = DEFAULT_HISTORY_BATCH
    incremental_window: int = DEFAULT_INCREMENTAL_WINDOW

    def __post_init__(self) -> None:
        if self.mode not in SYNC_MODES:
            raise SyncError(
                f"未知的同步模式：{self.mode!r}",
                code="SYNC_MODE_INVALID",
                allowed=list(SYNC_MODES),
            )
        if not isinstance(self.game, str) or not self.game:
            raise SyncError("同步请求缺少彩种", code="SYNC_GAME_REQUIRED")
        if self.start_issue is not None:
            validate_issue(self.start_issue)
        if self.end_issue is not None:
            validate_issue(self.end_issue)
        if (
            self.start_issue is not None
            and self.end_issue is not None
            and issue_sort_key(self.start_issue) > issue_sort_key(self.end_issue)
        ):
            raise SyncError(
                "起始期号不能晚于结束期号",
                code="SYNC_RANGE_INVALID",
                start_issue=self.start_issue,
                end_issue=self.end_issue,
            )
        for name in ("review_window", "history_batch", "incremental_window"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SyncError(
                    f"{name} 必须为正整数",
                    code="SYNC_WINDOW_INVALID",
                    field=name,
                    value=value,
                )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SyncRequest:
        """从任务载荷解析并校验；未知字段不影响首版兼容。"""
        if not isinstance(payload, Mapping):
            raise SyncError("同步任务载荷必须为对象", code="SYNC_PAYLOAD_INVALID")
        game = payload.get("game", "ssq")
        mode = payload.get("mode", "incremental")
        if not isinstance(game, str) or not isinstance(mode, str):
            raise SyncError("同步任务载荷字段类型不合法", code="SYNC_PAYLOAD_INVALID")

        def _optional_issue(key: str) -> str | None:
            value = payload.get(key)
            if value is None:
                return None
            if not isinstance(value, str):
                raise SyncError(
                    f"{key} 必须为字符串",
                    code="SYNC_PAYLOAD_INVALID",
                    field=key,
                )
            return value

        def _window(key: str, default: int) -> int:
            value = payload.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int):
                raise SyncError(
                    f"{key} 必须为整数",
                    code="SYNC_PAYLOAD_INVALID",
                    field=key,
                )
            return value

        return cls(
            game=game,
            mode=mode,
            start_issue=_optional_issue("start_issue"),
            end_issue=_optional_issue("end_issue"),
            review_window=_window("review_window", DEFAULT_REVIEW_WINDOW),
            history_batch=_window("history_batch", DEFAULT_HISTORY_BATCH),
            incremental_window=_window("incremental_window", DEFAULT_INCREMENTAL_WINDOW),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "game": self.game,
            "mode": self.mode,
            "start_issue": self.start_issue,
            "end_issue": self.end_issue,
            "review_window": self.review_window,
            "history_batch": self.history_batch,
            "incremental_window": self.incremental_window,
        }


@dataclass(frozen=True, slots=True)
class SyncPhase:
    """同步计划中的一个连续区间；复核模式会拆成最近段和历史滚动段。"""

    name: str
    start_issue: str | None
    end_issue: str | None


def _empty_counters() -> dict[str, int]:
    return {
        "fetched": 0,
        "observations_written": 0,
        "observations_deduped": 0,
        "inserted": 0,
        "corrected": 0,
        "unchanged": 0,
        "conflicts": 0,
    }


class JobCheckpointWriter:
    """把检查点写回 ``job`` 行；只在当前租约仍有效时成功。

    仓储层没有公开检查点方法，这里用条件 UPDATE 实现与租约一致的提交语义：
    任务不在 running 或 token 不匹配时拒绝写入，避免失效 worker 覆盖新检查点。
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        job_id: int,
        lease_token: str,
        *,
        now_provider: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._job_id = job_id
        self._lease_token = lease_token
        self._now_provider = now_provider
        self._last: dict[str, Any] = {}

    @property
    def last_checkpoint(self) -> dict[str, Any]:
        return dict(self._last)

    def __call__(self, checkpoint: Mapping[str, Any]) -> None:
        self._write(dict(checkpoint))

    def record_error(self, error: SourceError) -> None:
        """把来源失败追加到最后一个检查点，供重试与界面读取 retry_after。"""
        checkpoint = dict(self._last)
        checkpoint["last_error"] = error.to_dict()
        checkpoint["updated_at"] = as_utc(self._now_provider()).isoformat()
        self._write(checkpoint)

    def _write(self, checkpoint: dict[str, Any]) -> None:
        checkpoint.setdefault("updated_at", as_utc(self._now_provider()).isoformat())
        checkpoint_json = canonical_json(checkpoint)
        progress = {
            "phase": checkpoint.get("phase"),
            "pages": checkpoint.get("pages_done", 0),
            "counters": checkpoint.get("counters", _empty_counters()),
            "exhausted": checkpoint.get("exhausted", False),
        }
        timestamp = as_utc(self._now_provider())
        with self._session_factory() as session:
            result = session.execute(
                update(Job)
                .where(
                    Job.id == self._job_id,
                    Job.status == JOB_STATUS_RUNNING,
                    Job.lease_token == self._lease_token,
                )
                .values(
                    checkpoint_json=checkpoint_json,
                    progress_json=canonical_json(progress),
                    updated_at=timestamp,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                raise DomainError(
                    "检查点写入被拒绝：租约已失效",
                    code="LEASE_EXPIRED",
                    job_id=self._job_id,
                )
            session.commit()
        self._last = checkpoint


class SyncService:
    """把来源观察写入来源观察表、修订表与当前指针。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        source: DrawSource,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        now_provider: Callable[[], datetime] = utc_now,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size 必须为正整数")
        capabilities = source.capabilities()
        if not capabilities.games:
            raise ValueError("来源未声明支持任何彩种")
        self._session_factory = session_factory
        self._source = source
        self._capabilities = capabilities
        self._page_size = max(1, min(page_size, capabilities.max_page_size))
        self._now_provider = now_provider

    @property
    def page_size(self) -> int:
        return self._page_size

    def run(
        self,
        request: SyncRequest,
        *,
        checkpoint: Mapping[str, Any] | None = None,
        on_checkpoint: CheckpointCallback | None = None,
    ) -> dict[str, Any]:
        """执行同步；``checkpoint`` 来自上次任务的持久化检查点。"""
        if not self._capabilities.supports(request.game):
            raise SyncError(
                "来源不支持该彩种",
                code="SOURCE_GAME_UNSUPPORTED",
                game=request.game,
            )
        state = self._restore_state(request, checkpoint)
        self._notify(state, on_checkpoint)
        if state.get("exhausted"):
            return self._result(request, state)

        phases, review_state = self._plan(request, state)
        state["phases"] = [phase.name for phase in phases]
        if review_state is not None:
            state["review"] = review_state
        phase_index = int(state.get("phase_index", 0))
        if phase_index >= len(phases):
            state["exhausted"] = True
            self._notify(state, on_checkpoint)
            return self._result(request, state)

        cursor: str | None = state.get("cursor")
        while phase_index < len(phases):
            phase = phases[phase_index]
            while True:
                previous_cursor = cursor
                page = self._source.fetch_page(
                    request.game,
                    phase.start_issue,
                    phase.end_issue,
                    cursor,
                    self._page_size,
                )
                self._process_observations(page.observations, request, phase, state)
                state["pages_done"] = int(state.get("pages_done", 0)) + 1
                cursor = None if page.exhausted else page.next_cursor
                state.update(
                    {
                        "phase_index": phase_index,
                        "phase": phase.name,
                        "cursor": cursor,
                        "exhausted": False,
                    }
                )
                self._notify(state, on_checkpoint)
                if page.exhausted:
                    break
                if cursor is None or cursor == previous_cursor:
                    raise SourceError(
                        "来源分页游标没有推进",
                        code=SOURCE_CURSOR_LOOP,
                        retryable=False,
                        details={"phase": phase.name, "cursor": cursor},
                    )
            phase_index += 1
            cursor = None
            if phase.name == "review.history":
                review = state.setdefault("review", {})
                review["history_cursor"] = phase.start_issue
            state.update(
                {
                    "phase_index": phase_index,
                    "phase": phase.name,
                    "cursor": None,
                    "exhausted": phase_index >= len(phases),
                }
            )
            self._notify(state, on_checkpoint)
        return self._result(request, state)

    # ------------------------------------------------------------------ 计划
    def _plan(
        self,
        request: SyncRequest,
        state: Mapping[str, Any],
    ) -> tuple[list[SyncPhase], dict[str, Any] | None]:
        with self._session_factory() as session:
            require_game(session, request.game)
            known = self._known_issues(session, request.game)
        if request.mode == "initialize":
            return [SyncPhase("initialize", request.start_issue, request.end_issue)], None
        if request.mode == "incremental":
            candidates = [
                issue
                for issue in known
                if request.end_issue is None
                or issue_sort_key(issue) <= issue_sort_key(request.end_issue)
            ]
            end = request.end_issue or (candidates[-1] if candidates else None)
            if end is None:
                raise SyncError(
                    "增量同步需要结束期号或已确认期号目录",
                    code="INCREMENTAL_RANGE_UNKNOWN",
                    game=request.game,
                )
            start = request.start_issue
            if start is None:
                if not candidates:
                    raise SyncError(
                        "没有可用于推导增量窗口的期号目录",
                        code="SYNC_CATALOG_UNKNOWN",
                        game=request.game,
                    )
                start = candidates[max(0, len(candidates) - request.incremental_window)]
            return [SyncPhase("incremental", start, end)], None

        candidates = [
            issue
            for issue in known
            if request.end_issue is None
            or issue_sort_key(issue) <= issue_sort_key(request.end_issue)
        ]
        if not candidates:
            raise SyncError(
                "复核需要已确认的期号目录",
                code="REVIEW_RANGE_UNKNOWN",
                game=request.game,
            )
        recent = candidates[-request.review_window :]
        phases = [SyncPhase("review.recent", recent[0], recent[-1])]
        review_state = dict(state.get("review") or {})
        review_state.setdefault("recent_start", recent[0])
        review_state.setdefault("recent_end", recent[-1])
        history_exhausted = bool(review_state.get("history_exhausted"))
        if not history_exhausted:
            history_cursor = review_state.get("history_cursor")
            older = [issue for issue in candidates if issue_sort_key(issue) < issue_sort_key(recent[0])]
            if history_cursor is not None:
                older = [
                    issue
                    for issue in older
                    if issue_sort_key(issue) < issue_sort_key(history_cursor)
                ]
            if older:
                batch = older[-request.history_batch :]
                phases.append(SyncPhase("review.history", batch[0], batch[-1]))
            else:
                review_state["history_exhausted"] = True
        return phases, review_state

    def _known_issues(self, session: Session, game: str) -> list[str]:
        current = {str(issue) for issue in session.scalars(
            select(DrawCurrent.issue).where(DrawCurrent.game == game)
        )}
        calendar = {str(issue) for issue in session.scalars(
            select(DrawCalendar.issue).where(DrawCalendar.game == game)
        )}
        issues = sorted(issue for issue in current | calendar if issue.isdigit())
        return sort_issues(issues)

    # ------------------------------------------------------------------ 处理
    def _process_observations(
        self,
        observations: tuple[DrawObservation, ...],
        request: SyncRequest,
        phase: SyncPhase,
        state: dict[str, Any],
    ) -> None:
        if not observations:
            return
        counters = state["counters"]
        disputed_issues: list[str] = state.setdefault("disputed_issues", [])
        timestamp = as_utc(self._now_provider())
        with self._session_factory() as session:
            repository = DrawRepository(session)
            for observation in observations:
                self._check_observation(observation, request, phase)
                counters["fetched"] += 1
                payload = observation_payload(observation)
                payload_hash = sha256_hex(payload)
                existing = session.scalar(
                    select(SourceObservation).where(
                        SourceObservation.game == request.game,
                        SourceObservation.issue == observation.issue,
                        SourceObservation.source == self._capabilities.source_id,
                        SourceObservation.payload_hash == payload_hash,
                    )
                )
                observation_id: int | None = None
                if existing is not None:
                    counters["observations_deduped"] += 1
                    observation_id = existing.id
                    applied = session.scalar(
                        select(DrawRevisionRow.id)
                        .where(DrawRevisionRow.source_observation_id == existing.id)
                        .limit(1)
                    )
                    if applied is not None:
                        counters["unchanged"] += 1
                        continue
                else:
                    row = repository.record_observation(
                        request.game,
                        observation.issue,
                        self._capabilities.source_id,
                        observation.observed_at or timestamp,
                        payload=payload,
                        source_url=observation.source_url,
                        raw_ref=observation.raw_ref,
                        now=timestamp,
                    )
                    observation_id = row.id
                    counters["observations_written"] += 1

                current = repository.get_current(request.game, observation.issue)
                numbers = observation_numbers(observation)
                decision = classify_observation(current, observation)
                if decision == NEW_REVISION:
                    repository.append_revision(
                        DrawRevision(
                            game=request.game,
                            issue=observation.issue,
                            revision=1,
                            draw_date=observation.draw_date,
                            numbers=numbers,
                        ),
                        quality_status="unverified",
                        source_mode=self._source_mode,
                        source_observation_id=observation_id,
                        observed_at=observation.observed_at,
                        now=timestamp,
                    )
                    counters["inserted"] += 1
                elif decision == CORRECTION:
                    if current is None:  # pragma: no cover - classify 保证非空
                        raise SyncError(
                            "更正观察缺少当前修订",
                            code="SYNC_REVISION_STATE_INVALID",
                            issue=observation.issue,
                        )
                    repository.append_revision(
                        DrawRevision(
                            game=request.game,
                            issue=observation.issue,
                            revision=current.revision + 1,
                            draw_date=observation.draw_date,
                            numbers=numbers,
                        ),
                        quality_status="unverified",
                        source_mode=self._source_mode,
                        source_observation_id=observation_id,
                        observed_at=observation.observed_at,
                        now=timestamp,
                    )
                    counters["corrected"] += 1
                elif decision == CONFLICT:
                    mark_disputed(
                        session,
                        request.game,
                        observation.issue,
                        now=timestamp,
                    )
                    counters["conflicts"] += 1
                    if observation.issue not in disputed_issues:
                        disputed_issues.append(observation.issue)
                else:  # UNCHANGED
                    counters["unchanged"] += 1

    def _check_observation(
        self,
        observation: DrawObservation,
        request: SyncRequest,
        phase: SyncPhase,
    ) -> None:
        if observation.game != request.game:
            raise SourceError(
                "来源返回了请求彩种之外的观察",
                code=SOURCE_INVALID_PAYLOAD,
                retryable=False,
                details={
                    "expected_game": request.game,
                    "actual_game": observation.game,
                    "issue": observation.issue,
                },
            )
        if not isinstance(observation.issue, str) or not observation.issue.isdigit():
            raise SourceError(
                "来源返回的期号不合法",
                code=SOURCE_INVALID_PAYLOAD,
                retryable=False,
                details={"issue": observation.issue},
            )
        key = issue_sort_key(observation.issue)
        if phase.start_issue is not None and key < issue_sort_key(phase.start_issue):
            raise SourceError(
                "来源返回了区间之外的观察",
                code=SOURCE_INVALID_PAYLOAD,
                retryable=False,
                details={
                    "issue": observation.issue,
                    "start_issue": phase.start_issue,
                },
            )
        if phase.end_issue is not None and key > issue_sort_key(phase.end_issue):
            raise SourceError(
                "来源返回了区间之外的观察",
                code=SOURCE_INVALID_PAYLOAD,
                retryable=False,
                details={
                    "issue": observation.issue,
                    "end_issue": phase.end_issue,
                },
            )

    @property
    def _source_mode(self) -> str:
        return "recorded" if not self._capabilities.license.verified else "online"

    # ------------------------------------------------------------------ 状态
    def _restore_state(
        self,
        request: SyncRequest,
        checkpoint: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if not checkpoint:
            return {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "mode": request.mode,
                "game": request.game,
                "source_id": self._capabilities.source_id,
                "phase_index": 0,
                "phase": None,
                "cursor": None,
                "exhausted": False,
                "pages_done": 0,
                "counters": _empty_counters(),
                "disputed_issues": [],
                "review": None,
            }
        if not isinstance(checkpoint, Mapping):
            raise SyncError("任务检查点必须为对象", code="SYNC_CHECKPOINT_INVALID")
        if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise SyncError(
                "不支持的任务检查点版本",
                code="SYNC_CHECKPOINT_INVALID",
                schema_version=checkpoint.get("schema_version"),
            )
        if checkpoint.get("mode") != request.mode or checkpoint.get("game") != request.game:
            raise SyncError(
                "任务检查点与请求不一致",
                code="SYNC_CHECKPOINT_MISMATCH",
                checkpoint_mode=checkpoint.get("mode"),
                checkpoint_game=checkpoint.get("game"),
            )
        source_id = checkpoint.get("source_id")
        if source_id is not None and source_id != self._capabilities.source_id:
            raise SyncError(
                "任务检查点来源与当前来源不一致",
                code="SYNC_CHECKPOINT_MISMATCH",
                checkpoint_source=source_id,
                source_id=self._capabilities.source_id,
            )
        state = dict(checkpoint)
        state["counters"] = _empty_counters()
        raw_counters = checkpoint.get("counters")
        if isinstance(raw_counters, Mapping):
            for key in state["counters"]:
                value = raw_counters.get(key, 0)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise SyncError(
                        "任务检查点计数不合法",
                        code="SYNC_CHECKPOINT_INVALID",
                        field=key,
                    )
                state["counters"][key] = value
        raw_disputed = checkpoint.get("disputed_issues")
        state["disputed_issues"] = (
            [str(issue) for issue in raw_disputed]
            if isinstance(raw_disputed, list)
            else []
        )
        state["phase_index"] = int(checkpoint.get("phase_index", 0))
        state["cursor"] = checkpoint.get("cursor")
        state["exhausted"] = bool(checkpoint.get("exhausted", False))
        state["pages_done"] = int(checkpoint.get("pages_done", 0))
        return state

    def _notify(
        self,
        state: dict[str, Any],
        callback: CheckpointCallback | None,
    ) -> None:
        if callback is None:
            return
        state["updated_at"] = as_utc(self._now_provider()).isoformat()
        callback(json.loads(canonical_json(state)))

    def _result(self, request: SyncRequest, state: Mapping[str, Any]) -> dict[str, Any]:
        counters = state.get("counters") or _empty_counters()
        return {
            "game": request.game,
            "mode": request.mode,
            "source_id": self._capabilities.source_id,
            "synthetic": not self._capabilities.license.verified,
            "pages": int(state.get("pages_done", 0)),
            "fetched": int(counters.get("fetched", 0)),
            "observations_written": int(counters.get("observations_written", 0)),
            "observations_deduped": int(counters.get("observations_deduped", 0)),
            "inserted": int(counters.get("inserted", 0)),
            "corrected": int(counters.get("corrected", 0)),
            "unchanged": int(counters.get("unchanged", 0)),
            "conflicts": int(counters.get("conflicts", 0)),
            "disputed_issues": list(state.get("disputed_issues") or []),
            "phases": list(state.get("phases") or []),
            "review": state.get("review"),
            "cursor": state.get("cursor"),
            "exhausted": bool(state.get("exhausted", False)),
        }


def load_job_checkpoint(text: str | None) -> dict[str, Any] | None:
    """解析持久化检查点；旧任务或损坏内容按“无检查点”处理。"""
    if not text:
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _source_error_to_domain(error: SourceError) -> DomainError:
    message = error.message
    if error.retry_after is not None:
        message = f"{message} (retry_after={error.retry_after}s)"
    return DomainError(
        message,
        code=SOURCE_ERROR_TO_JOB_CODE.get(error.code, error.code),
        source_code=error.code,
        retryable=error.retryable,
    )


def make_sync_handler(
    session_factory: sessionmaker[Session],
    source: DrawSource,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    now_provider: Callable[[], datetime] = utc_now,
) -> JobHandler:
    """构造 ``sync.run`` 处理器；检查点写回当前 job，来源错误映射错误码。"""

    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        request = SyncRequest.from_payload(payload)
        job = current_job()
        if job is None or job.id is None or not job.lease_token:
            raise SyncError(
                "同步处理器缺少 worker 任务上下文",
                code="SYNC_JOB_CONTEXT_MISSING",
            )
        writer = JobCheckpointWriter(
            session_factory,
            job.id,
            job.lease_token,
            now_provider=now_provider,
        )
        checkpoint = load_job_checkpoint(job.checkpoint_json)
        service = SyncService(
            session_factory,
            source,
            page_size=page_size,
            now_provider=now_provider,
        )
        try:
            return service.run(request, checkpoint=checkpoint, on_checkpoint=writer)
        except SourceError as exc:
            writer.record_error(exc)
            raise _source_error_to_domain(exc) from exc

    return handler


def register_handlers(
    session_factory: sessionmaker[Session],
    source: DrawSource,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    now_provider: Callable[[], datetime] = utc_now,
) -> JobHandler:
    """把同步处理器注册到 worker 进程级注册表，并返回处理器。"""
    handler = make_sync_handler(
        session_factory,
        source,
        page_size=page_size,
        now_provider=now_provider,
    )
    register_handler(SYNC_JOB_KIND, handler)
    return handler


def unregister_handlers() -> None:
    """移除 ``sync.run`` 处理器（测试清理用）。"""
    unregister_handler(SYNC_JOB_KIND)


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "DEFAULT_HISTORY_BATCH",
    "DEFAULT_INCREMENTAL_WINDOW",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_REVIEW_WINDOW",
    "SOURCE_ERROR_TO_JOB_CODE",
    "SYNC_JOB_KIND",
    "SYNC_MODES",
    "JobCheckpointWriter",
    "SyncError",
    "SyncPhase",
    "SyncRequest",
    "SyncService",
    "load_job_checkpoint",
    "make_sync_handler",
    "register_handlers",
    "source_from_env",
    "unregister_handlers",
]
