"""``AiReportV1`` 的严格校验（设计文档 8.3、8.4）。

校验是“默认拒绝”的：协议结构、回显字段、事实引用、数值声明、策略参数、
禁止字段任何一项不满足都返回结构化错误（``code`` + ``path``），绝不宽松放行。
错误码覆盖设计要求的失败类型：

- ``PROTOCOL_INVALID`` / ``UNEXPECTED_FIELD``：协议不合法或出现额外字段；
- ``REQUEST_ID_MISMATCH`` / ``SNAPSHOT_MISMATCH`` / ``SCHEMA_VERSION_MISMATCH``：
  回显与本次证据包不一致；
- ``UNKNOWN_FACT_ID``：引用了证据包中不存在的事实；
- ``NUMERIC_CLAIM_UNSUPPORTED``：观察文字声明的数字无法对应所引用事实；
- ``INVALID_VALIDATION_STATUS``：AI 自封“已验证”；
- ``UNEXPECTED_S0_PARAMS`` / ``MISSING_S1_PARAMS`` / ``INVALID_WINDOW`` /
  ``INVALID_ALPHA`` / ``INVALID_LAMBDA``：策略参数越界；
- ``FORBIDDEN_CONTENT`` / ``FORBIDDEN_SECRET``：SQL、脚本、URL、文件路径或密钥。

数值声明检查只要求“文字里的数字能在被引用事实/证据上下文中找到出处”，
不判断推理是否正确；它拦的是凭空编造的数值。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from dajidali.domain.errors import InvalidNumbersError
from dajidali.domain.strategy import (
    ALLOWED_ALPHAS,
    ALLOWED_LAMBDAS,
    ALLOWED_WINDOWS,
    Conditions,
)
from dajidali.engine.statistics.frequency import window_label

REPORT_SCHEMA_VERSION = "1.0"
OBSERVATION_MAX_LENGTH = 1000
REQUIRED_REPORT_FIELDS: tuple[str, ...] = (
    "schema_version",
    "request_id",
    "snapshot_hash",
    "observations",
    "strategy_proposals",
    "limitations",
)
OBSERVATION_FIELDS: frozenset[str] = frozenset({"fact_ids", "interpretation"})
PROPOSAL_FIELDS: frozenset[str] = frozenset(
    {
        "strategy_type",
        "window",
        "alpha_periods",
        "lambda",
        "conditions",
        "reason_fact_ids",
        "validation_status",
    }
)
PROPOSAL_REQUIRED_FIELDS: frozenset[str] = frozenset({"strategy_type", "validation_status"})
CONDITION_FIELDS: frozenset[str] = frozenset(
    {
        "fixed_red",
        "excluded_red",
        "allowed_odd_counts",
        "sum_range",
        "zone_counts",
        "max_adjacent_pairs",
    }
)
STRATEGY_TYPES: tuple[str, ...] = ("S0", "S1")

NUMBER_TOKEN = re.compile(r"\d+(?:\.\d+)?")
URL_PATTERN = re.compile(r"(?i)\b(?:https?|ftp|file)://|\bwww\.")
DOMAIN_PATTERN = re.compile(
    r"(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\."
    r"(?:com|cn|net|org|io|ai|dev|app|invalid|local|top|xyz)\b"
)
PATH_PATTERN = re.compile(
    r"(?i)(?:[a-z]:\\|\\\\[a-z0-9._-]+\\|/(?:etc|home|var|usr|tmp|opt|root|users)/|\.\./|\.\.\\)"
)
SQL_PATTERN = re.compile(
    r"(?is)\b(?:select\s+.{0,160}?\s+from\s+\w+|insert\s+into\s+\w+|"
    r"update\s+\w+\s+set\s|delete\s+from\s+\w+|drop\s+table\s+\w+|"
    r"alter\s+table\s+\w+|create\s+table\s+\w+|union\s+select\s+\w+)"
)
SCRIPT_PATTERN = re.compile(
    r"(?i)(<script|javascript:|onerror\s*=|eval\s*\(|exec\s*\(|os\.system|"
    r"subprocess\.|__import__|powershell|cmd\.exe)"
)
SECRET_PATTERN = re.compile(
    r"(?i)(sk-[a-z0-9]{8,}|api[_-]?key|authorization\s*:|bearer\s+[a-z0-9._-]{8,}|"
    r"password\s*=|secret\s*=|master_key)"
)


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """一条结构化校验错误：机器可读错误码、字段路径与可读原因。"""

    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """校验结果；只有 ``errors`` 为空时才携带可应用报告。"""

    errors: tuple[ValidationIssue, ...] = ()
    report: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def primary_code(self) -> str | None:
        """第一条错误码，供任务失败码使用。"""
        return self.errors[0].code if self.errors else None

    @property
    def error_codes(self) -> list[str]:
        seen: list[str] = []
        for issue in self.errors:
            if issue.code not in seen:
                seen.append(issue.code)
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [issue.to_dict() for issue in self.errors],
            "error_codes": self.error_codes,
        }


def failure_result(code: str, message: str, *, path: str = "$") -> ValidationResult:
    """构造“没有可用报告”的失败结果（超时、无响应、损坏 JSON 等）。"""
    return ValidationResult(errors=(ValidationIssue(code, path, message),))


def _field(path: str, name: str) -> str:
    return f"{path}.{name}"


def _index(path: str, index: int) -> str:
    return f"{path}[{index}]"


def _check_object(
    value: Any,
    path: str,
    *,
    allowed: frozenset[str] | set[str] | tuple[str, ...],
    required: Sequence[str],
    issues: list[ValidationIssue],
) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        issues.append(ValidationIssue("PROTOCOL_INVALID", path, "必须是 JSON 对象"))
        return None
    extra = sorted(set(value) - set(allowed))
    for name in extra:
        issues.append(
            ValidationIssue("UNEXPECTED_FIELD", _field(path, name), f"不允许的字段：{name}")
        )
    for name in required:
        if name not in value:
            issues.append(
                ValidationIssue("PROTOCOL_INVALID", _field(path, name), f"缺少必填字段：{name}")
            )
    return value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number_tokens(value: Any) -> list[str]:
    return NUMBER_TOKEN.findall(str(value))


def _fact_identity_tokens(fact: Mapping[str, Any]) -> set[str]:
    """事实自身支持的书面数字：数值、样本量、窗口与 ID 内数字。"""
    tokens: set[str] = set()
    value = fact.get("value")
    if _is_number(value):
        tokens.add(str(value))
        if isinstance(value, float) and value.is_integer():
            tokens.add(str(int(value)))
    sample_n = fact.get("sample_n")
    if isinstance(sample_n, int) and not isinstance(sample_n, bool):
        tokens.add(str(sample_n))
    window = fact.get("window")
    if window is not None:
        tokens.add(str(window))
        if window in ALLOWED_WINDOWS:
            tokens.add(window_label(window))
    tokens.update(_number_tokens(fact.get("fact_id", "")))
    tokens.update(_number_tokens(fact.get("metric_version", "")))
    return tokens


def _evidence_context_tokens(evidence: Mapping[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for window in evidence.get("windows") or []:
        tokens.add(str(window))
        if window in ALLOWED_WINDOWS:
            tokens.add(window_label(window))
    tokens.add(str(evidence.get("sample_n")))
    issue = str(evidence.get("as_of_issue", ""))
    tokens.update(_number_tokens(issue))
    if len(issue) >= 4:
        tokens.add(issue[:4])
    quality = evidence.get("quality")
    if isinstance(quality, Mapping):
        for name in ("effective_n", "missing", "conflicts"):
            tokens.add(str(quality.get(name)))
    return tokens


def _check_numeric_claims(
    interpretation: str,
    referenced_facts: Sequence[Mapping[str, Any]],
    evidence: Mapping[str, Any],
    path: str,
    issues: list[ValidationIssue],
) -> None:
    supported = _evidence_context_tokens(evidence)
    values: list[float] = []
    for fact in referenced_facts:
        supported.update(_fact_identity_tokens(fact))
        value = fact.get("value")
        if _is_number(value):
            values.append(float(value))
    reported: set[str] = set()
    for token in NUMBER_TOKEN.findall(interpretation):
        if len(token.replace(".", "")) < 2:
            # 个位数在中文叙述里常是序数/量词，不作为“数值声明”判定。
            continue
        if token in supported or token in reported:
            continue
        number = float(token)
        if any(abs(number - value) <= 1e-9 * max(1.0, abs(value)) for value in values):
            continue
        reported.add(token)
        issues.append(
            ValidationIssue(
                "NUMERIC_CLAIM_UNSUPPORTED",
                path,
                f"数字 {token} 无法对应所引用的事实",
            )
        )


def _check_observations(
    value: Any,
    evidence: Mapping[str, Any],
    facts_by_id: Mapping[str, Mapping[str, Any]],
    issues: list[ValidationIssue],
) -> None:
    if not isinstance(value, list):
        issues.append(ValidationIssue("PROTOCOL_INVALID", "$.observations", "必须是数组"))
        return
    for index, observation in enumerate(value):
        path = _index("$.observations", index)
        checked = _check_object(
            observation,
            path,
            allowed=OBSERVATION_FIELDS,
            required=("fact_ids", "interpretation"),
            issues=issues,
        )
        if checked is None:
            continue
        referenced: list[Mapping[str, Any]] = []
        fact_ids = checked.get("fact_ids")
        if not isinstance(fact_ids, list) or not fact_ids:
            issues.append(
                ValidationIssue("PROTOCOL_INVALID", _field(path, "fact_ids"), "至少引用一个事实")
            )
        else:
            for position, fact_id in enumerate(fact_ids):
                fact_path = _index(_field(path, "fact_ids"), position)
                if not isinstance(fact_id, str) or not fact_id:
                    issues.append(ValidationIssue("PROTOCOL_INVALID", fact_path, "事实 ID 必须为非空字符串"))
                    continue
                fact = facts_by_id.get(fact_id)
                if fact is None:
                    issues.append(
                        ValidationIssue("UNKNOWN_FACT_ID", fact_path, f"证据包中不存在事实：{fact_id}")
                    )
                    continue
                referenced.append(fact)
        interpretation = checked.get("interpretation")
        if not isinstance(interpretation, str) or not interpretation:
            issues.append(
                ValidationIssue("PROTOCOL_INVALID", _field(path, "interpretation"), "解释文本不能为空")
            )
            continue
        if len(interpretation) > OBSERVATION_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    "PROTOCOL_INVALID",
                    _field(path, "interpretation"),
                    f"解释文本不能超过 {OBSERVATION_MAX_LENGTH} 字符",
                )
            )
            continue
        if fact_ids and referenced:
            _check_numeric_claims(
                interpretation,
                referenced,
                evidence,
                _field(path, "interpretation"),
                issues,
            )


def _check_conditions(
    value: Any,
    path: str,
    issues: list[ValidationIssue],
) -> None:
    if value is None:
        return
    checked = _check_object(
        value,
        path,
        allowed=CONDITION_FIELDS,
        required=(),
        issues=issues,
    )
    if checked is None:
        return
    kwargs: dict[str, Any] = {}
    for name in ("fixed_red", "excluded_red", "allowed_odd_counts"):
        if checked.get(name) is None:
            continue
        item = checked[name]
        if not isinstance(item, list) or any(
            isinstance(entry, bool) or not isinstance(entry, int) for entry in item
        ):
            issues.append(
                ValidationIssue("INVALID_CONDITIONS", _field(path, name), f"{name} 必须是整数数组")
            )
            return
        kwargs[name] = tuple(item)
    for name in ("sum_range", "zone_counts"):
        if checked.get(name) is None:
            continue
        item = checked[name]
        if not isinstance(item, list) or any(
            isinstance(entry, bool) or not isinstance(entry, int) for entry in item
        ):
            issues.append(
                ValidationIssue("INVALID_CONDITIONS", _field(path, name), f"{name} 必须是整数数组")
            )
            return
        kwargs[name] = tuple(item)
    if checked.get("max_adjacent_pairs") is not None:
        maximum = checked["max_adjacent_pairs"]
        if isinstance(maximum, bool) or not isinstance(maximum, int):
            issues.append(
                ValidationIssue(
                    "INVALID_CONDITIONS",
                    _field(path, "max_adjacent_pairs"),
                    "max_adjacent_pairs 必须是整数",
                )
            )
            return
        kwargs["max_adjacent_pairs"] = maximum
    try:
        Conditions(**kwargs)
    except InvalidNumbersError as exc:
        issues.append(
            ValidationIssue(
                "INVALID_CONDITIONS",
                path,
                f"结构条件不合法：{exc.code}",
            )
        )


def _check_proposals(
    value: Any,
    evidence: Mapping[str, Any],
    facts_by_id: Mapping[str, Mapping[str, Any]],
    issues: list[ValidationIssue],
) -> None:
    if not isinstance(value, list):
        issues.append(ValidationIssue("PROTOCOL_INVALID", "$.strategy_proposals", "必须是数组"))
        return
    allowed_strategies = set(evidence.get("allowed_strategy_types") or ())
    for index, proposal in enumerate(value):
        path = _index("$.strategy_proposals", index)
        checked = _check_object(
            proposal,
            path,
            allowed=PROPOSAL_FIELDS,
            required=tuple(PROPOSAL_REQUIRED_FIELDS),
            issues=issues,
        )
        if checked is None:
            continue
        status = checked.get("validation_status")
        if status != "proposed":
            issues.append(
                ValidationIssue(
                    "INVALID_VALIDATION_STATUS",
                    _field(path, "validation_status"),
                    "validation_status 只能是 proposed",
                )
            )
        strategy_type = checked.get("strategy_type")
        if strategy_type not in STRATEGY_TYPES:
            issues.append(
                ValidationIssue(
                    "INVALID_STRATEGY_TYPE",
                    _field(path, "strategy_type"),
                    "strategy_type 只接受 S0 或 S1",
                )
            )
            strategy_type = None
        elif strategy_type not in allowed_strategies:
            issues.append(
                ValidationIssue(
                    "STRATEGY_NOT_ALLOWED",
                    _field(path, "strategy_type"),
                    f"本次证据包不允许策略 {strategy_type}",
                )
            )

        if strategy_type == "S0":
            for name in ("window", "alpha_periods", "lambda"):
                if name in checked and checked[name] is not None:
                    issues.append(
                        ValidationIssue(
                            "UNEXPECTED_S0_PARAMS",
                            _field(path, name),
                            "S0 不接受 S1 参数",
                        )
                    )
        elif strategy_type == "S1":
            missing = [
                name
                for name in ("window", "alpha_periods", "lambda")
                if name not in checked or checked[name] is None
            ]
            if missing:
                issues.append(
                    ValidationIssue(
                        "MISSING_S1_PARAMS",
                        path,
                        f"S1 必须齐全参数：{'、'.join(missing)}",
                    )
                )
            if "window" in checked and checked["window"] is not None:
                window = checked["window"]
                if isinstance(window, bool) or window not in ALLOWED_WINDOWS:
                    issues.append(
                        ValidationIssue(
                            "INVALID_WINDOW",
                            _field(path, "window"),
                            "窗口必须是 30、100、300 或 all",
                        )
                    )
            if "alpha_periods" in checked and checked["alpha_periods"] is not None:
                alpha = checked["alpha_periods"]
                if isinstance(alpha, bool) or alpha not in ALLOWED_ALPHAS:
                    issues.append(
                        ValidationIssue(
                            "INVALID_ALPHA",
                            _field(path, "alpha_periods"),
                            "alpha 必须是 10、30 或 100",
                        )
                    )
            if "lambda" in checked and checked["lambda"] is not None:
                lam = checked["lambda"]
                if isinstance(lam, bool) or not _is_number(lam) or float(lam) not in ALLOWED_LAMBDAS:
                    issues.append(
                        ValidationIssue(
                            "INVALID_LAMBDA",
                            _field(path, "lambda"),
                            "lambda 必须是 0、0.1 或 0.25",
                        )
                    )

        if "conditions" in checked:
            _check_conditions(checked["conditions"], _field(path, "conditions"), issues)

        reason_ids = checked.get("reason_fact_ids")
        if reason_ids is not None:
            if not isinstance(reason_ids, list):
                issues.append(
                    ValidationIssue(
                        "PROTOCOL_INVALID", _field(path, "reason_fact_ids"), "必须是数组"
                    )
                )
            else:
                for position, fact_id in enumerate(reason_ids):
                    fact_path = _index(_field(path, "reason_fact_ids"), position)
                    if not isinstance(fact_id, str) or fact_id not in facts_by_id:
                        issues.append(
                            ValidationIssue(
                                "UNKNOWN_FACT_ID",
                                fact_path,
                                f"证据包中不存在事实：{fact_id}",
                            )
                        )


def _check_limitations(value: Any, issues: list[ValidationIssue]) -> None:
    if not isinstance(value, list) or not value:
        issues.append(
            ValidationIssue("PROTOCOL_INVALID", "$.limitations", "至少需要一条限制说明")
        )
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            issues.append(
                ValidationIssue(
                    "PROTOCOL_INVALID", _index("$.limitations", index), "限制说明不能为空"
                )
            )


def _scan_forbidden(value: Any, path: str, issues: list[ValidationIssue]) -> None:
    if isinstance(value, str):
        if URL_PATTERN.search(value) or DOMAIN_PATTERN.search(value):
            issues.append(ValidationIssue("FORBIDDEN_CONTENT", path, "不允许出现 URL"))
        if PATH_PATTERN.search(value):
            issues.append(ValidationIssue("FORBIDDEN_CONTENT", path, "不允许出现文件路径"))
        if SQL_PATTERN.search(value):
            issues.append(ValidationIssue("FORBIDDEN_CONTENT", path, "不允许出现 SQL 语句"))
        if SCRIPT_PATTERN.search(value):
            issues.append(ValidationIssue("FORBIDDEN_CONTENT", path, "不允许出现脚本代码"))
        if SECRET_PATTERN.search(value):
            issues.append(ValidationIssue("FORBIDDEN_SECRET", path, "不允许出现密钥或认证信息"))
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_path = _field(path, str(key))
            _scan_forbidden(str(key), key_path, issues)
            _scan_forbidden(item, key_path, issues)
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for index, item in enumerate(value):
            _scan_forbidden(item, _index(path, index), issues)


def validate_report(report: Any, evidence: Mapping[str, Any]) -> ValidationResult:
    """校验模型输出，返回结构化错误或可应用报告。

    Args:
        report: 已解析的 JSON 对象（模型输出）。
        evidence: 本次发送的 ``AnalysisEvidenceV1``，用于回显与事实引用比对。

    Returns:
        ``ValidationResult``；``ok`` 为真时 ``report`` 才非空。
    """
    if not isinstance(evidence, Mapping):
        raise ValueError("evidence 必须是证据包字典")
    issues: list[ValidationIssue] = []
    evidence_facts = evidence.get("facts")
    facts_by_id: dict[str, Mapping[str, Any]] = {}
    if isinstance(evidence_facts, list):
        for fact in evidence_facts:
            if isinstance(fact, Mapping) and isinstance(fact.get("fact_id"), str):
                facts_by_id[fact["fact_id"]] = fact

    checked = _check_object(
        report,
        "$",
        allowed=REQUIRED_REPORT_FIELDS,
        required=REQUIRED_REPORT_FIELDS,
        issues=issues,
    )
    if checked is None:
        return ValidationResult(errors=tuple(issues))

    schema_version = checked.get("schema_version")
    if not isinstance(schema_version, str):
        issues.append(
            ValidationIssue("PROTOCOL_INVALID", "$.schema_version", "schema_version 必须为字符串")
        )
    elif schema_version != REPORT_SCHEMA_VERSION or schema_version != evidence.get(
        "schema_version"
    ):
        issues.append(
            ValidationIssue(
                "SCHEMA_VERSION_MISMATCH",
                "$.schema_version",
                "schema_version 与本次证据包不一致",
            )
        )

    request_id = checked.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        issues.append(
            ValidationIssue("PROTOCOL_INVALID", "$.request_id", "request_id 必须为非空字符串")
        )
    elif request_id != evidence.get("request_id"):
        issues.append(
            ValidationIssue("REQUEST_ID_MISMATCH", "$.request_id", "request_id 与本次请求不一致")
        )

    snapshot_hash = checked.get("snapshot_hash")
    if not isinstance(snapshot_hash, str):
        issues.append(
            ValidationIssue("PROTOCOL_INVALID", "$.snapshot_hash", "snapshot_hash 必须为字符串")
        )
    elif snapshot_hash != evidence.get("snapshot_hash"):
        issues.append(
            ValidationIssue(
                "SNAPSHOT_MISMATCH", "$.snapshot_hash", "snapshot_hash 与本次证据包不一致"
            )
        )

    _check_observations(checked.get("observations"), evidence, facts_by_id, issues)
    _check_proposals(checked.get("strategy_proposals"), evidence, facts_by_id, issues)
    _check_limitations(checked.get("limitations"), issues)
    _scan_forbidden(checked, "$", issues)

    if issues:
        return ValidationResult(errors=tuple(issues))
    return ValidationResult(report=dict(checked))


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "ValidationIssue",
    "ValidationResult",
    "failure_result",
    "validate_report",
]
