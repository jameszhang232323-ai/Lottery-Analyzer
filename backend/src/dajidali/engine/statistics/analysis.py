"""统计事实集、质量摘要与内容哈希（设计文档 7.1、8.2、11.1）。

``compute_facts`` 把窗口统计整理成带唯一 ID 的事实列表，供分析页、AI 证据
包和锁定记录引用。每条事实包含：

``fact_id``、``value``、``unit``、``sample_n``、``window``、
``metric_version``，以及 ``lower_bound``。``lower_bound=True`` 表示数值是
下界（界面显示“至少 n 期”），而不是精确历史最大值。

事实 ID 采用 ``ssq.<球色>.<号码>.<指标>.w<窗口>``：

- 红球：``ssq.red.01.count.w100``、``.frequency.``、``.omission.``、
  ``.max_omission.``；
- 蓝球：``ssq.blue.01.count.w100`` 等（同样四类）；
- 结构：``ssq.red.sum.mean.w100``、``ssq.red.span.max.``、
  ``ssq.red.odd.mean.``、``ssq.red.big.mean.``、``ssq.red.zone1.mean.``、
  ``ssq.red.adjacent_pairs.mean.``、``ssq.red.longest_run.max.``、
  ``ssq.red.repeat.mean.``。

单位词汇：``draws``（期数）、``ratio``（期频率）、``sum``（和值）、
``span``（跨度）、``balls``（号码个数）、``pairs``（连号对数）。

窗口内没有任何有效样本时，该窗口不生成事实（不以 0 填充），质量摘要会
说明原因。``analysis_hash`` 只覆盖事实与指标版本，保证同一输入得到同一哈希。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from dajidali.domain.canonical import sha256_hex
from dajidali.domain.draw import DrawRevision
from dajidali.engine import EngineError
from dajidali.engine.statistics.frequency import (
    BLUE_NUMBERS,
    RED_NUMBERS,
    WindowSelection,
    WindowSpec,
    blue_counts,
    blue_frequencies,
    red_counts,
    red_frequencies,
    select_window,
    validate_draws,
    window_label,
    window_requested_periods,
)
from dajidali.engine.statistics.omission import (
    current_omission,
    historical_max_omission,
)
from dajidali.engine.statistics.structure import StructureStats, structure_stats

METRIC_VERSION = "frequency-v1"
"""统计口径版本；口径变化时必须升版，避免跨口径引用碰撞。"""

DEFAULT_WINDOWS: tuple[WindowSpec, ...] = (30, 100, 300, "all")
"""默认多窗口比较：30/100/300 期与全范围。"""


@dataclass(frozen=True, slots=True)
class Fact:
    """一条可引用、可复现的统计事实。"""

    fact_id: str
    value: int | float
    unit: str
    sample_n: int
    window: WindowSpec
    metric_version: str = METRIC_VERSION
    lower_bound: bool = False

    def to_canonical(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "value": self.value,
            "unit": self.unit,
            "sample_n": self.sample_n,
            "window": self.window,
            "metric_version": self.metric_version,
            "lower_bound": self.lower_bound,
        }


@dataclass(frozen=True, slots=True)
class WindowQuality:
    """单个窗口的样本质量。"""

    window: WindowSpec
    requested: int | None
    sample_n: int
    insufficient: bool
    truncated_left: bool
    note: str

    def to_canonical(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "requested": self.requested,
            "sample_n": self.sample_n,
            "insufficient": self.insufficient,
            "truncated_left": self.truncated_left,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class QualitySummary:
    """整体数据质量摘要：有效样本、截止期与窗口不足说明。"""

    sample_n: int
    as_of_issue: str | None
    as_of_date: str | None
    windows: tuple[WindowQuality, ...]
    notes: tuple[str, ...]

    def to_canonical(self) -> dict[str, Any]:
        return {
            "sample_n": self.sample_n,
            "as_of_issue": self.as_of_issue,
            "as_of_date": self.as_of_date,
            "windows": [quality.to_canonical() for quality in self.windows],
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class AnalysisFacts:
    """一次统计计算的完整结果。"""

    facts: tuple[Fact, ...]
    quality_summary: QualitySummary
    analysis_hash: str
    metric_version: str = METRIC_VERSION

    def find(self, fact_id: str) -> Fact | None:
        """按 ID 取事实；不存在返回 ``None``。"""
        for fact in self.facts:
            if fact.fact_id == fact_id:
                return fact
        return None

    @staticmethod
    def hash_payload(
        facts: Sequence[Fact], metric_version: str = METRIC_VERSION
    ) -> dict[str, Any]:
        """返回 ``analysis_hash`` 的规范化输入（事实 + 指标版本）。

        指标版本参与哈希，避免不同口径下相同数值互相碰撞（附录 B.1）。
        """
        return {
            "facts": [fact.to_canonical() for fact in facts],
            "metric_version": metric_version,
        }

    def to_canonical(self) -> dict[str, Any]:
        return {
            "metric_version": self.metric_version,
            "facts": [fact.to_canonical() for fact in self.facts],
            "quality_summary": self.quality_summary.to_canonical(),
            "analysis_hash": self.analysis_hash,
        }


def _normalize_windows(windows: Iterable[WindowSpec]) -> tuple[WindowSpec, ...]:
    if isinstance(windows, (str, bytes)) or not isinstance(windows, Iterable):
        raise EngineError("windows 必须是窗口序列", code="INVALID_WINDOWS")
    specs: list[WindowSpec] = []
    for window in windows:
        window_requested_periods(window)  # 校验取值
        if window not in specs:
            specs.append(window)
    if not specs:
        raise EngineError("至少需要一个统计窗口", code="EMPTY_WINDOWS")
    return tuple(specs)


def _omission_fact(
    fact_id: str,
    omission: int | None,
    sample_n: int,
    window: WindowSpec,
) -> Fact:
    if omission is None:
        # 窗口内从未出现：只能确认“至少 n 期”。
        return Fact(
            fact_id=fact_id,
            value=sample_n,
            unit="draws",
            sample_n=sample_n,
            window=window,
            lower_bound=True,
        )
    return Fact(
        fact_id=fact_id,
        value=omission,
        unit="draws",
        sample_n=sample_n,
        window=window,
    )


def _number_facts(draws: Sequence[DrawRevision], window: WindowSpec, ball: str) -> list[Fact]:
    label = window_label(window)
    selection = select_window(draws, window)
    sample_n = selection.sample_n
    if sample_n == 0:
        return []

    if ball == "red":
        numbers = RED_NUMBERS
        counts = red_counts(draws, window)
        frequencies = red_frequencies(draws, window)
        omissions = current_omission(draws, window, ball="red")
        maxima = historical_max_omission(draws, window, ball="red")
    else:
        numbers = BLUE_NUMBERS
        counts = blue_counts(draws, window)
        frequencies = blue_frequencies(draws, window)
        omissions = current_omission(draws, window, ball="blue")
        maxima = historical_max_omission(draws, window, ball="blue")

    facts: list[Fact] = []
    for number in numbers:
        prefix = f"ssq.{ball}.{number:02d}"
        facts.append(
            Fact(
                fact_id=f"{prefix}.count.{label}",
                value=counts[number],
                unit="draws",
                sample_n=sample_n,
                window=window,
            )
        )
        facts.append(
            Fact(
                fact_id=f"{prefix}.frequency.{label}",
                value=frequencies[number],
                unit="ratio",
                sample_n=sample_n,
                window=window,
            )
        )
        facts.append(
            _omission_fact(
                f"{prefix}.omission.{label}",
                omissions[number],
                sample_n,
                window,
            )
        )
        maximum = maxima[number]
        facts.append(
            Fact(
                fact_id=f"{prefix}.max_omission.{label}",
                value=maximum.lower_bound,
                unit="draws",
                sample_n=sample_n,
                window=window,
                lower_bound=not maximum.is_exact,
            )
        )
    return facts


def _structure_facts(window: WindowSpec, structure: StructureStats) -> list[Fact]:
    label = window_label(window)
    sample_n = structure.sample_n
    rows: list[tuple[str, int | float | None, str, int]] = [
        (f"ssq.red.sum.mean.{label}", structure.sum_mean, "sum", sample_n),
        (f"ssq.red.sum.min.{label}", structure.sum_min, "sum", sample_n),
        (f"ssq.red.sum.max.{label}", structure.sum_max, "sum", sample_n),
        (f"ssq.red.span.mean.{label}", structure.span_mean, "span", sample_n),
        (f"ssq.red.span.min.{label}", structure.span_min, "span", sample_n),
        (f"ssq.red.span.max.{label}", structure.span_max, "span", sample_n),
        (f"ssq.red.odd.mean.{label}", structure.odd_mean, "balls", sample_n),
        (f"ssq.red.big.mean.{label}", structure.big_mean, "balls", sample_n),
        (f"ssq.red.adjacent_pairs.mean.{label}", structure.adjacent_pairs_mean, "pairs", sample_n),
        (f"ssq.red.longest_run.max.{label}", structure.longest_run_max, "balls", sample_n),
    ]
    if structure.zone_means is not None:
        for zone, mean in enumerate(structure.zone_means, start=1):
            rows.append((f"ssq.red.zone{zone}.mean.{label}", mean, "balls", sample_n))
    if structure.repeat_mean is not None:
        # 重号均值只使用存在前一实际开奖期的期次。
        rows.append(
            (
                f"ssq.red.repeat.mean.{label}",
                structure.repeat_mean,
                "balls",
                structure.repeat_sample_n,
            )
        )

    return [
        Fact(
            fact_id=fact_id,
            value=value,
            unit=unit,
            sample_n=row_sample_n,
            window=window,
        )
        for fact_id, value, unit, row_sample_n in rows
        if value is not None
    ]


def _window_facts(draws: Sequence[DrawRevision], window: WindowSpec) -> list[Fact]:
    structure = structure_stats(draws, window)
    if structure.sample_n == 0:
        return []
    facts = _number_facts(draws, window, "red")
    facts.extend(_number_facts(draws, window, "blue"))
    facts.extend(_structure_facts(window, structure))
    return facts


def _window_quality(selection: WindowSelection) -> WindowQuality:
    requested = selection.requested
    sample_n = selection.sample_n
    if requested is None:
        note = f"全范围{sample_n}期"
    elif selection.insufficient:
        note = f"请求{requested}期，实际{sample_n}期，样本不足按实际计算，未补满"
    else:
        note = f"{sample_n}期"
    if sample_n == 0:
        note += "；无有效样本，未生成事实"
    if selection.truncated_left:
        note += f"；窗口从第{selection.start_index + 1}期开始，左截断区间按下界报告"
    return WindowQuality(
        window=selection.window,
        requested=requested,
        sample_n=sample_n,
        insufficient=selection.insufficient,
        truncated_left=selection.truncated_left,
        note=note,
    )


def _quality_notes(
    draws: Sequence[DrawRevision], qualities: Sequence[WindowQuality]
) -> tuple[str, ...]:
    notes: list[str] = []
    if not draws:
        notes.append("无有效开奖样本，未生成统计事实")
    insufficient = [str(quality.window) for quality in qualities if quality.insufficient]
    if insufficient:
        notes.append(f"窗口 {'、'.join(insufficient)} 样本不足，未补满")
    if any(quality.truncated_left for quality in qualities):
        notes.append("存在左截断窗口，历史最大遗漏首段按下界报告")
    if any(quality.sample_n == 0 for quality in qualities):
        notes.append("部分窗口没有有效样本，对应窗口不生成事实")
    return tuple(notes)


def compute_facts(
    draws: Sequence[DrawRevision],
    windows: Iterable[WindowSpec] = DEFAULT_WINDOWS,
) -> AnalysisFacts:
    """按多个窗口计算事实集与质量摘要。

    Args:
        draws: 双色球开奖记录，按开奖日期升序，每期一个当前修订。
        windows: 窗口序列，取值 ``30``/``100``/``300``/``"all"``；重复值只算一次。
    """
    items = validate_draws(draws)
    specs = _normalize_windows(windows)

    facts: list[Fact] = []
    qualities: list[WindowQuality] = []
    for window in specs:
        selection = select_window(items, window)
        qualities.append(_window_quality(selection))
        facts.extend(_window_facts(items, window))

    quality_summary = QualitySummary(
        sample_n=len(items),
        as_of_issue=items[-1].issue if items else None,
        as_of_date=items[-1].draw_date.isoformat() if items else None,
        windows=tuple(qualities),
        notes=_quality_notes(items, qualities),
    )
    fact_tuple = tuple(facts)
    analysis_hash = sha256_hex(AnalysisFacts.hash_payload(fact_tuple))
    return AnalysisFacts(
        facts=fact_tuple,
        quality_summary=quality_summary,
        analysis_hash=analysis_hash,
        metric_version=METRIC_VERSION,
    )


__all__ = [
    "DEFAULT_WINDOWS",
    "METRIC_VERSION",
    "AnalysisFacts",
    "Fact",
    "QualitySummary",
    "WindowQuality",
    "compute_facts",
]
