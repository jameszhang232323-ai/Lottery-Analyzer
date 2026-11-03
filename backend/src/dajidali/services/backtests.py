"""``backtest.run`` 任务编排：冻结计划、时间顺序走查、B0/B1 与 bootstrap。

接口契约（设计文档 9.1—9.3、11.2）：

- ``POST /backtest-jobs`` 只入队；载荷包含 ``snapshot_id``、``strategy``、``k``、
  探索配置（最多 12 套）、``master_seed_hex``（可选，缺省按计划几何派生）、
  ``baseline_trajectories``（默认 200，可降档）、``stage``（train/validation/
  holdout，默认 validation）与可选的 ``name``/``request_id``；
- worker 执行时从快照读取开奖修订，构建 :class:`BacktestPlan`，运行
  :func:`run_walk_forward` 与 B0/B1 :func:`run_baselines`，再用移动块 bootstrap
  给出策略与基线的逐期差值区间；
- 持久化 ``experiment_plan``（冻结参数与全部尝试注册表）、``experiment_run``
  （状态/进度/指标摘要/区间）与 ``backtest_period_result``（逐期候选哈希、命中、
  基线与排除原因）。

幂等：``(owner_id, plan_hash)`` 复用同一计划；``(plan_id, job_id)`` 复用同一
回测运行，重跑会原地重写该运行的逐期结果，不产生重复业务行。

取消与续跑暂不实现；执行期间通过 job 的 ``checkpoint_json``/``progress_json``
记录“已处理期数”，供后续接入真正的续跑逻辑。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from dajidali.data.models import (
    JOB_STATUS_RUNNING,
    BacktestPeriodResult,
    DatasetSnapshot,
    ExperimentPlan,
    ExperimentRun,
    Job,
    as_utc,
    utc_now,
)
from dajidali.domain.canonical import canonical_json, sha256_hex
from dajidali.domain.errors import DomainError
from dajidali.domain.strategy import StrategyConfig
from dajidali.engine.backtest.baselines import (
    B0_PURPOSE,
    B1_PURPOSE,
    DEFAULT_TRAJECTORIES,
    BaselineResult,
    run_baselines,
)
from dajidali.engine.backtest.bootstrap import (
    BOOTSTRAP_METHOD,
    BOOTSTRAP_PURPOSE,
    DEFAULT_BLOCK_LENGTH,
    DEFAULT_CONFIDENCE,
    DEFAULT_RESAMPLES,
    bootstrap_mean_ci,
)
from dajidali.engine.backtest.metrics import ScoredPeriod, diff_values
from dajidali.engine.backtest.plan import (
    HOLDOUT_ALREADY_OPENED,
    MAX_REGISTERED_ATTEMPTS,
    MIN_PERIODS,
    BacktestPlan,
    HoldoutState,
    SplitStage,
    build_backtest_plan,
)
from dajidali.engine.backtest.walk_forward import run_walk_forward
from dajidali.engine.candidates import MAX_CANDIDATES, MIN_CANDIDATES, normalize_seed_hex
from dajidali.jobs.repository import JobRepository
from dajidali.jobs.worker import (
    JobHandler,
    current_job,
    current_job_id,
    current_job_owner_id,
    register_handler,
    unregister_handler,
)
from dajidali.security.authorization import NOT_FOUND_MESSAGE, NotFoundError
from dajidali.services.analysis import load_snapshot_draws
from dajidali.services.candidate_jobs import CandidateJobError, parse_strategy
from dajidali.services.queue_limits import ensure_capacity_for_submission

API_PREFIX = "/api/v1"
BACKTEST_JOB_KIND = "backtest.run"
BACKTEST_PERIOD_HASH_SCHEMA = "backtest-period-v1"
DEFAULT_K = 5
MAX_BASELINE_TRAJECTORIES = 200
SUPPORTED_GAME = "ssq"
BACKTEST_STAGES: tuple[str, ...] = ("train", "validation", "holdout")


class BacktestJobError(DomainError):
    """回测任务输入或前置条件不合法（映射 422）。"""

    code = "BACKTEST_JOB_INVALID"
    http_status = 422


class BacktestConflictError(DomainError):
    """回测状态冲突（封存已打开、幂等键冲突等，映射 409）。"""

    code = "BACKTEST_CONFLICT"
    http_status = 409


def _wrap_strategy_error(exc: CandidateJobError) -> BacktestJobError:
    return BacktestJobError(exc.message, code=exc.code)


def parse_k(value: Any) -> int:
    """每期候选组数 K，必须在 1—20。"""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_CANDIDATES <= value <= MAX_CANDIDATES
    ):
        raise BacktestJobError(
            f"每期候选组数必须为 {MIN_CANDIDATES}—{MAX_CANDIDATES}",
            code="INVALID_K",
            k=value,
        )
    return value


def parse_trajectories(value: Any) -> int:
    """基线重复轨迹数：默认 200，允许降档但必须如实记录。"""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_BASELINE_TRAJECTORIES
    ):
        raise BacktestJobError(
            f"基线轨迹数必须为 1—{MAX_BASELINE_TRAJECTORIES}",
            code="INVALID_TRAJECTORY_COUNT",
            trajectories=value,
        )
    return value


def parse_stage(value: Any) -> SplitStage:
    if value is None:
        return SplitStage.VALIDATION
    if not isinstance(value, str) or value not in BACKTEST_STAGES:
        raise BacktestJobError(
            "stage 只允许 train、validation 或 holdout",
            code="INVALID_STAGE",
            stage=value,
        )
    return SplitStage(value)


def parse_master_seed(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return normalize_seed_hex(value).hex()
    except DomainError as exc:
        raise BacktestJobError(
            "主种子必须是 64 位十六进制字符串",
            code="INVALID_SEED_HEX",
        ) from exc


def parse_attempts(raw: Any) -> list[StrategyConfig]:
    """解析探索配置：最多 12 套、不得重复；空列表交给计划构建器补冻结策略。"""
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise BacktestJobError("attempts 必须是策略配置数组", code="INVALID_ATTEMPTS")
    if len(raw) > MAX_REGISTERED_ATTEMPTS:
        raise BacktestJobError(
            f"尝试注册表最多 {MAX_REGISTERED_ATTEMPTS} 套配置",
            code="ATTEMPT_REGISTRY_FULL",
            max_attempts=MAX_REGISTERED_ATTEMPTS,
            received=len(raw),
        )
    configs: list[StrategyConfig] = []
    seen: set[str] = set()
    for item in raw:
        try:
            config = parse_strategy(item)
        except CandidateJobError as exc:
            raise _wrap_strategy_error(exc) from exc
        version_hash = config.version_hash()
        if version_hash in seen:
            raise BacktestJobError(
                "同一参数配置不能重复注册",
                code="DUPLICATE_ATTEMPT_CONFIG",
                strategy_hash=version_hash,
            )
        seen.add(version_hash)
        configs.append(config)
    return configs


def parse_split(raw: Any) -> dict[str, int] | None:
    """解析请求中的时间划分并校验首版固定 60/20/20。

    首版划分由开奖期数按完整期向下取整派生，请求只能显式确认这组比例，
    不能改变它；接受 ``train``/``validation``/``holdout`` 小数或 ``*_percent``
    百分比两种写法。缺省（``None``）表示使用默认 60/20/20。
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise BacktestJobError("split 必须是对象", code="INVALID_SPLIT_CONFIG")
    percent_keys = {"train_percent", "validation_percent", "holdout_percent"}
    fraction_keys = {"train", "validation", "holdout"}
    unknown = sorted(set(raw) - percent_keys - fraction_keys)
    if unknown:
        raise BacktestJobError(
            "split 含未知字段",
            code="INVALID_SPLIT_CONFIG",
            fields=unknown,
        )
    if set(raw) & percent_keys:
        if set(raw) & fraction_keys or set(raw) != percent_keys:
            raise BacktestJobError(
                "split 必须同时给出 train/validation/holdout 三个百分比",
                code="INVALID_SPLIT_CONFIG",
            )
        values = [raw["train_percent"], raw["validation_percent"], raw["holdout_percent"]]
        target = (60.0, 20.0, 20.0)
    else:
        if set(raw) != fraction_keys:
            raise BacktestJobError(
                "split 必须同时给出 train/validation/holdout 三个比例",
                code="INVALID_SPLIT_CONFIG",
            )
        values = [raw["train"], raw["validation"], raw["holdout"]]
        target = (0.6, 0.2, 0.2)
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in values
    ):
        raise BacktestJobError("split 比例必须是数值", code="INVALID_SPLIT_CONFIG")
    if any(abs(float(value) - expected) > 1e-9 for value, expected in zip(values, target, strict=True)):
        raise BacktestJobError(
            "首版固定 60/20/20 时间划分，split 只能确认该比例",
            code="INVALID_SPLIT_CONFIG",
        )
    return {"train_percent": 60, "validation_percent": 20, "holdout_percent": 20}



