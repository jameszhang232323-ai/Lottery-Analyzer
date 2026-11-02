"""统计引擎公开入口（设计文档 7.1）。

子模块职责：

- :mod:`frequency`：窗口选择、红/蓝球出现次数与期频率、截断元信息；
- :mod:`omission`：当前遗漏与历史最大遗漏（完整最大值与截断下界分开）；
- :mod:`structure`：和值、跨度、奇偶、大小、三区、连号与重号；
- :mod:`analysis`：事实集、质量摘要与 ``analysis_hash``。
"""

from __future__ import annotations

from dajidali.engine.statistics.analysis import (
    DEFAULT_WINDOWS,
    METRIC_VERSION,
    AnalysisFacts,
    Fact,
    QualitySummary,
    WindowQuality,
    compute_facts,
)
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
)
from dajidali.engine.statistics.omission import (
    MaxOmission,
    current_omission,
    historical_max_omission,
    omission_summary,
)
from dajidali.engine.statistics.structure import (
    StructurePeriod,
    StructureStats,
    red_adjacent_pairs,
    red_longest_run,
    red_odd_count,
    red_zone_counts,
    structure_stats,
)

__all__ = [
    "BLUE_NUMBERS",
    "DEFAULT_WINDOWS",
    "METRIC_VERSION",
    "RED_NUMBERS",
    "AnalysisFacts",
    "Fact",
    "MaxOmission",
    "QualitySummary",
    "StructurePeriod",
    "StructureStats",
    "WindowQuality",
    "WindowSelection",
    "WindowSpec",
    "blue_counts",
    "blue_frequencies",
    "compute_facts",
    "current_omission",
    "historical_max_omission",
    "omission_summary",
    "red_adjacent_pairs",
    "red_counts",
    "red_frequencies",
    "red_longest_run",
    "red_odd_count",
    "red_zone_counts",
    "select_window",
    "structure_stats",
    "validate_draws",
    "window_label",
]
