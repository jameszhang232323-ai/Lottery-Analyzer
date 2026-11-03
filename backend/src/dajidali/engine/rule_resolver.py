"""双色球奖级规则版本与解析（设计文档 9.4、10.3）。

规则版本是**按期号区间生效的不可变内容**：同一彩种的期号区间不得重叠，
解析时只取覆盖该期的那一个版本。新规则自第 ``2026014`` 期起执行，并在
特别规定期间增设「福运奖」；``2026014`` 之前的期号解析到旧版本，因而不会
出现福运奖。

本模块是纯函数层：只依赖标准库与 :mod:`dajidali.domain`，不读数据库、不联网。
数据库行（``rule_version``）由仓储解析为 :class:`PrizeRule` 后传入。

奖级匹配顺序按 ``rank`` 取最高适用奖级（1 最高）：规则内容里每个奖级声明
一组 ``(hits_red, hits_blue)`` 组合，特别奖另带启用开关。金额不在规则里，
由当期 ``draw_prize`` 提供；缺失时只输出奖级与命中明细，绝不猜金额。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from dajidali.domain.canonical import sha256_hex
from dajidali.domain.errors import DomainError
from dajidali.engine import EngineError

PRIZE_RULE_SCHEMA = "prize-rule-v1"

SSQ_GAME = "ssq"
SSQ_RED_COUNT = 6
SSQ_BLUE_MAX = 1

PRIZE_CODE_FIRST = "FIRST"
PRIZE_CODE_SECOND = "SECOND"
PRIZE_CODE_THIRD = "THIRD"
PRIZE_CODE_FOURTH = "FOURTH"
PRIZE_CODE_FIFTH = "FIFTH"
PRIZE_CODE_SIXTH = "SIXTH"
PRIZE_CODE_FUYUN = "FUYUN"

AMOUNT_TYPES: tuple[str, ...] = ("fixed", "floating")

# 新规则（自 2026014 期起）与现行规则的官方依据；旧规则沿用六奖级结构，
# 未取得可引用的独立公告，证据 URL 留空而不是伪造。
RULE_EVIDENCE_URL_NEW = "https://www.szlottery.org/fcw/fcxw/tzgg/content/post_1649773.html"
RULE_EVIDENCE_URL_REFERENCE = "https://www.scflcp.com.cn/yxgz/823145252.jhtml"

RULE_OLD_FROM_ISSUE = "2003001"
RULE_OLD_TO_ISSUE = "2026013"
RULE_NEW_FROM_ISSUE = "2026014"


class RuleError(DomainError):
    """规则版本缺失、区间重叠或内容不合法（数据/状态冲突，映射 409）。"""

    code = "RULE_ERROR"
    http_status = 409


def issue_key(issue: str) -> tuple[str, str]:
    """期号排序键：前四位为年份，其余为当年序号。

    与 :func:`dajidali.domain.draw.sort_issues` 和开奖仓储保持同一规则，避免
    跨年期号按字符串或整数误排。
    """
    if not isinstance(issue, str) or not issue or not issue.isdigit():
        raise EngineError(
            "期号必须为非空数字字符串",
            code="INVALID_ISSUE",
            issue=issue,
        )
    return (issue[:4], issue[4:].rjust(3, "0"))


@dataclass(frozen=True, slots=True)
class PrizeLevel:
    """常规奖级：名次、代码与命中组合。"""

    code: str
    name: str
    rank: int
    amount_type: str
    matches: tuple[tuple[int, int], ...]

    def to_canonical(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "rank": self.rank,
            "amount_type": self.amount_type,
            "match": [
                {"hits_red": hits_red, "hits_blue": hits_blue}
                for hits_red, hits_blue in self.matches
            ],
        }


@dataclass(frozen=True, slots=True)
class SpecialPrize:
    """特别规定奖级（如福运奖）：可启停，命中条件独立于常规奖级。"""

    code: str
    name: str
    rank: int
    enabled: bool
    hits_red: int
    hits_blue: int

    def to_canonical(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "rank": self.rank,
            "enabled": self.enabled,
            "condition": {"hits_red": self.hits_red, "hits_blue": self.hits_blue},
        }

    def matches(self, hits_red: int, hits_blue: int) -> bool:
        return (
            self.enabled
            and self.hits_red == hits_red
            and self.hits_blue == hits_blue
        )


@dataclass(frozen=True, slots=True)
class PrizeRule:
    """一个按期号区间生效的完整奖级规则版本。"""

    game: str
    effective_from_issue: str
    effective_to_issue: str | None
    prize_levels: tuple[PrizeLevel, ...]
    special_prizes: tuple[SpecialPrize, ...] = ()
    schema: str = PRIZE_RULE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PRIZE_RULE_SCHEMA:
            raise RuleError(
                "不支持的规则内容架构",
                code="INVALID_RULE_CONTENT",
                schema=self.schema,
            )
        if not self.game:
            raise RuleError("规则必须声明彩种", code="INVALID_RULE_CONTENT")
        issue_key(self.effective_from_issue)
        if self.effective_to_issue is not None:
            issue_key(self.effective_to_issue)
            if issue_key(self.effective_to_issue) < issue_key(self.effective_from_issue):
                raise RuleError(
                    "规则结束期号不能早于起始期号",
                    code="INVALID_RULE_INTERVAL",
                    game=self.game,
                    effective_from_issue=self.effective_from_issue,
                    effective_to_issue=self.effective_to_issue,
                )
        if not self.prize_levels:
            raise RuleError("规则至少需要一个奖级", code="INVALID_RULE_CONTENT")
        seen_ranks: set[int] = set()
        seen_codes: set[str] = set()
        for level in self.prize_levels:
            _validate_rank_and_code(level.rank, level.code, seen_ranks, seen_codes)
            if level.amount_type not in AMOUNT_TYPES:
                raise RuleError(
                    "未知的奖金类型",
                    code="INVALID_RULE_CONTENT",
                    code_name=level.code,
                    amount_type=level.amount_type,
                )
            if not level.matches:
                raise RuleError(
                    "奖级必须声明命中组合",
                    code="INVALID_RULE_CONTENT",
                    code_name=level.code,
                )
            for hits_red, hits_blue in level.matches:
                _validate_hits(hits_red, hits_blue, prize_code=level.code)
        for special in self.special_prizes:
            _validate_rank_and_code(special.rank, special.code, seen_ranks, seen_codes)
            _validate_hits(special.hits_red, special.hits_blue, prize_code=special.code)

    def covers_issue(self, issue: str) -> bool:
        """期号是否落在本规则版本的有效区间内（闭区间）。"""
        key = issue_key(issue)
        if key < issue_key(self.effective_from_issue):
            return False
        if self.effective_to_issue is None:
            return True
        return key <= issue_key(self.effective_to_issue)

    def to_canonical(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "game": self.game,
            "effective_from_issue": self.effective_from_issue,
            "effective_to_issue": self.effective_to_issue,
            "prize_levels": [level.to_canonical() for level in self.prize_levels],
            "special_prizes": [special.to_canonical() for special in self.special_prizes],
        }

    def content_hash(self) -> str:
        return sha256_hex(self.to_canonical())


@dataclass(frozen=True, slots=True)
class PrizeMatch:
    """一次命中对应的奖级；``level`` 与 ``rank`` 相同，1 为最高奖级。"""

    level: int
    code: str
    name: str
    rank: int


def _validate_rank_and_code(
    rank: int,
    code: str,
    seen_ranks: set[int],
    seen_codes: set[str],
) -> None:
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        raise RuleError(
            "奖级名次必须为正整数",
            code="INVALID_RULE_CONTENT",
            rank=rank,
        )
    if not code:
        raise RuleError("奖级代码不能为空", code="INVALID_RULE_CONTENT")
    if rank in seen_ranks or code in seen_codes:
        raise RuleError(
            "奖级名次或代码重复",
            code="INVALID_RULE_CONTENT",
            rank=rank,
            code_name=code,
        )
    seen_ranks.add(rank)
    seen_codes.add(code)


def _validate_hits(hits_red: int, hits_blue: int, *, prize_code: str) -> None:
    if (
        isinstance(hits_red, bool)
        or not isinstance(hits_red, int)
        or not 0 <= hits_red <= SSQ_RED_COUNT
    ):
        raise RuleError(
            "红球命中数必须在 0—6",
            code="INVALID_RULE_CONTENT",
            prize_code=prize_code,
            hits_red=hits_red,
        )
    if (
        isinstance(hits_blue, bool)
        or not isinstance(hits_blue, int)
        or not 0 <= hits_blue <= SSQ_BLUE_MAX
    ):
        raise RuleError(
            "蓝球命中数必须在 0—1",
            code="INVALID_RULE_CONTENT",
            prize_code=prize_code,
            hits_blue=hits_blue,
        )


def _default_prize_levels() -> list[dict[str, Any]]:
    """六奖级的常规命中组合（新旧规则相同），返回规范化内容格式。"""
    return [
        {
            "code": PRIZE_CODE_FIRST,
            "name": "一等奖",
            "rank": 1,
            "amount_type": "floating",
            "match": [{"hits_red": 6, "hits_blue": 1}],
        },
        {
            "code": PRIZE_CODE_SECOND,
            "name": "二等奖",
            "rank": 2,
            "amount_type": "floating",
            "match": [{"hits_red": 6, "hits_blue": 0}],
        },
        {
            "code": PRIZE_CODE_THIRD,
            "name": "三等奖",
            "rank": 3,
            "amount_type": "fixed",
            "match": [{"hits_red": 5, "hits_blue": 1}],
        },
        {
            "code": PRIZE_CODE_FOURTH,
            "name": "四等奖",
            "rank": 4,
            "amount_type": "fixed",
            "match": [
                {"hits_red": 5, "hits_blue": 0},
                {"hits_red": 4, "hits_blue": 1},
            ],
        },
        {
            "code": PRIZE_CODE_FIFTH,
            "name": "五等奖",
            "rank": 5,
            "amount_type": "fixed",
            "match": [
                {"hits_red": 4, "hits_blue": 0},
                {"hits_red": 3, "hits_blue": 1},
            ],
        },
        {
            "code": PRIZE_CODE_SIXTH,
            "name": "六等奖",
            "rank": 6,
            "amount_type": "fixed",
            "match": [
                {"hits_red": 2, "hits_blue": 1},
                {"hits_red": 1, "hits_blue": 1},
                {"hits_red": 0, "hits_blue": 1},
            ],
        },
    ]


def default_rule_contents() -> tuple[dict[str, Any], dict[str, Any]]:
    """返回旧/新两个规则版本的规范化内容（迁移种子与测试共用）。

    - 旧版本：``2003001``—``2026013``，只有六奖级，不含福运奖；
    - 新版本：自 ``2026014`` 起，六奖级 + 特别规定福运奖（3 红 0 蓝）。
    """
    old: dict[str, Any] = {
        "schema": PRIZE_RULE_SCHEMA,
        "game": SSQ_GAME,
        "effective_from_issue": RULE_OLD_FROM_ISSUE,
        "effective_to_issue": RULE_OLD_TO_ISSUE,
        "prize_levels": _default_prize_levels(),
        "special_prizes": [],
    }
    new: dict[str, Any] = {
        "schema": PRIZE_RULE_SCHEMA,
        "game": SSQ_GAME,
        "effective_from_issue": RULE_NEW_FROM_ISSUE,
        "effective_to_issue": None,
        "prize_levels": _default_prize_levels(),
        "special_prizes": [
            {
                "code": PRIZE_CODE_FUYUN,
                "name": "福运奖",
                "rank": 7,
                "enabled": True,
                "condition": {"hits_red": 3, "hits_blue": 0},
            }
        ],
    }
    return old, new


def default_rule_versions() -> tuple[PrizeRule, PrizeRule]:
    """返回内置的旧/新规则版本对象（按生效期号升序）。"""
    old_content, new_content = default_rule_contents()
    return rule_from_content(old_content), rule_from_content(new_content)


def rule_from_content(content: Mapping[str, Any]) -> PrizeRule:
    """把 ``rule_version.content_json`` 解析为 :class:`PrizeRule`。

    严格校验字段集合与取值，保证 ``to_canonical()`` 能逐字还原被哈希的内容；
    因此内容哈希可由数据库行独立复核。
    """
    if not isinstance(content, Mapping):
        raise RuleError("规则内容必须是对象", code="INVALID_RULE_CONTENT")
    expected_keys = {
        "schema",
        "game",
        "effective_from_issue",
        "effective_to_issue",
        "prize_levels",
        "special_prizes",
    }
    if set(content) != expected_keys:
        raise RuleError(
            "规则内容字段集合不合法",
            code="INVALID_RULE_CONTENT",
            expected=sorted(expected_keys),
            actual=sorted(content),
        )
    schema = content["schema"]
    if schema != PRIZE_RULE_SCHEMA:
        raise RuleError(
            "不支持的规则内容架构",
            code="INVALID_RULE_CONTENT",
            schema=schema,
        )
    game = content["game"]
    if not isinstance(game, str) or not game:
        raise RuleError("规则必须声明彩种", code="INVALID_RULE_CONTENT")
    effective_from = _require_issue(content["effective_from_issue"], field="effective_from_issue")
    raw_to = content["effective_to_issue"]
    effective_to = None if raw_to is None else _require_issue(raw_to, field="effective_to_issue")

    raw_levels = content["prize_levels"]
    if not isinstance(raw_levels, list) or not raw_levels:
        raise RuleError("规则至少需要一个奖级", code="INVALID_RULE_CONTENT")
    levels: list[PrizeLevel] = []
    for raw_level in raw_levels:
        levels.append(_parse_prize_level(raw_level))

    raw_specials = content["special_prizes"]
    if not isinstance(raw_specials, list):
        raise RuleError("special_prizes 必须是数组", code="INVALID_RULE_CONTENT")
    specials = [_parse_special_prize(item) for item in raw_specials]

    return PrizeRule(
        game=game,
        effective_from_issue=effective_from,
        effective_to_issue=effective_to,
        prize_levels=tuple(levels),
        special_prizes=tuple(specials),
    )


def _require_issue(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or not value.isdigit():
        raise RuleError(
            f"{field} 必须为非空数字字符串",
            code="INVALID_RULE_CONTENT",
            field=field,
            value=value,
        )
    return value


def _parse_prize_level(raw: Any) -> PrizeLevel:
    if not isinstance(raw, Mapping) or set(raw) != {
        "code",
        "name",
        "rank",
        "amount_type",
        "match",
    }:
        raise RuleError("奖级字段集合不合法", code="INVALID_RULE_CONTENT")
    matches_raw = raw["match"]
    if not isinstance(matches_raw, list) or not matches_raw:
        raise RuleError("奖级必须声明命中组合", code="INVALID_RULE_CONTENT")
    matches: list[tuple[int, int]] = []
    for item in matches_raw:
        if not isinstance(item, Mapping) or set(item) != {"hits_red", "hits_blue"}:
            raise RuleError("命中组合字段不合法", code="INVALID_RULE_CONTENT")
        matches.append((item["hits_red"], item["hits_blue"]))
    return PrizeLevel(
        code=raw["code"],
        name=raw["name"],
        rank=raw["rank"],
        amount_type=raw["amount_type"],
        matches=tuple(matches),
    )


def _parse_special_prize(raw: Any) -> SpecialPrize:
    if not isinstance(raw, Mapping) or set(raw) != {
        "code",
        "name",
        "rank",
        "enabled",
        "condition",
    }:
        raise RuleError("特别奖字段集合不合法", code="INVALID_RULE_CONTENT")
    condition = raw["condition"]
    if not isinstance(condition, Mapping) or set(condition) != {"hits_red", "hits_blue"}:
        raise RuleError("特别奖命中条件不合法", code="INVALID_RULE_CONTENT")
    enabled = raw["enabled"]
    if not isinstance(enabled, bool):
        raise RuleError("特别奖启用状态必须为布尔值", code="INVALID_RULE_CONTENT")
    return SpecialPrize(
        code=raw["code"],
        name=raw["name"],
        rank=raw["rank"],
        enabled=enabled,
        hits_red=condition["hits_red"],
        hits_blue=condition["hits_blue"],
    )


def validate_rule_intervals(rules: Sequence[PrizeRule]) -> None:
    """校验同一彩种的规则期号区间不重叠；重叠是数据错误，直接拒绝。"""
    by_game: dict[str, list[PrizeRule]] = {}
    for rule in rules:
        by_game.setdefault(rule.game, []).append(rule)
    for game, items in by_game.items():
        ordered = sorted(items, key=lambda rule: issue_key(rule.effective_from_issue))
        for previous, following in pairwise(ordered):
            if previous.effective_to_issue is None or issue_key(
                previous.effective_to_issue
            ) >= issue_key(following.effective_from_issue):
                raise RuleError(
                    "同一彩种的规则版本期号区间重叠",
                    code="RULE_INTERVAL_OVERLAP",
                    game=game,
                    previous_from=previous.effective_from_issue,
                    previous_to=previous.effective_to_issue,
                    next_from=following.effective_from_issue,
                )


def resolve_rule(
    rules: Sequence[PrizeRule],
    game: str,
    issue: str,
) -> PrizeRule:
    """返回期号适用的唯一规则版本；缺失或重叠时抛出明确错误。"""
    issue_key(issue)
    matches = [rule for rule in rules if rule.game == game and rule.covers_issue(issue)]
    if not matches:
        raise RuleError(
            f"期号 {issue} 没有适用的规则版本",
            code="RULE_NOT_FOUND",
            game=game,
            issue=issue,
        )
    if len(matches) > 1:
        raise RuleError(
            "期号命中了多个规则版本，请先修正区间",
            code="RULE_INTERVAL_OVERLAP",
            game=game,
            issue=issue,
            versions=[[rule.effective_from_issue, rule.effective_to_issue] for rule in matches],
        )
    return matches[0]


def match_prize(hits_red: int, hits_blue: int, rule: PrizeRule) -> PrizeMatch | None:
    """按该期规则返回最高适用奖级；无匹配返回 ``None``。

    常规奖级与特别规定奖级一起参与比较，取 ``rank`` 最小者（1 为最高），
    因此 3 红 1 蓝命中五等奖而不是福运奖。
    """
    _validate_hits(hits_red, hits_blue, prize_code="*")
    candidates: list[PrizeMatch] = []
    for level in rule.prize_levels:
        if (hits_red, hits_blue) in level.matches:
            candidates.append(
                PrizeMatch(level=level.rank, code=level.code, name=level.name, rank=level.rank)
            )
    for special in rule.special_prizes:
        if special.matches(hits_red, hits_blue):
            candidates.append(
                PrizeMatch(
                    level=special.rank,
                    code=special.code,
                    name=special.name,
                    rank=special.rank,
                )
            )
    if not candidates:
        return None
    return min(candidates, key=lambda match: match.rank)


__all__ = [
    "AMOUNT_TYPES",
    "PRIZE_CODE_FIFTH",
    "PRIZE_CODE_FIRST",
    "PRIZE_CODE_FOURTH",
    "PRIZE_CODE_FUYUN",
    "PRIZE_CODE_SECOND",
    "PRIZE_CODE_SIXTH",
    "PRIZE_CODE_THIRD",
    "PRIZE_RULE_SCHEMA",
    "RULE_EVIDENCE_URL_NEW",
    "RULE_EVIDENCE_URL_REFERENCE",
    "RULE_NEW_FROM_ISSUE",
    "RULE_OLD_FROM_ISSUE",
    "RULE_OLD_TO_ISSUE",
    "PrizeLevel",
    "PrizeMatch",
    "PrizeRule",
    "RuleError",
    "SpecialPrize",
    "default_rule_contents",
    "default_rule_versions",
    "issue_key",
    "match_prize",
    "resolve_rule",
    "rule_from_content",
    "validate_rule_intervals",
]
