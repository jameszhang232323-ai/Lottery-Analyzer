"""奖级规则解析与结算纯函数测试（设计文档 9.4、10.3）。

覆盖：
- 常规六奖级的全部分支与最高奖级优先；
- 2026013 / 2026014 规则边界，旧期号不得出现福运奖；
- 特别规定启用时 3 红 0 蓝为福运奖，3 红 1 蓝仍为五等奖；
- 金额缺失时 ``amount_fen=None`` 且 ``status="unknown"``，不猜金额；
- 规则内容哈希可复算、区间重叠与缺失版本被拒绝。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from dajidali.domain.draw import SsqNumbers
from dajidali.engine import EngineError
from dajidali.engine.rule_resolver import (
    PRIZE_CODE_FIFTH,
    PRIZE_CODE_FIRST,
    PRIZE_CODE_FOURTH,
    PRIZE_CODE_FUYUN,
    PRIZE_CODE_SECOND,
    PRIZE_CODE_SIXTH,
    PRIZE_CODE_THIRD,
    RULE_NEW_FROM_ISSUE,
    PrizeRule,
    RuleError,
    SpecialPrize,
    default_rule_contents,
    default_rule_versions,
    match_prize,
    resolve_rule,
    rule_from_content,
    validate_rule_intervals,
)
from dajidali.engine.settlement import (
    LINE_STATUS_AWARDED,
    LINE_STATUS_NO_PRIZE,
    LINE_STATUS_UNKNOWN,
    LineResult,
    settle_lines,
    summarize_results,
)

DRAW = SsqNumbers.create([1, 2, 3, 4, 5, 6], 9)
"""固定开奖号码：红 1—6、蓝 9。"""

NEW_RULE = default_rule_versions()[1]
OLD_RULE = default_rule_versions()[0]


def line(reds: list[int], blue: int) -> SsqNumbers:
    return SsqNumbers.create(reds, blue)


def new_rule_with_special(special: SpecialPrize) -> PrizeRule:
    return replace(NEW_RULE, special_prizes=(special,), prize_levels=NEW_RULE.prize_levels)


def test_default_rule_versions_boundary_and_content() -> None:
    old_rule, new_rule = default_rule_versions()

    assert old_rule.game == new_rule.game == "ssq"
    assert old_rule.effective_from_issue == "2003001"
    assert old_rule.effective_to_issue == "2026013"
    assert new_rule.effective_from_issue == RULE_NEW_FROM_ISSUE == "2026014"
    assert new_rule.effective_to_issue is None

    assert resolve_rule([old_rule, new_rule], "ssq", "2026013") is old_rule
    assert resolve_rule([old_rule, new_rule], "ssq", "2026014") is new_rule
    assert resolve_rule([old_rule, new_rule], "ssq", "2026001") is old_rule
    assert resolve_rule([old_rule, new_rule], "ssq", "2030010") is new_rule

    # 旧规则没有任何特别奖；新规则只有启用的福运奖（3 红 0 蓝）。
    assert old_rule.special_prizes == ()
    assert [special.code for special in new_rule.special_prizes] == [PRIZE_CODE_FUYUN]
    special = new_rule.special_prizes[0]
    assert (special.hits_red, special.hits_blue, special.enabled) == (3, 0, True)

    # 规范化内容可逐字还原，内容哈希可由内容独立复算。
    for rule in (old_rule, new_rule):
        content = rule.to_canonical()
        restored = rule_from_content(content)
        assert restored == rule
        assert restored.content_hash() == rule.content_hash()
    old_content, new_content = default_rule_contents()
    assert old_content["special_prizes"] == []
    assert new_content["special_prizes"][0]["code"] == PRIZE_CODE_FUYUN


def test_rule_interval_overlap_is_rejected() -> None:
    extended_old = replace(OLD_RULE, effective_to_issue=None)
    with pytest.raises(RuleError) as overlap:
        validate_rule_intervals([extended_old, NEW_RULE])
    assert overlap.value.code == "RULE_INTERVAL_OVERLAP"

    # 即使调用方不先校验，解析到多个版本时也必须拒绝而不是任选一个。
    with pytest.raises(RuleError) as ambiguous:
        resolve_rule([extended_old, NEW_RULE], "ssq", "2026014")
    assert ambiguous.value.code == "RULE_INTERVAL_OVERLAP"

    # 同一版本起点重复也算重叠。
    with pytest.raises(RuleError) as duplicate:
        validate_rule_intervals([OLD_RULE, replace(OLD_RULE, effective_to_issue="2026013")])
    assert duplicate.value.code == "RULE_INTERVAL_OVERLAP"


def test_missing_rule_and_invalid_content_are_rejected() -> None:
    with pytest.raises(RuleError) as missing:
        resolve_rule([OLD_RULE, NEW_RULE], "ssq", "1999001")
    assert missing.value.code == "RULE_NOT_FOUND"

    with pytest.raises(RuleError) as wrong_game:
        resolve_rule([OLD_RULE, NEW_RULE], "fc3d", "2026014")
    assert wrong_game.value.code == "RULE_NOT_FOUND"

    content = NEW_RULE.to_canonical()
    content["effective_to_issue"] = "1999010"
    with pytest.raises(RuleError) as bad_interval:
        rule_from_content(content)
    assert bad_interval.value.code == "INVALID_RULE_INTERVAL"

    broken = NEW_RULE.to_canonical()
    broken["prize_levels"][0]["amount_type"] = "guess"
    with pytest.raises(RuleError) as bad_amount_type:
        rule_from_content(broken)
    assert bad_amount_type.value.code == "INVALID_RULE_CONTENT"

    missing_field = NEW_RULE.to_canonical()
    del missing_field["special_prizes"]
    with pytest.raises(RuleError) as bad_fields:
        rule_from_content(missing_field)
    assert bad_fields.value.code == "INVALID_RULE_CONTENT"


@pytest.mark.parametrize(
    ("hits_red", "hits_blue", "level", "code"),
    [
        (6, 1, 1, PRIZE_CODE_FIRST),
        (6, 0, 2, PRIZE_CODE_SECOND),
        (5, 1, 3, PRIZE_CODE_THIRD),
        (5, 0, 4, PRIZE_CODE_FOURTH),
        (4, 1, 4, PRIZE_CODE_FOURTH),
        (4, 0, 5, PRIZE_CODE_FIFTH),
        (3, 1, 5, PRIZE_CODE_FIFTH),
        (2, 1, 6, PRIZE_CODE_SIXTH),
        (1, 1, 6, PRIZE_CODE_SIXTH),
        (0, 1, 6, PRIZE_CODE_SIXTH),
    ],
)
def test_all_regular_prize_branches(
    hits_red: int, hits_blue: int, level: int, code: str
) -> None:
    """旧规则（无福运奖）下逐个验证六奖级分支。"""
    match = match_prize(hits_red, hits_blue, OLD_RULE)
    assert match is not None
    assert (match.level, match.code) == (level, code)


@pytest.mark.parametrize(
    ("hits_red", "hits_blue"),
    [(3, 0), (2, 0), (1, 0), (0, 0)],
)
def test_no_prize_combinations(hits_red: int, hits_blue: int) -> None:
    assert match_prize(hits_red, hits_blue, OLD_RULE) is None


def test_fuyun_only_exists_from_2026014() -> None:
    # 旧规则期号：3 红 0 蓝无奖，不出现福运奖。
    assert match_prize(3, 0, OLD_RULE) is None
    # 新规则期号：3 红 0 蓝为福运奖。
    match = match_prize(3, 0, NEW_RULE)
    assert match is not None
    assert (match.level, match.code) == (7, PRIZE_CODE_FUYUN)
    # 3 红 1 蓝命中五等奖，不是福运奖（最高奖级优先）。
    blue_match = match_prize(3, 1, NEW_RULE)
    assert blue_match is not None
    assert (blue_match.level, blue_match.code) == (5, PRIZE_CODE_FIFTH)


def test_special_prize_respects_enabled_flag() -> None:
    disabled = replace(NEW_RULE.special_prizes[0], enabled=False)
    rule = new_rule_with_special(disabled)
    assert match_prize(3, 0, rule) is None
    # 关闭特别规定不影响常规奖级。
    assert match_prize(3, 1, rule) is not None
    assert match_prize(3, 1, rule).code == PRIZE_CODE_FIFTH  # type: ignore[union-attr]


def test_highest_prize_level_wins_when_special_overlaps() -> None:
    overlapping = SpecialPrize(
        code=PRIZE_CODE_FUYUN,
        name="福运奖",
        rank=7,
        enabled=True,
        hits_red=6,
        hits_blue=1,
    )
    rule = new_rule_with_special(overlapping)
    match = match_prize(6, 1, rule)
    assert match is not None
    assert (match.level, match.code) == (1, PRIZE_CODE_FIRST)


def test_settle_lines_hits_and_amount_status() -> None:
    lines = [
        line([1, 2, 3, 4, 5, 6], 9),  # 6+1 一等奖
        line([1, 2, 3, 30, 31, 32], 9),  # 3+1 五等奖
        line([1, 2, 3, 30, 31, 32], 10),  # 3+0 福运奖
        line([20, 21, 22, 23, 24, 25], 10),  # 未中奖
    ]
    prizes = {PRIZE_CODE_FIFTH: 12_345}

    results = settle_lines(lines, DRAW, NEW_RULE, prizes)

    assert [result.to_canonical() for result in results] == [
        {
            "hits_red": 6,
            "hits_blue": 1,
            "prize_level": 1,
            "prize_code": PRIZE_CODE_FIRST,
            "amount_fen": None,
            "status": LINE_STATUS_UNKNOWN,
        },
        {
            "hits_red": 3,
            "hits_blue": 1,
            "prize_level": 5,
            "prize_code": PRIZE_CODE_FIFTH,
            "amount_fen": 12_345,
            "status": LINE_STATUS_AWARDED,
        },
        {
            "hits_red": 3,
            "hits_blue": 0,
            "prize_level": 7,
            "prize_code": PRIZE_CODE_FUYUN,
            "amount_fen": None,
            "status": LINE_STATUS_UNKNOWN,
        },
        {
            "hits_red": 0,
            "hits_blue": 0,
            "prize_level": None,
            "prize_code": None,
            "amount_fen": 0,
            "status": LINE_STATUS_NO_PRIZE,
        },
    ]
    # 同一组号码在旧规则期号下不产生福运奖。
    old_results = settle_lines([line([1, 2, 3, 30, 31, 32], 10)], DRAW, OLD_RULE, {})
    assert old_results[0].prize_code is None
    assert old_results[0].status == LINE_STATUS_NO_PRIZE


def test_amount_unknown_when_prize_row_missing_or_null() -> None:
    fuyun_line = [line([1, 2, 3, 30, 31, 32], 10)]

    missing = settle_lines(fuyun_line, DRAW, NEW_RULE, None)
    assert missing[0].amount_fen is None
    assert missing[0].status == LINE_STATUS_UNKNOWN

    explicit_null = settle_lines(fuyun_line, DRAW, NEW_RULE, {PRIZE_CODE_FUYUN: None})
    assert explicit_null[0].amount_fen is None
    assert explicit_null[0].status == LINE_STATUS_UNKNOWN

    known = settle_lines(fuyun_line, DRAW, NEW_RULE, {PRIZE_CODE_FUYUN: 500})
    assert known[0].amount_fen == 500
    assert known[0].status == LINE_STATUS_AWARDED


def test_settle_lines_rejects_bad_input_and_negative_amount() -> None:
    with pytest.raises(EngineError) as bad_line:
        settle_lines(["not-a-line"], DRAW, NEW_RULE)  # type: ignore[list-item]
    assert bad_line.value.code == "NUMBER_TYPE_MISMATCH"

    with pytest.raises(EngineError) as bad_amount:
        settle_lines(
            [line([1, 2, 3, 30, 31, 32], 10)],
            DRAW,
            NEW_RULE,
            {PRIZE_CODE_FUYUN: -1},
        )
    assert bad_amount.value.code == "INVALID_PRIZE_AMOUNT"

    with pytest.raises(EngineError) as bad_rule:
        settle_lines([line([1, 2, 3, 30, 31, 32], 10)], DRAW, "rule")  # type: ignore[arg-type]
    assert bad_rule.value.code == "INVALID_RULE"


def test_summarize_results_counts_and_known_return() -> None:
    results = [
        LineResult(6, 1, 1, PRIZE_CODE_FIRST, None, LINE_STATUS_UNKNOWN),
        LineResult(5, 1, 3, PRIZE_CODE_THIRD, 300_000, LINE_STATUS_AWARDED),
        LineResult(3, 0, 7, PRIZE_CODE_FUYUN, None, LINE_STATUS_UNKNOWN),
        LineResult(0, 0, None, None, 0, LINE_STATUS_NO_PRIZE),
    ]
    summary = summarize_results(results)

    assert summary["total_lines"] == 4
    assert summary["winning_lines"] == 3
    assert summary["known_amount_lines"] == 1
    assert summary["unknown_amount_lines"] == 2
    assert summary["known_return_fen"] == 300_000
    assert summary["return_complete"] is False
    assert summary["hit_distribution"] == [
        {"hits_red": 0, "hits_blue": 0, "count": 1},
        {"hits_red": 3, "hits_blue": 0, "count": 1},
        {"hits_red": 5, "hits_blue": 1, "count": 1},
        {"hits_red": 6, "hits_blue": 1, "count": 1},
    ]
    assert summary["prize_distribution"] == [
        {"prize_level": 1, "prize_code": PRIZE_CODE_FIRST, "count": 1, "known_return_fen": 0},
        {"prize_level": 3, "prize_code": PRIZE_CODE_THIRD, "count": 1, "known_return_fen": 300_000},
        {"prize_level": 7, "prize_code": PRIZE_CODE_FUYUN, "count": 1, "known_return_fen": 0},
    ]

    complete = summarize_results(
        [LineResult(5, 1, 3, PRIZE_CODE_THIRD, 300_000, LINE_STATUS_AWARDED)]
    )
    assert complete["return_complete"] is True
    assert complete["known_return_fen"] == 300_000
