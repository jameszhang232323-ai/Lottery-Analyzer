"""冻结的回测计划：时间划分、封存闸门、种子派生与尝试注册（设计文档 9.1）。

时间顺序固定为“最早 60% 训练、随后 20% 验证、最后 20% 封存”，边界按完整
开奖期向下取整；每段实际期数与因热身不足被排除的期数分别列出。热身长度取
策略最长特征窗口（``S1`` 的 ``30/100/300``；``"all"`` 与 ``S0`` 没有固定
特征窗口，热身期为 0）。

封存闸门是计划内的单一可变对象：:class:`BacktestPlan` 本身冻结，但
``open_holdout()`` 需要恰好成功一次。封存打开后，任何新参数尝试必须显式
标记为探索（``exploration=True``），否则拒绝，避免把封存结果当作未见数据
继续调参。

本模块只做纯计算，不读数据库、不写文件、不联网。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from dajidali.domain.canonical import sha256_hex
from dajidali.domain.draw import DrawRevision, SsqNumbers
from dajidali.domain.errors import DomainError
from dajidali.domain.strategy import StrategyConfig, StrategyType
from dajidali.engine.backtest.metrics import PRIMARY_METRIC, MetricName
from dajidali.engine.candidates import MAX_CANDIDATES, MIN_CANDIDATES, normalize_seed_hex
from dajidali.engine.random_stream import candidate_domain
from dajidali.engine.statistics.frequency import validate_draws

TRAIN_PERCENT = 60
VALIDATION_PERCENT = 20
HOLDOUT_PERCENT = 20

DEFAULT_K = 5
MIN_PERIODS = 5
MAX_REGISTERED_ATTEMPTS = 12
SEED_SCHEMA = "backtest-seed-v1"
SNAPSHOT_SCHEMA = "backtest-snapshot-v1"

HOLDOUT_ALREADY_OPENED = "HOLDOUT_ALREADY_OPENED"
HOLDOUT_SEALED = "HOLDOUT_SEALED"
ATTEMPT_REQUIRES_EXPLORATION = "ATTEMPT_REQUIRES_EXPLORATION"
ATTEMPT_REGISTRY_FULL = "ATTEMPT_REGISTRY_FULL"
DUPLICATE_ATTEMPT_CONFIG = "DUPLICATE_ATTEMPT_CONFIG"
DATASET_TOO_SMALL = "DATASET_TOO_SMALL"
INVALID_PLAN_PERIODS = "INVALID_PLAN_PERIODS"
INVALID_K = "INVALID_K"
INVALID_STRATEGY = "INVALID_STRATEGY"
INVALID_DATASET_HASH = "INVALID_DATASET_HASH"


class SplitStage(StrEnum):
    """回测时间分段。"""

    TRAIN = "train"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


class HoldoutState(StrEnum):
    """封存集状态：打开后不可回到 sealed。"""

    SEALED = "sealed"
    OPENED = "opened"


def normalize_draws(draws: Sequence[DrawRevision]) -> tuple[DrawRevision, ...]:
    """按开奖日期（同日再按期号）升序排序，并校验为双色球当前修订序列。"""
    items = tuple(draws)
    if not items:
        return ()
    ordered = tuple(
        sorted(items, key=lambda revision: (revision.draw_date, revision.issue))
    )
    return validate_draws(ordered)


def draws_snapshot_hash(draws: Sequence[DrawRevision]) -> str:
    """对一段开奖前缀取内容哈希；只包含传入的期次，天然不覆盖未来数据。"""
    payload = []
    for revision in draws:
        if not isinstance(revision.numbers, SsqNumbers):
            raise DomainError("开奖快照只支持双色球", code="UNSUPPORTED_GAME")
        payload.append(
            {
                "issue": revision.issue,
                "revision": revision.revision,
                "draw_date": revision.draw_date.isoformat(),
                "reds": list(revision.numbers.reds),
                "blue": revision.numbers.blue,
            }
        )
    return sha256_hex({"schema": SNAPSHOT_SCHEMA, "draws": payload})


def feature_warmup_periods(strategy: StrategyConfig) -> int:
    """返回最长特征窗口所需的先前期数（``all`` 与 ``S0`` 为 0）。"""
    if not isinstance(strategy, StrategyConfig):
        raise DomainError("策略必须是 StrategyConfig", code=INVALID_STRATEGY)
    if strategy.strategy_type is not StrategyType.S1:
        return 0
    window = strategy.window
    return 0 if window == "all" else int(window)


@dataclass(frozen=True, slots=True)
class PeriodRange:
    """半开区间 ``[start, end)``，以 0 为最早一期。"""

    stage: SplitStage
    start: int
    end: int

    def __post_init__(self) -> None:
        try:
            stage = SplitStage(self.stage)
        except ValueError as exc:
            raise DomainError(
                "未知的分段",
                code=INVALID_PLAN_PERIODS,
                stage=self.stage,
            ) from exc
        object.__setattr__(self, "stage", stage)
        if isinstance(self.start, bool) or not isinstance(self.start, int):
            raise DomainError("分段起点必须为整数", code=INVALID_PLAN_PERIODS)
        if isinstance(self.end, bool) or not isinstance(self.end, int):
            raise DomainError("分段终点必须为整数", code=INVALID_PLAN_PERIODS)
        if self.start < 0 or self.end < self.start:
            raise DomainError(
                "分段必须满足 0 <= start <= end",
                code=INVALID_PLAN_PERIODS,
                start=self.start,
                end=self.end,
            )

    @property
    def count(self) -> int:
        return self.end - self.start

    @property
    def indices(self) -> tuple[int, ...]:
        return tuple(range(self.start, self.end))

    def to_canonical(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "start_index": self.start,
            "end_index": self.end,
            "periods": self.count,
        }


def build_split(
    total_periods: int,
    strategy: StrategyConfig | None = None,
    *,
    feature_window: int | str | None = None,
) -> SplitPlan:
    """按固定 60/20/20 与完整期边界构造划分。

    边界使用向下取整（``train_end = floor(N*0.6)``、``validation_end =
    floor(N*0.8)``），保证三段都是完整开奖期且合计等于 ``N``。热身长度等于
    最长特征窗口（``S1`` 的 ``30/100/300``）；``S0`` 与 ``all`` 没有固定
    特征窗口，热身期为 0。
    """
    if (
        isinstance(total_periods, bool)
        or not isinstance(total_periods, int)
        or total_periods < MIN_PERIODS
    ):
        raise DomainError(
            f"回测至少需要 {MIN_PERIODS} 期开奖",
            code=DATASET_TOO_SMALL,
            total_periods=total_periods,
        )
    if strategy is None and feature_window is None:
        feature = 0
    elif strategy is not None:
        feature = feature_warmup_periods(strategy)
    else:
        if feature_window not in (30, 100, 300, "all", 0, None):
            raise DomainError(
                "特征窗口必须是 30、100、300、all 或 0",
                code="INVALID_FEATURE_WINDOW",
                feature_window=feature_window,
            )
        feature = 0 if feature_window in ("all", None) else int(feature_window)

    train_end = total_periods * TRAIN_PERCENT // 100
    validation_end = total_periods * (TRAIN_PERCENT + VALIDATION_PERCENT) // 100
    return SplitPlan(
        total_periods=total_periods,
        train=PeriodRange(SplitStage.TRAIN, 0, train_end),
        validation=PeriodRange(SplitStage.VALIDATION, train_end, validation_end),
        holdout=PeriodRange(SplitStage.HOLDOUT, validation_end, total_periods),
        feature_window=feature_window if strategy is None else strategy.window,
        feature_warmup_periods=feature,
        warmup_periods=feature,
    )


@dataclass(frozen=True, slots=True)
class SplitPlan:
    """冻结的时间划分；索引直接对应按日期升序后的开奖序列。"""

    total_periods: int
    train: PeriodRange
    validation: PeriodRange
    holdout: PeriodRange
    warmup_periods: int
    feature_window: int | str | None = None
    feature_warmup_periods: int = 0

    def __post_init__(self) -> None:
        if self.train.start != 0 or self.train.end != self.validation.start:
            raise DomainError("训练与验证分段必须首尾相接", code=INVALID_PLAN_PERIODS)
        if self.validation.end != self.holdout.start:
            raise DomainError("验证与封存分段必须首尾相接", code=INVALID_PLAN_PERIODS)
        if self.holdout.end != self.total_periods:
            raise DomainError("封存分段必须结束于最后一期", code=INVALID_PLAN_PERIODS)
        if (
            isinstance(self.warmup_periods, bool)
            or not isinstance(self.warmup_periods, int)
            or self.warmup_periods < 0
        ):
            raise DomainError("热身期必须为非负整数", code=INVALID_PLAN_PERIODS)

    @property
    def segments(self) -> tuple[PeriodRange, PeriodRange, PeriodRange]:
        return (self.train, self.validation, self.holdout)

    def range_for(self, stage: SplitStage | str) -> PeriodRange:
        normalized = SplitStage(stage)
        for segment in self.segments:
            if segment.stage is normalized:
                return segment
        raise DomainError(  # pragma: no cover - 三个分段总有一个匹配
            "未知的分段",
            code=INVALID_PLAN_PERIODS,
            stage=stage,
        )

    def evaluable_periods(self, stage: SplitStage | str) -> tuple[int, ...]:
        """该段中热身已满、可评价的期序号。"""
        segment = self.range_for(stage)
        return tuple(range(max(segment.start, self.warmup_periods), segment.end))

    def excluded_periods(self, stage: SplitStage | str) -> tuple[int, ...]:
        """该段中因热身历史不足而排除的期序号。"""
        segment = self.range_for(stage)
        return tuple(range(segment.start, min(segment.end, self.warmup_periods)))

    def segment_summary(self, stage: SplitStage | str) -> dict[str, Any]:
        segment = self.range_for(stage)
        excluded = self.excluded_periods(segment.stage)
        return {
            "start_index": segment.start,
            "end_index": segment.end,
            "periods": segment.count,
            "warmup_excluded_periods": len(excluded),
            "evaluable_periods": segment.count - len(excluded),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "total_periods": self.total_periods,
            "warmup_periods": self.warmup_periods,
            "feature_warmup_periods": self.feature_warmup_periods,
            "feature_window": self.feature_window,
            "train": self.segment_summary(SplitStage.TRAIN),
            "validation": self.segment_summary(SplitStage.VALIDATION),
            "holdout": self.segment_summary(SplitStage.HOLDOUT),
        }

    def to_canonical(self) -> dict[str, Any]:
        return {
            "total_periods": self.total_periods,
            "warmup_periods": self.warmup_periods,
            "feature_window": self.feature_window,
            "feature_warmup_periods": self.feature_warmup_periods,
            "train": self.train.to_canonical(),
            "validation": self.validation.to_canonical(),
            "holdout": self.holdout.to_canonical(),
        }


@dataclass(slots=True)
class HoldoutGate:
    """封存集的单向闸门：``sealed -> opened`` 只能发生一次。"""

    _state: HoldoutState = HoldoutState.SEALED

    def __post_init__(self) -> None:
        if not isinstance(self._state, HoldoutState):
            object.__setattr__(self, "_state", HoldoutState(self._state))

    @property
    def state(self) -> HoldoutState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state is HoldoutState.OPENED

    def open(self) -> None:
        if self.is_open:
            raise DomainError(
                "封存测试集只能打开一次；再次打开不再是未见测试",
                code=HOLDOUT_ALREADY_OPENED,
            )
        self._state = HoldoutState.OPENED

    def require_open(self) -> None:
        if not self.is_open:
            raise DomainError(
                "封存测试集尚未打开，不能读取封存结果",
                code=HOLDOUT_SEALED,
            )

    def to_canonical(self) -> dict[str, Any]:
        return {"state": self._state.value, "opened": self.is_open}


@dataclass(frozen=True, slots=True)
class SeedRule:
    """确定性的种子派生规则。

    主种子固定 32 字节；每个 ``(purpose, issue, trajectory)`` 先经规范化哈希
    派生独立种子，再作为 ``sha256-counter-v1`` 的种子、并把同一域写入流。
    域隔离与种子派生同时生效，任何一方变化都不会复用随机流。
    """

    master_seed_hex: str
    schema: str = SEED_SCHEMA

    def __post_init__(self) -> None:
        seed = normalize_seed_hex(self.master_seed_hex)
        object.__setattr__(self, "master_seed_hex", seed.hex())
        if not isinstance(self.schema, str) or not self.schema:
            raise DomainError("种子派生 schema 不能为空", code="INVALID_SEED_SCHEMA")

    @property
    def master_seed_bytes(self) -> bytes:
        return bytes.fromhex(self.master_seed_hex)

    def domain(
        self,
        purpose: str,
        *,
        issue: str | None = None,
        trajectory: int | None = None,
    ) -> dict[str, Any]:
        """返回规范化的 ``sha256-counter-v1`` 域。"""
        return candidate_domain(purpose=purpose, issue=issue, trajectory=trajectory)

    def derive_seed_hex(
        self,
        purpose: str,
        *,
        issue: str | None = None,
        trajectory: int | None = None,
    ) -> str:
        """按域派生 64 位十六进制种子；同输入永远同输出。"""
        return sha256_hex(
            {
                "schema": self.schema,
                "master_seed_hex": self.master_seed_hex,
                "domain": self.domain(purpose, issue=issue, trajectory=trajectory),
            }
        )

    def to_canonical(self) -> dict[str, Any]:
        return {"schema": self.schema, "master_seed_hex": self.master_seed_hex}


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """尝试注册表中的一条记录。"""

    index: int
    config: StrategyConfig
    exploration: bool
    stage: SplitStage = SplitStage.TRAIN
    note: str | None = None

    @property
    def strategy_hash(self) -> str:
        return self.config.version_hash()

    def to_canonical(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "strategy_hash": self.strategy_hash,
            "strategy": self.config.to_canonical(),
            "exploration": self.exploration,
            "stage": self.stage.value,
            "note": self.note,
        }


@dataclass(slots=True)
class AttemptRegistry:
    """最多 12 套参数配置的全量尝试注册表（含探索配置）。

    注册表只记录尝试，不参与选优；报告必须展示全部记录，禁止只报告“最优者”。
    """

    max_attempts: int = MAX_REGISTERED_ATTEMPTS
    _records: list[AttemptRecord] = field(default_factory=list)
    _seen_hashes: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= MAX_REGISTERED_ATTEMPTS
        ):
            raise DomainError(
                f"尝试注册表上限必须是 1—{MAX_REGISTERED_ATTEMPTS}",
                code="INVALID_ATTEMPT_LIMIT",
                max_attempts=self.max_attempts,
            )
        for record in self._records:
            self._seen_hashes.add(record.strategy_hash)

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[AttemptRecord]:
        return iter(self._records)

    @property
    def records(self) -> tuple[AttemptRecord, ...]:
        return tuple(self._records)

    def register(
        self,
        config: StrategyConfig,
        *,
        exploration: bool = False,
        stage: SplitStage | str = SplitStage.TRAIN,
        note: str | None = None,
    ) -> AttemptRecord:
        if not isinstance(config, StrategyConfig):
            raise DomainError("尝试配置必须是 StrategyConfig", code=INVALID_STRATEGY)
        strategy_hash = config.version_hash()
        if strategy_hash in self._seen_hashes:
            raise DomainError(
                "同一参数配置不能重复注册",
                code=DUPLICATE_ATTEMPT_CONFIG,
                strategy_hash=strategy_hash,
            )
        if len(self._records) >= self.max_attempts:
            raise DomainError(
                f"尝试注册表最多 {self.max_attempts} 套配置",
                code=ATTEMPT_REGISTRY_FULL,
                max_attempts=self.max_attempts,
            )
        record = AttemptRecord(
            index=len(self._records) + 1,
            config=config,
            exploration=bool(exploration),
            stage=SplitStage(stage),
            note=note,
        )
        self._records.append(record)
        self._seen_hashes.add(strategy_hash)
        return record

    def to_canonical(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "count": len(self._records),
            "records": [record.to_canonical() for record in self._records],
        }


@dataclass(frozen=True, slots=True)
class BacktestPlan:
    """冻结的回测计划。

    除封存闸门与尝试注册表（本体可变、受控）外，所有字段构造后不可修改。
    ``strategy`` 是验证后冻结的一套策略快照；``attempts`` 记录全部探索配置。
    """

    split: SplitPlan
    k: int
    strategy: StrategyConfig
    seed_rule: SeedRule
    primary_metric: MetricName = PRIMARY_METRIC
    dataset_hash: str = ""
    attempts: AttemptRegistry = field(default_factory=AttemptRegistry)
    holdout: HoldoutGate = field(default_factory=HoldoutGate)

    def __post_init__(self) -> None:
        if not isinstance(self.split, SplitPlan):
            raise DomainError("计划必须包含 SplitPlan", code=INVALID_PLAN_PERIODS)
        if (
            isinstance(self.k, bool)
            or not isinstance(self.k, int)
            or not MIN_CANDIDATES <= self.k <= MAX_CANDIDATES
        ):
            raise DomainError(
                f"每期候选组数必须为 {MIN_CANDIDATES}—{MAX_CANDIDATES}",
                code=INVALID_K,
                k=self.k,
            )
        if not isinstance(self.strategy, StrategyConfig):
            raise DomainError("计划必须包含策略快照", code=INVALID_STRATEGY)
        if not isinstance(self.seed_rule, SeedRule):
            raise DomainError("计划必须包含种子派生规则", code="INVALID_SEED_RULE")
        if not isinstance(self.attempts, AttemptRegistry):
            raise DomainError("计划必须包含尝试注册表", code="INVALID_ATTEMPT_REGISTRY")
        if not isinstance(self.holdout, HoldoutGate):
            raise DomainError("计划必须包含封存闸门", code="INVALID_HOLDOUT_GATE")
        try:
            metric = MetricName(self.primary_metric)
        except ValueError as exc:
            raise DomainError(
                "未知的主指标",
                code="INVALID_PRIMARY_METRIC",
                primary_metric=self.primary_metric,
            ) from exc
        object.__setattr__(self, "primary_metric", metric)
        if not isinstance(self.dataset_hash, str) or not self.dataset_hash:
            raise DomainError("数据快照哈希不能为空", code=INVALID_DATASET_HASH)

    @property
    def holdout_state(self) -> HoldoutState:
        return self.holdout.state

    @property
    def holdout_opened(self) -> bool:
        return self.holdout.is_open

    @property
    def attempt_records(self) -> tuple[AttemptRecord, ...]:
        return self.attempts.records

    def open_holdout(self) -> None:
        """打开封存集；只能成功一次，重复调用抛 ``HOLDOUT_ALREADY_OPENED``。"""
        self.holdout.open()

    def require_holdout_open(self) -> None:
        self.holdout.require_open()

    def register_attempt(
        self,
        config: StrategyConfig,
        *,
        exploration: bool = False,
        stage: SplitStage | str = SplitStage.TRAIN,
        note: str | None = None,
    ) -> AttemptRecord:
        """登记一次参数尝试；封存打开后必须显式标记为探索。"""
        if self.holdout.is_open and not exploration:
            raise DomainError(
                "封存测试打开后，任何调参都必须标记为探索",
                code=ATTEMPT_REQUIRES_EXPLORATION,
            )
        return self.attempts.register(
            config,
            exploration=exploration,
            stage=stage,
            note=note,
        )

    def plan_hash(self) -> str:
        """冻结定义的内容哈希（含尝试注册表，不含封存开关状态）。"""
        return sha256_hex(
            {
                "schema": "backtest-plan-v1",
                "dataset_hash": self.dataset_hash,
                "split": self.split.to_canonical(),
                "k": self.k,
                "strategy": self.strategy.to_canonical(),
                "strategy_hash": self.strategy.version_hash(),
                "primary_metric": self.primary_metric.value,
                "seed_rule": self.seed_rule.to_canonical(),
                "attempts": self.attempts.to_canonical(),
            }
        )

    def summary(self) -> dict[str, Any]:
        return {
            "plan_hash": self.plan_hash(),
            "dataset_hash": self.dataset_hash,
            "k": self.k,
            "strategy_hash": self.strategy.version_hash(),
            "strategy": self.strategy.to_canonical(),
            "primary_metric": self.primary_metric.value,
            "master_seed_hex": self.seed_rule.master_seed_hex,
            "holdout_state": self.holdout_state.value,
            "attempt_count": len(self.attempts),
            "split": self.split.summary(),
        }

    def to_canonical(self) -> dict[str, Any]:
        return {
            "schema": "backtest-plan-v1",
            "dataset_hash": self.dataset_hash,
            "split": self.split.to_canonical(),
            "k": self.k,
            "strategy": self.strategy.to_canonical(),
            "strategy_hash": self.strategy.version_hash(),
            "primary_metric": self.primary_metric.value,
            "seed_rule": self.seed_rule.to_canonical(),
            "attempts": self.attempts.to_canonical(),
            "holdout": self.holdout.to_canonical(),
        }


def build_backtest_plan(
    draws: Sequence[DrawRevision],
    *,
    strategy: StrategyConfig,
    k: int = DEFAULT_K,
    attempts: Sequence[StrategyConfig] = (),
    master_seed_hex: str | None = None,
    primary_metric: MetricName | str = PRIMARY_METRIC,
    dataset_hash: str | None = None,
) -> BacktestPlan:
    """从开奖清单构建冻结计划。

    ``master_seed_hex`` 未提供时，由时间划分几何（总期数与三段边界）、策略
    哈希与 K 确定性派生；**不读取任何开奖号码或日期内容**，因此重放时篡改
    ``t`` 及之后的数据不会通过种子反流进 ``t`` 期候选。提供时必须为 64 位
    十六进制。``attempts`` 在封存仍关闭时预注册（最多 12 套，重复配置拒绝）。
    """
    normalized = normalize_draws(draws)
    if not isinstance(strategy, StrategyConfig):
        raise DomainError("策略必须是 StrategyConfig", code=INVALID_STRATEGY)
    split = build_split(len(normalized), strategy)
    if dataset_hash is None:
        resolved_dataset_hash = draws_snapshot_hash(normalized)
    else:
        if not isinstance(dataset_hash, str) or not dataset_hash:
            raise DomainError("数据快照哈希不能为空", code=INVALID_DATASET_HASH)
        resolved_dataset_hash = dataset_hash
    try:
        normalized_metric = MetricName(primary_metric)
    except ValueError as exc:
        raise DomainError(
            "未知的主指标",
            code="INVALID_PRIMARY_METRIC",
            primary_metric=primary_metric,
        ) from exc
    if master_seed_hex is None:
        derived = sha256_hex(
            {
                "schema": SEED_SCHEMA,
                "strategy_hash": strategy.version_hash(),
                "k": k,
                "split": split.to_canonical(),
            }
        )
    else:
        derived = normalize_seed_hex(master_seed_hex).hex()
    plan = BacktestPlan(
        split=split,
        k=k,
        strategy=strategy,
        seed_rule=SeedRule(master_seed_hex=derived),
        primary_metric=normalized_metric,
        dataset_hash=resolved_dataset_hash,
    )
    for config in attempts:
        plan.register_attempt(config)
    return plan


create_plan = build_backtest_plan
"""``build_backtest_plan`` 的简写别名。"""


__all__ = [
    "ATTEMPT_REGISTRY_FULL",
    "ATTEMPT_REQUIRES_EXPLORATION",
    "DATASET_TOO_SMALL",
    "DEFAULT_K",
    "DUPLICATE_ATTEMPT_CONFIG",
    "HOLDOUT_ALREADY_OPENED",
    "HOLDOUT_PERCENT",
    "HOLDOUT_SEALED",
    "MAX_REGISTERED_ATTEMPTS",
    "MIN_PERIODS",
    "PRIMARY_METRIC",
    "SEED_SCHEMA",
    "TRAIN_PERCENT",
    "VALIDATION_PERCENT",
    "AttemptRecord",
    "AttemptRegistry",
    "BacktestPlan",
    "HoldoutGate",
    "HoldoutState",
    "MetricName",
    "PeriodRange",
    "SeedRule",
    "SplitPlan",
    "SplitStage",
    "build_backtest_plan",
    "build_split",
    "create_plan",
    "draws_snapshot_hash",
    "feature_warmup_periods",
    "normalize_draws",
]
