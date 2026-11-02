"""开奖号码与开奖记录的领域模型。

规则来源（设计文档 6.2、7.1、10.3）：

- 双色球：红球 6 个互异整数，范围 1—33；蓝球 1 个整数，范围 1—16。
  红球按升序保存，仅表示集合，不代表摇出顺序；真实摇出顺序是独立可空字段。
- 福彩 3D：三位数字字符串，允许重复、保留前导零（``"007"`` 仍是 ``"007"``），
  不能复用红球的去重与排序逻辑。首版仅预留类型，产品 V1.1 启用。

期号保存为字符串（``"2026014"``），排序使用规范化的年份与序号，不直接做整数比较。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from dajidali.domain.errors import InvalidNumbersError

SSQ_RED_COUNT = 6
SSQ_RED_MIN = 1
SSQ_RED_MAX = 33
SSQ_BLUE_MIN = 1
SSQ_BLUE_MAX = 16

FC3D_DIGITS = 3


@dataclass(frozen=True, slots=True)
class SsqNumbers:
    """一注双色球号码：6 个升序红球 + 1 个蓝球。"""

    reds: tuple[int, ...]
    blue: int

    def __post_init__(self) -> None:
        if len(self.reds) != SSQ_RED_COUNT:
            raise InvalidNumbersError(
                f"红球必须为 {SSQ_RED_COUNT} 个",
                code="INVALID_RED_COUNT",
                expected=SSQ_RED_COUNT,
                actual=len(self.reds),
            )
        if len(set(self.reds)) != SSQ_RED_COUNT:
            raise InvalidNumbersError(
                "红球不得重复",
                code="DUPLICATE_RED_BALLS",
                reds=list(self.reds),
            )
        if any(not isinstance(n, int) or isinstance(n, bool) for n in self.reds):
            raise InvalidNumbersError("红球必须为整数", code="INVALID_RED_TYPE")
        if any(n < SSQ_RED_MIN or n > SSQ_RED_MAX for n in self.reds):
            raise InvalidNumbersError(
                f"红球范围必须为 {SSQ_RED_MIN}—{SSQ_RED_MAX}",
                code="RED_BALL_OUT_OF_RANGE",
                reds=list(self.reds),
            )
        if tuple(sorted(self.reds)) != self.reds:
            raise InvalidNumbersError(
                "红球必须按升序保存",
                code="RED_BALLS_NOT_SORTED",
                reds=list(self.reds),
            )
        if isinstance(self.blue, bool) or not isinstance(self.blue, int):
            raise InvalidNumbersError("蓝球必须为整数", code="INVALID_BLUE_TYPE")
        if self.blue < SSQ_BLUE_MIN or self.blue > SSQ_BLUE_MAX:
            raise InvalidNumbersError(
                f"蓝球范围必须为 {SSQ_BLUE_MIN}—{SSQ_BLUE_MAX}",
                code="BLUE_BALL_OUT_OF_RANGE",
                blue=self.blue,
            )

    @classmethod
    def create(cls, reds: Iterable[int], blue: int) -> SsqNumbers:
        """从任意顺序的红球构造；保存时统一升序。"""
        return cls(reds=tuple(sorted(reds)), blue=blue)

    def as_text(self) -> str:
        """可读形式：``"02 07 15 18 25 33 + 09"``。"""
        return " ".join(f"{n:02d}" for n in self.reds) + f" + {self.blue:02d}"


@dataclass(frozen=True, slots=True)
class Fc3dNumbers:
    """福彩 3D 号码：固定三位字符串，允许重复与前导零。"""

    digits: str

    def __post_init__(self) -> None:
        if not isinstance(self.digits, str) or len(self.digits) != FC3D_DIGITS:
            raise InvalidNumbersError(
                f"3D 号码必须为 {FC3D_DIGITS} 位字符串",
                code="INVALID_FC3D_LENGTH",
                value=self.digits,
            )
        if not self.digits.isdigit() or not self.digits.isascii():
            raise InvalidNumbersError(
                "3D 号码只能由数字 0—9 组成",
                code="INVALID_FC3D_DIGITS",
                value=self.digits,
            )


@dataclass(frozen=True, slots=True)
class DrawRevision:
    """一期开奖的某一次修订（不可变）。

    修订号单调递增，同一 ``(game, issue, revision)`` 唯一；更正只新增修订，
    不覆盖旧修订（设计文档 5.5）。
    """

    game: str
    issue: str
    revision: int
    draw_date: date
    numbers: SsqNumbers | Fc3dNumbers
    is_current: bool = True

    def __post_init__(self) -> None:
        if not self.game:
            raise InvalidNumbersError("彩种不能为空", code="MISSING_GAME")
        if not self.issue or not self.issue.isdigit():
            raise InvalidNumbersError(
                "期号必须为非空数字字符串",
                code="INVALID_ISSUE",
                issue=self.issue,
            )
        if self.revision < 1:
            raise InvalidNumbersError(
                "修订号从 1 开始",
                code="INVALID_REVISION",
                revision=self.revision,
            )
        expected = SsqNumbers if self.game == "ssq" else Fc3dNumbers
        if not isinstance(self.numbers, expected):
            raise InvalidNumbersError(
                f"{self.game} 的号码类型应为 {expected.__name__}",
                code="NUMBER_TYPE_MISMATCH",
                game=self.game,
            )


def sort_issues(issues: Sequence[str]) -> list[str]:
    """按年份与序号排序期号字符串，而不是按字典序或整数比较。

    ``"2026001"`` 与 ``"2025140"`` 这类跨年相邻期号必须落在正确位置。
    """
    def key(issue: str) -> tuple[str, str]:
        if not issue.isdigit():
            raise InvalidNumbersError(
                "期号必须为数字字符串",
                code="INVALID_ISSUE",
                issue=issue,
            )
        # 年份为前四位，其余为当年序号；位数不足时整体视为序号。
        return (issue[:4], issue[4:].rjust(3, "0"))

    return sorted(issues, key=key)
