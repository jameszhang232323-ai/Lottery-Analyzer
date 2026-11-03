"""结算纯函数：逐行命中、奖级与金额状态（设计文档 9.4、10.3）。

输入是候选行号码、当期中奖号码、该期规则版本，以及可选的当期奖级金额表
（来自 ``draw_prize``）。输出逐行结果：

- ``awarded``：命中奖级且金额已知；
- ``unknown``：命中奖级但金额未取得，``amount_fen`` 为 ``None``，不猜金额；
- ``no_prize``：未中任何奖级，返奖为 0（不是未知）。

汇总只把已知金额计入 ``known_return_fen``；只要存在未知金额行，
``return_complete`` 就为 ``False``，不生成完整返奖结论。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from dajidali.domain.draw import SsqNumbers
from dajidali.engine import EngineError
from dajidali.engine.rule_resolver import PrizeRule, match_prize

LINE_STATUS_AWARDED = "awarded"
LINE_STATUS_UNKNOWN = "unknown"
LINE_STATUS_NO_PRIZE = "no_prize"

LINE_STATUSES: tuple[str, ...] = (LINE_STATUS_AWARDED, LINE_STATUS_UNKNOWN, LINE_STATUS_NO_PRIZE)


@dataclass(frozen=True, slots=True)
class LineResult:
    """一注候选的结算结果；``prize_level``/``prize_code`` 在未中奖时为 ``None``。"""

    hits_red: int
    hits_blue: int
    prize_level: int | None
    prize_code: str | None
    amount_fen: int | None
    status: str

    def to_canonical(self) -> dict[str, Any]:
        return {
            "hits_red": self.hits_red,
            "hits_blue": self.hits_blue,
            "prize_level": self.prize_level,
            "prize_code": self.prize_code,
            "amount_fen": self.amount_fen,
            "status": self.status,
        }


def _validate_amount(prize_code: str, amount_fen: int | None) -> int | None:
    if amount_fen is None:
        return None
    if isinstance(amount_fen, bool) or not isinstance(amount_fen, int) or amount_fen < 0:
        raise EngineError(
            "奖级金额必须为非负整数分",
            code="INVALID_PRIZE_AMOUNT",
            prize_code=prize_code,
            amount_fen=amount_fen,
        )
    return amount_fen


def settle_lines(
    lines: Sequence[SsqNumbers],
    draw_numbers: SsqNumbers,
    rule: PrizeRule,
    prizes: Mapping[str, int | None] | None = None,
) -> list[LineResult]:
    """按当期规则结算每注号码，返回与输入顺序一致的结果列表。

    Args:
        lines: 候选号码（每注 6 红 + 1 蓝）。
        draw_numbers: 当期开奖号码。
        rule: 已按期号解析出的规则版本。
        prizes: 当期奖级金额表 ``prize_code -> amount_fen``；缺失的奖级视为
            金额未知，而不是 0。
    """
    if not isinstance(draw_numbers, SsqNumbers):
        raise EngineError(
            "结算只支持双色球号码",
            code="NUMBER_TYPE_MISMATCH",
            type=type(draw_numbers).__name__,
        )
    if not isinstance(rule, PrizeRule):
        raise EngineError("结算必须提供 PrizeRule", code="INVALID_RULE")
    amounts: dict[str, int | None] = dict(prizes or {})
    draw_reds = set(draw_numbers.reds)
    results: list[LineResult] = []
    for line in lines:
        if not isinstance(line, SsqNumbers):
            raise EngineError(
                "候选行必须是双色球号码",
                code="NUMBER_TYPE_MISMATCH",
                type=type(line).__name__,
            )
        hits_red = len(draw_reds.intersection(line.reds))
        hits_blue = int(line.blue == draw_numbers.blue)
        match = match_prize(hits_red, hits_blue, rule)
        if match is None:
            results.append(
                LineResult(
                    hits_red=hits_red,
                    hits_blue=hits_blue,
                    prize_level=None,
                    prize_code=None,
                    amount_fen=0,
                    status=LINE_STATUS_NO_PRIZE,
                )
            )
            continue
        amount_fen = _validate_amount(match.code, amounts.get(match.code))
        results.append(
            LineResult(
                hits_red=hits_red,
                hits_blue=hits_blue,
                prize_level=match.level,
                prize_code=match.code,
                amount_fen=amount_fen,
                status=LINE_STATUS_AWARDED if amount_fen is not None else LINE_STATUS_UNKNOWN,
            )
        )
    return results


def summarize_results(results: Sequence[LineResult]) -> dict[str, Any]:
    """汇总命中分布、总注数、已知返奖与未知金额行数。"""
    hit_counter: Counter[tuple[int, int]] = Counter(
        (result.hits_red, result.hits_blue) for result in results
    )
    prize_counter: Counter[tuple[int, str]] = Counter()
    prize_known_return: dict[str, int] = {}
    winning_lines = 0
    known_amount_lines = 0
    unknown_amount_lines = 0
    known_return_fen = 0
    for result in results:
        if result.prize_code is None:
            continue
        winning_lines += 1
        level = result.prize_level if result.prize_level is not None else 0
        prize_counter[(level, result.prize_code)] += 1
        if result.status == LINE_STATUS_AWARDED and result.amount_fen is not None:
            known_amount_lines += 1
            known_return_fen += result.amount_fen
            prize_known_return[result.prize_code] = (
                prize_known_return.get(result.prize_code, 0) + result.amount_fen
            )
        else:
            unknown_amount_lines += 1

    hit_distribution = [
        {"hits_red": hits_red, "hits_blue": hits_blue, "count": count}
        for (hits_red, hits_blue), count in sorted(hit_counter.items())
    ]
    prize_distribution = [
        {
            "prize_level": level,
            "prize_code": code,
            "count": count,
            "known_return_fen": prize_known_return.get(code, 0),
        }
        for (level, code), count in sorted(prize_counter.items())
    ]
    return {
        "total_lines": len(results),
        "winning_lines": winning_lines,
        "known_amount_lines": known_amount_lines,
        "unknown_amount_lines": unknown_amount_lines,
        "known_return_fen": known_return_fen,
        "return_complete": unknown_amount_lines == 0,
        "hit_distribution": hit_distribution,
        "prize_distribution": prize_distribution,
    }


__all__ = [
    "LINE_STATUSES",
    "LINE_STATUS_AWARDED",
    "LINE_STATUS_NO_PRIZE",
    "LINE_STATUS_UNKNOWN",
    "LineResult",
    "settle_lines",
    "summarize_results",
]