def normalize_name(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise BacktestJobError("name 必须为 1—128 字符的非空字符串", code="INVALID_NAME")
    return value


def derive_backtest_request_id(
    owner_id: int,
    snapshot_hash: str,
    strategy_hash: str,
    k: int,
    attempt_hashes: Sequence[str],
    master_seed_hex: str | None,
    trajectories: int,
    stage: str,
) -> str:
    digest = sha256_hex(
        {
            "owner_id": owner_id,
            "snapshot_hash": snapshot_hash,
            "strategy_hash": strategy_hash,
            "k": k,
            "attempts": list(attempt_hashes),
            "master_seed_hex": master_seed_hex,
            "baseline_trajectories": trajectories,
            "stage": stage,
        }
    )
    return f"btr-{digest[:32]}"


def submit_backtest_job(
    session: Session,
    *,
    owner_id: int,
    snapshot_id: int,
    strategy: Any,
    k: int = DEFAULT_K,
    attempts: Any = None,
    split: Any = None,
    master_seed_hex: str | None = None,
    baseline_trajectories: int = DEFAULT_TRAJECTORIES,
    stage: str | None = None,
    name: str | None = None,
    request_id: str | None = None,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """校验输入并幂等入队 ``backtest.run``；返回 202 响应体。"""
    if (
        isinstance(snapshot_id, bool)
        or not isinstance(snapshot_id, int)
        or snapshot_id < 1
    ):
        raise BacktestJobError("snapshot_id 必须为正整数", code="INVALID_SNAPSHOT_ID")
    snapshot = session.get(DatasetSnapshot, snapshot_id)
    if snapshot is None:
        raise NotFoundError(NOT_FOUND_MESSAGE)
    if snapshot.game != SUPPORTED_GAME:
        raise BacktestJobError(
            "回测功能首版只支持双色球",
            code="UNSUPPORTED_GAME",
            game=snapshot.game,
        )
    if snapshot.member_count < MIN_PERIODS:
        raise BacktestJobError(
            f"回测至少需要 {MIN_PERIODS} 期开奖",
            code="DATASET_TOO_SMALL",
            total_periods=snapshot.member_count,
        )
    try:
        parsed_strategy = parse_strategy(strategy)
    except CandidateJobError as exc:
        raise _wrap_strategy_error(exc) from exc
    parsed_k = parse_k(k)
    parsed_attempts = parse_attempts(attempts)
    parsed_split = parse_split(split)
    parsed_trajectories = parse_trajectories(baseline_trajectories)
    parsed_stage = parse_stage(stage)
    parsed_seed = parse_master_seed(master_seed_hex)
    parsed_name = normalize_name(name)
    request_key = request_id or derive_backtest_request_id(
        owner_id,
        snapshot.hash,
        parsed_strategy.version_hash(),
        parsed_k,
        [config.version_hash() for config in parsed_attempts],
        parsed_seed,
        parsed_trajectories,
        parsed_stage.value,
    )
    job_payload = {
        "request_id": request_key,
        "snapshot_id": snapshot.id,
        "snapshot_hash": snapshot.hash,
        "strategy": parsed_strategy.to_canonical(),
        "k": parsed_k,
        "attempts": [config.to_canonical() for config in parsed_attempts],
        "split": parsed_split,
        "master_seed_hex": parsed_seed,
        "baseline_trajectories": parsed_trajectories,
        "stage": parsed_stage.value,
        "name": parsed_name,
    }
    ensure_capacity_for_submission(
        session, owner_id=owner_id, idempotency_key=idempotency_key
    )
    try:
        job = JobRepository(session).enqueue(
            BACKTEST_JOB_KIND,
            job_payload,
            idempotency_key,
            owner_id=owner_id,
            input_hash=sha256_hex(job_payload),
            snapshot_hash=snapshot.hash,
            now=now,
        )
    except DomainError as exc:
        if exc.code == "IDEMPOTENCY_CONFLICT":
            raise BacktestConflictError(
                "幂等键已用于不同载荷",
                code="IDEMPOTENCY_CONFLICT",
                idempotency_key=idempotency_key,
            ) from None
        raise
    return {
        "job_id": job.id,
        "status": job.status,
        "status_url": f"{API_PREFIX}/jobs/{job.id}",
        "request_id": request_key,
        "snapshot_hash": snapshot.hash,
        "stage": parsed_stage.value,
    }


def period_candidate_hash(period: ScoredPeriod, *, strategy_hash: str) -> str:
    """复算某一期候选的内容哈希（不含当期开奖结果，避免未来信息混入）。"""
    return sha256_hex(
        {
            "schema": BACKTEST_PERIOD_HASH_SCHEMA,
            "index": period.index,
            "issue": period.issue,
            "seed_hex": period.seed_hex,
            "snapshot_hash": period.snapshot_hash,
            "strategy_hash": strategy_hash,
            "lines": [
                {"reds": list(line.reds), "blue": line.blue} for line in period.lines
            ],
        }
    )


def _payload_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BacktestJobError(
            f"任务载荷缺少合法的 {key}",
            code="BACKTEST_PAYLOAD_INVALID",
        )
    return value


def _payload_strategy(payload: Mapping[str, Any]) -> StrategyConfig:
    try:
        return parse_strategy(payload.get("strategy"))
    except CandidateJobError as exc:
        raise _wrap_strategy_error(exc) from exc


def _optional_request_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("request_id")
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 64:
        raise BacktestJobError(
            "request_id 必须为 1—64 字符的非空字符串",
            code="INVALID_REQUEST_ID",
        )
    return value


def _checkpoint(checkpoint: Callable[[Mapping[str, Any]], None] | None, state: Mapping[str, Any]) -> None:
    if checkpoint is not None:
        checkpoint(state)


def _write_job_checkpoint(
    session_factory: sessionmaker[Session],
    job: Job,
    state: Mapping[str, Any],
    *,
    now: datetime,
) -> None:
    """尽力更新 job 检查点；失败不影响回测计算（检查点不是业务结果）。"""
    payload = dict(state)
    with suppress(Exception), session_factory() as session:
        session.execute(
            update(Job)
            .where(
                Job.id == job.id,
                Job.status == JOB_STATUS_RUNNING,
                Job.lease_token == job.lease_token,
            )
            .values(
                checkpoint_json=canonical_json(payload),
                progress_json=canonical_json(payload),
                updated_at=as_utc(now),
            )
        )
        session.commit()


def execute_backtest(
    session: Session,
    payload: Mapping[str, Any],
    *,
    owner_id: int,
    job_id: int | None = None,
    checkpoint: Callable[[Mapping[str, Any]], None] | None = None,
    now_provider: Callable[[], datetime] = utc_now,
) -> dict[str, Any]:
    """执行一次回测：冻结计划 → 走查 → B0/B1 → bootstrap → 单事务落库。"""
    snapshot_id = _payload_int(payload, "snapshot_id")
    snapshot = session.get(DatasetSnapshot, snapshot_id)
    if snapshot is None:
        raise BacktestJobError(
            "回测任务引用的快照不存在",
            code="SNAPSHOT_NOT_FOUND",
            snapshot_id=snapshot_id,
        )
    stored_hash = payload.get("snapshot_hash")
    if stored_hash is not None and stored_hash != snapshot.hash:
        raise BacktestJobError(
            "快照哈希与任务不一致",
            code="SNAPSHOT_CHANGED",
            snapshot_id=snapshot_id,
        )
    if snapshot.game != SUPPORTED_GAME:
        raise BacktestJobError(
            "回测功能首版只支持双色球",
            code="UNSUPPORTED_GAME",
            game=snapshot.game,
        )

    strategy = _payload_strategy(payload)
    k = parse_k(payload.get("k"))
    attempt_configs = parse_attempts(payload.get("attempts"))
    requested_split = parse_split(payload.get("split"))
    trajectories = parse_trajectories(payload.get("baseline_trajectories"))
    stage = parse_stage(payload.get("stage"))
    master_seed = parse_master_seed(payload.get("master_seed_hex"))
    request_key = _optional_request_id(payload)
    name = normalize_name(payload.get("name")) or f"回测 {snapshot.end_issue}"

    started_at = as_utc(now_provider())
    draws = load_snapshot_draws(session, snapshot)
    if len(draws) < MIN_PERIODS:
        raise BacktestJobError(
            f"回测至少需要 {MIN_PERIODS} 期开奖",
            code="DATASET_TOO_SMALL",
            total_periods=len(draws),
        )
    try:
        plan: BacktestPlan = build_backtest_plan(
            draws,
            strategy=strategy,
            k=k,
            attempts=attempt_configs or [strategy],
            master_seed_hex=master_seed,
            dataset_hash=snapshot.hash,
        )
    except DomainError as exc:
        raise BacktestJobError(exc.message, code=exc.code) from exc

    plan_hash = plan.plan_hash()
    existing_plan = session.scalar(
        select(ExperimentPlan).where(
            ExperimentPlan.owner_id == owner_id,
            ExperimentPlan.plan_hash == plan_hash,
        )
    )
    if stage is SplitStage.HOLDOUT:
        if existing_plan is not None and existing_plan.holdout_state == HoldoutState.OPENED.value:
            same_job_run = None
            if job_id is not None:
                same_job_run = session.scalar(
                    select(ExperimentRun.id).where(
                        ExperimentRun.plan_id == existing_plan.id,
                        ExperimentRun.job_id == job_id,
                    )
                )
            if same_job_run is None:
                raise BacktestConflictError(
                    "封存测试集只能打开一次；再次运行不再属于未见测试",
                    code=HOLDOUT_ALREADY_OPENED,
                    plan_id=existing_plan.id,
                )
        plan.open_holdout()
        holdout_state = HoldoutState.OPENED.value
    else:
        holdout_state = (
            existing_plan.holdout_state
            if existing_plan is not None
            else HoldoutState.SEALED.value
        )

    evaluable = plan.split.evaluable_periods(stage)
    if not evaluable:
        raise BacktestJobError(
            f"{stage.value} 段没有可评价期（热身 {plan.split.warmup_periods} 期）",
            code="BACKTEST_NO_EVALUABLE_PERIODS",
            stage=stage.value,
            warmup_periods=plan.split.warmup_periods,
        )
    progress_base: dict[str, Any] = {
        "stage": stage.value,
        "request_id": request_key,
        "evaluable_periods": len(evaluable),
        "processed_periods": 0,
        "baseline_trajectories": trajectories,
        "plan_hash": plan_hash,
        "snapshot_id": snapshot.id,
    }
    _checkpoint(checkpoint, {**progress_base, "phase": "walk_forward"})
    walk = run_walk_forward(draws, plan, stage=stage)
    _checkpoint(
        checkpoint,
        {
            **progress_base,
            "phase": "baseline",
            "processed_periods": len(walk.periods),
        },
    )
    suite = run_baselines(draws, plan, trajectories=trajectories, stage=stage)
    b0: BaselineResult = suite.b0
    b1: BaselineResult = suite.b1
    _checkpoint(
        checkpoint,
        {
            **progress_base,
            "phase": "bootstrap",
            "processed_periods": len(walk.periods),
        },
    )

    metric = plan.primary_metric
    b0_ci = bootstrap_mean_ci(
        diff_values(b0.diff_series(walk.periods, metric=metric)),
        plan.seed_rule.derive_seed_hex(BOOTSTRAP_PURPOSE),
        block_length=DEFAULT_BLOCK_LENGTH,
        resamples=DEFAULT_RESAMPLES,
    )
    b1_ci = bootstrap_mean_ci(
        diff_values(b1.diff_series(walk.periods, metric=metric)),
        plan.seed_rule.derive_seed_hex(BOOTSTRAP_PURPOSE),
        block_length=DEFAULT_BLOCK_LENGTH,
        resamples=DEFAULT_RESAMPLES,
    )
    strategy_summary = walk.summary()

    split_payload = {
        "snapshot": {
            "id": snapshot.id,
            "hash": snapshot.hash,
            "start_issue": snapshot.start_issue,
            "end_issue": snapshot.end_issue,
            "member_count": snapshot.member_count,
        },
        "requested": requested_split,
        "split": plan.split.to_canonical(),
        "summary": plan.split.summary(),
    }
    metrics_payload = {
        "plan_hash": plan_hash,
        "dataset_hash": plan.dataset_hash,
        "snapshot_id": snapshot.id,
        "stage": stage.value,
        "k": plan.k,
        "primary_metric": metric.value,
        "strategy_hash": plan.strategy.version_hash(),
        "period_indices": list(walk.period_indices),
        "excluded_periods": [item.to_canonical() for item in walk.excluded],
        "strategy": strategy_summary.to_canonical(),
        "baselines": {
            "trajectory_count": trajectories,
            "conditions_equivalent": suite.conditions_equivalent,
            "b0": {
                "purpose": B0_PURPOSE,
                "period_indices": list(b0.period_indices),
                "summary": b0.pooled_summary().to_canonical(),
            },
            "b1": {
                "purpose": B1_PURPOSE,
                "period_indices": list(b1.period_indices),
                "summary": b1.pooled_summary().to_canonical(),
            },
        },
        "comparisons": {
            "vs_b0": {"point_diff": b0_ci.point, "bootstrap": b0_ci.to_canonical()},
            "vs_b1": {"point_diff": b1_ci.point, "bootstrap": b1_ci.to_canonical()},
        },
    }
    bootstrap_payload = {
        "method": BOOTSTRAP_METHOD,
        "block_length": DEFAULT_BLOCK_LENGTH,
        "resamples": DEFAULT_RESAMPLES,
        "confidence": DEFAULT_CONFIDENCE,
        "primary_metric": metric.value,
        "seed_hex": plan.seed_rule.derive_seed_hex(BOOTSTRAP_PURPOSE),
        "vs_b0": b0_ci.to_canonical(),
        "vs_b1": b1_ci.to_canonical(),
    }
    progress_payload = {
        **progress_base,
        "phase": "finished",
        "processed_periods": len(walk.periods),
        "excluded_periods": len(walk.excluded),
        "bootstrap_resamples": DEFAULT_RESAMPLES,
    }

    if existing_plan is None:
        plan_row = ExperimentPlan(
            owner_id=owner_id,
            name=name,
            plan_hash=plan_hash,
            split_json=canonical_json(split_payload),
            strategy_json=canonical_json(plan.strategy.to_canonical()),
            registry_json=canonical_json(plan.attempts.to_canonical()),
            primary_metric=metric.value,
            master_seed_hex=plan.seed_rule.master_seed_hex,
            holdout_state=holdout_state,
            created_at=started_at,
        )
        session.add(plan_row)
        session.flush()
    else:
        plan_row = existing_plan
        if holdout_state == HoldoutState.OPENED.value and plan_row.holdout_state != holdout_state:
            plan_row.holdout_state = holdout_state

    run: ExperimentRun | None = None
    if job_id is not None:
        run = session.scalar(
            select(ExperimentRun).where(
                ExperimentRun.plan_id == plan_row.id,
                ExperimentRun.job_id == job_id,
            )
        )
    finished_at = as_utc(now_provider())
    if run is None:
        run = ExperimentRun(
            plan_id=plan_row.id,
            status="succeeded",
            progress_json=canonical_json(progress_payload),
            metrics_json=canonical_json(metrics_payload),
            bootstrap_json=canonical_json(bootstrap_payload),
            started_at=started_at,
            finished_at=finished_at,
            job_id=job_id,
        )
        session.add(run)
        session.flush()
    else:
        session.execute(
            delete(BacktestPeriodResult).where(BacktestPeriodResult.run_id == run.id)
        )
        run.status = "succeeded"
        run.progress_json = canonical_json(progress_payload)
        run.metrics_json = canonical_json(metrics_payload)
        run.bootstrap_json = canonical_json(bootstrap_payload)
        run.started_at = started_at
        run.finished_at = finished_at

    strategy_hash = plan.strategy.version_hash()
    b0_means = dict(
        zip(b0.period_indices, b0.period_means(metric=metric), strict=True)
    )
    b1_means = dict(
        zip(b1.period_indices, b1.period_means(metric=metric), strict=True)
    )
    for period in walk.periods:
        hits_payload = {
            "index": period.index,
            "issue": period.issue,
            "draw_date": period.draw_date.isoformat(),
            "status": period.status,
            "reason": period.reason,
            "attempts": period.attempts,
            "seed_hex": period.seed_hex,
            "snapshot_hash": period.snapshot_hash,
            "lines": [
                {"reds": list(line.reds), "blue": line.blue} for line in period.lines
            ],
            "scores": [score.to_canonical() for score in period.scores],
            "red_hits_total": period.red_hits_total,
            "blue_hits_total": period.blue_hits_total,
            "red_hits_per_group": period.mean_red_hits,
            "blue_hit_rate": period.blue_hit_rate,
            "max_red_hits": period.max_red_hits,
        }
        baseline_payload = {
            "primary_metric": metric.value,
            "b0_mean": b0_means[period.index],
            "b1_mean": b1_means[period.index],
            "b0_trajectory_count": b0.trajectory_count,
            "b1_trajectory_count": b1.trajectory_count,
        }
        session.add(
            BacktestPeriodResult(
                run_id=run.id,
                issue=period.issue,
                stage=stage.value,
                candidate_hash=period_candidate_hash(period, strategy_hash=strategy_hash),
                hits_json=canonical_json(hits_payload),
                baseline_json=canonical_json(baseline_payload),
                excluded_reason=None,
            )
        )
    for excluded in walk.excluded:
        session.add(
            BacktestPeriodResult(
                run_id=run.id,
                issue=excluded.issue,
                stage=stage.value,
                candidate_hash=None,
                hits_json=None,
                baseline_json=None,
                excluded_reason=excluded.reason,
            )
        )
    session.commit()
    _checkpoint(checkpoint, progress_payload)

    return {
        "backtest_id": run.id,
        "plan_id": plan_row.id,
        "plan_hash": plan_hash,
        "request_id": request_key,
        "stage": stage.value,
        "status": run.status,
        "period_count": len(walk.periods),
        "excluded_count": len(walk.excluded),
        "baseline_trajectories": trajectories,
        "primary_metric": metric.value,
        "snapshot_id": snapshot.id,
        "snapshot_hash": snapshot.hash,
        "bootstrap": {
            "vs_b0": b0_ci.to_canonical(),
            "vs_b1": b1_ci.to_canonical(),
        },
    }


def _load_json(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):  # pragma: no cover - 正常写入不会出现
        return None


def backtest_payload(session: Session, run: ExperimentRun) -> dict[str, Any]:
    """把 ``experiment_run`` 行还原为接口对象：计划、指标、区间与逐期摘要。"""
    plan = session.get(ExperimentPlan, run.plan_id)
    split_payload = _load_json(plan.split_json) if plan is not None else None
    periods = list(
        session.scalars(
            select(BacktestPeriodResult)
            .where(BacktestPeriodResult.run_id == run.id)
            .order_by(BacktestPeriodResult.id)
        )
    )
    evaluated = [row for row in periods if row.excluded_reason is None]
    return {
        "id": run.id,
        "plan_id": run.plan_id,
        "job_id": run.job_id,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "progress": _load_json(run.progress_json),
        "metrics": _load_json(run.metrics_json),
        "bootstrap": _load_json(run.bootstrap_json),
        "plan": (
            {
                "id": plan.id,
                "owner_id": plan.owner_id,
                "name": plan.name,
                "plan_hash": plan.plan_hash,
                "primary_metric": plan.primary_metric,
                "master_seed_hex": plan.master_seed_hex,
                "holdout_state": plan.holdout_state,
                "created_at": plan.created_at.isoformat() if plan.created_at else None,
                "snapshot": (split_payload or {}).get("snapshot"),
                "requested_split": (split_payload or {}).get("requested"),
                "split": (split_payload or {}).get("split"),
                "split_summary": (split_payload or {}).get("summary"),
                "strategy": _load_json(plan.strategy_json),
                "registry": _load_json(plan.registry_json),
            }
            if plan is not None
            else None
        ),
        "periods": [
            {
                "issue": row.issue,
                "stage": row.stage,
                "candidate_hash": row.candidate_hash,
                "hits": _load_json(row.hits_json),
                "baseline": _load_json(row.baseline_json),
                "excluded_reason": row.excluded_reason,
            }
            for row in periods
        ],
        "counts": {
            "periods": len(periods),
            "evaluated": len(evaluated),
            "excluded": len(periods) - len(evaluated),
        },
    }


def make_backtest_handler(
    session_factory: sessionmaker[Session],
    *,
    now_provider: Callable[[], datetime] = utc_now,
) -> JobHandler:
    """构造 ``backtest.run`` 处理器；执行期间尽力更新 job 检查点。"""

    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        owner_id = current_job_owner_id()
        if owner_id is None:
            raise BacktestJobError(
                "回测任务缺少归属用户",
                code="BACKTEST_OWNER_MISSING",
            )
        job = current_job()

        def checkpoint(state: Mapping[str, Any]) -> None:
            if job is not None:
                _write_job_checkpoint(
                    session_factory,
                    job,
                    state,
                    now=as_utc(now_provider()),
                )

        with session_factory() as session:
            return execute_backtest(
                session,
                payload,
                owner_id=owner_id,
                job_id=current_job_id(),
                checkpoint=checkpoint,
                now_provider=now_provider,
            )

    return handler


def register_handlers(
    session_factory: sessionmaker[Session],
    *,
    now_provider: Callable[[], datetime] = utc_now,
) -> JobHandler:
    """把 ``backtest.run`` 处理器注册到 worker 进程级注册表。"""
    handler = make_backtest_handler(session_factory, now_provider=now_provider)
    register_handler(BACKTEST_JOB_KIND, handler)
    return handler


def unregister_handlers() -> None:
    """移除 ``backtest.run`` 处理器（测试清理用）。"""
    unregister_handler(BACKTEST_JOB_KIND)


__all__ = [
    "API_PREFIX",
    "BACKTEST_JOB_KIND",
    "BACKTEST_PERIOD_HASH_SCHEMA",
    "BACKTEST_STAGES",
    "BacktestConflictError",
    "BacktestJobError",
    "MAX_BASELINE_TRAJECTORIES",
    "backtest_payload",
    "derive_backtest_request_id",
    "execute_backtest",
    "make_backtest_handler",
    "normalize_name",
    "parse_attempts",
    "parse_k",
    "parse_master_seed",
    "parse_split",
    "parse_stage",
    "parse_trajectories",
    "period_candidate_hash",
    "register_handlers",
    "submit_backtest_job",
    "unregister_handlers",
]
