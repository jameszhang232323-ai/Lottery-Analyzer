"""领域对象单元测试：号码合法性、期号排序与不可变性。

对应实施计划任务 01 的验收项：

- 红球 ``[1, 1, 3, 4, 5, 6]`` 必须被拒绝；
- 3D 的 ``007`` 序列化后仍然是 ``007``；
- 跨年期号排序按年份与序号，而不是字典序。
"""

from __future__ import annotations

import datetime as dt

import pytest

from dajidali.domain.draw import (
    DrawRevision,
    Fc3dNumbers,
    SsqNumbers,
    sort_issues,
)
from dajidali.domain.errors import InvalidNumbersError


def test_ssq_create_sorts_reds() -> None:
    numbers = SsqNumbers.create([33, 1, 15, 7, 25, 18], 9)
    assert numbers.reds == (1, 7, 15, 18, 25, 33)
    assert numbers.blue == 9
    assert numbers.as_text() == "01 07 15 18 25 33 + 09"


def test_ssq_rejects_duplicate_reds() -> None:
    with pytest.raises(InvalidNumbersError) as excinfo:
        SsqNumbers.create([1, 1, 3, 4, 5, 6], 7)
    assert excinfo.value.code == "DUPLICATE_RED_BALLS"


@pytest.mark.parametrize(
    ("reds", "code"),
    [
        ([0, 2, 3, 4, 5, 6], "RED_BALL_OUT_OF_RANGE"),
        ([1, 2, 3, 4, 5, 34], "RED_BALL_OUT_OF_RANGE"),
        ([1, 2, 3, 4, 5], "INVALID_RED_COUNT"),
        ([1, 2, 3, 4, 5, 6, 7], "INVALID_RED_COUNT"),
    ],
)
def test_ssq_rejects_illegal_reds(reds: list[int], code: str) -> None:
    with pytest.raises(InvalidNumbersError) as excinfo:
        SsqNumbers.create(reds, 7)
    assert excinfo.value.code == code


@pytest.mark.parametrize("blue", [0, 17, -1])
def test_ssq_rejects_illegal_blue(blue: int) -> None:
    with pytest.raises(InvalidNumbersError) as excinfo:
        SsqNumbers.create([1, 2, 3, 4, 5, 6], blue)
    assert excinfo.value.code == "BLUE_BALL_OUT_OF_RANGE"


def test_ssq_requires_sorted_storage() -> None:
    with pytest.raises(InvalidNumbersError) as excinfo:
        SsqNumbers(reds=(3, 1, 2, 4, 5, 6), blue=7)
    assert excinfo.value.code == "RED_BALLS_NOT_SORTED"


def test_fc3d_keeps_leading_zeros() -> None:
    numbers = Fc3dNumbers("007")
    assert numbers.digits == "007"
    assert numbers.digits != "7"


@pytest.mark.parametrize("digits", ["7", "0077", "00a", " 07"])
def test_fc3d_rejects_illegal_values(digits: str) -> None:
    with pytest.raises(InvalidNumbersError):
        Fc3dNumbers(digits)


def test_issue_sorting_is_cross_year() -> None:
    issues = ["2026001", "2025140", "2025151", "2026010", "2025143"]
    assert sort_issues(issues) == ["2025140", "2025143", "2025151", "2026001", "2026010"]


def test_draw_revision_rejects_number_type_mismatch() -> None:
    revision = DrawRevision(
        game="ssq",
        issue="2026001",
        revision=1,
        draw_date=dt.date(2026, 1, 1),
        numbers=SsqNumbers.create([1, 2, 3, 4, 5, 6], 7),
    )
    assert revision.revision == 1

    with pytest.raises(InvalidNumbersError) as excinfo:
        DrawRevision(
            game="ssq",
            issue="2026001",
            revision=1,
            draw_date=dt.date(2026, 1, 1),
            numbers=Fc3dNumbers("007"),
        )
    assert excinfo.value.code == "NUMBER_TYPE_MISMATCH"


def test_draw_revision_is_frozen() -> None:
    revision = DrawRevision(
        game="fc3d",
        issue="2026001",
        revision=1,
        draw_date=dt.date(2026, 1, 1),
        numbers=Fc3dNumbers("007"),
    )
    with pytest.raises(AttributeError):
        revision.revision = 2  # type: ignore[misc]
