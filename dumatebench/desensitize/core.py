"""Desensitization logic ported from the Go implementation.

The Go files in this directory contain two related pieces:

* ``desensitize.go`` masks cloud/API secrets in strings and JSON objects.
* ``engine.go`` + ``dlp_v1.go`` audit common PII and credential patterns.

This module implements both as masking rules so dataset files can be rewritten
locally without depending on the Go service packages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import re
from typing import Any, Callable, Iterable

MASK_VALUE = "***"
DESENSITIZE_RULE_HITS_FIELD = "desensitize_rule_hits"
SECRET_KEY_RULE = "secret_key"

DEFAULT_WHITELIST_FIELDS = {
    "account_id",
    "account_type",
    "appid_v2",
    "cloud_id",
    "url",
    "as_id",
    "request_id",
}

SCOPE_TEXT = 1
SCOPE_FIELD = 2


Validator = Callable[[str], bool]


@dataclass(frozen=True)
class ReplacementRule:
    """A regex rule whose ``value_group`` is replaced with ``MASK_VALUE``."""

    rule_id: str
    description: str
    pattern: re.Pattern[str]
    value_group: int = 0
    scope: int = SCOPE_TEXT
    validator: Validator | None = None
    status: str = "active"


@dataclass
class MaskStats:
    """Counts masked values and rule hits."""

    masked_secrets: int = 0
    rule_hits: dict[str, int] = field(default_factory=dict)
    files_scanned: int = 0
    files_changed: int = 0
    files_skipped: int = 0

    def add_hit(self, rule_id: str, count: int = 1) -> None:
        self.masked_secrets += count
        self.rule_hits[rule_id] = self.rule_hits.get(rule_id, 0) + count

    def merge(self, other: "MaskStats") -> None:
        self.masked_secrets += other.masked_secrets
        self.files_scanned += other.files_scanned
        self.files_changed += other.files_changed
        self.files_skipped += other.files_skipped
        for rule_id, count in other.rule_hits.items():
            self.rule_hits[rule_id] = self.rule_hits.get(rule_id, 0) + count


@dataclass(frozen=True)
class _SecretPattern:
    pattern: re.Pattern[str]
    groups: tuple[int, ...]


SECRET_PATTERNS = (
    _SecretPattern(re.compile(r"(?i)([^0-9A-Za-z]|^)(LTAI[a-z0-9]{20})([^0-9A-Za-z]|$)"), (2,)),
    _SecretPattern(
        re.compile(r"(?i)(alibaba[a-z0-9_ .\-,]{0,25})(=|>|:=|\|\|:|<=|=>|:).{0,5}['\"]([a-z0-9]{30})['\"]"),
        (3,),
    ),
    _SecretPattern(re.compile(r"((A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16})"), (1,)),
    _SecretPattern(re.compile(r"(?i)(aws)?_?(secret)?_?(access)?_?key.{0,5}['\"]([A-Za-z0-9/+=]{40})['\"]"), (4,)),
    _SecretPattern(re.compile(r"(?i)\b([0-9a-f]{12}4[0-9a-f]{19}|ALTAK[a-z0-9]{21})\b"), (1,)),
    _SecretPattern(
        re.compile(r"(?i)(ACCESS_KEY_SECRET|secret[-_.]?(access)?(key)?|sk)[ \t'\"]*(?:[:=]|=>)[ \t]*['\"]?([0-9A-Za-z]{32})(?:[^a-zA-Z0-9(]|$)"),
        (4,),
    ),
    _SecretPattern(re.compile(r"(?i)(^|[^0-9A-Za-z_-])(sk-[A-Za-z0-9][A-Za-z0-9_-]{20,})([^0-9A-Za-z_-]|$)"), (2,)),
    _SecretPattern(re.compile(r"(bce-v3/)(ALTAK-[A-Za-z0-9]{21})(/)([a-z0-9]{40})"), (2, 4)),
    _SecretPattern(re.compile(r"(?i)(bce-v[1-3]/)(ALTAK-[A-Za-z0-9_-]{8,})(/)([A-Za-z0-9]{20,})"), (2, 4)),
    _SecretPattern(re.compile(r"(?i)(^|[^0-9A-Za-z_-])(ALTAK-[a-z0-9]{21})([^0-9A-Za-z_-]|$)"), (2,)),
    _SecretPattern(re.compile(r"(?i)(^|[^0-9A-Za-z])([0-9a-f]{40})([^0-9A-Za-z]|$)"), (2,)),
)


def _valid_id_card_date(value: str) -> bool:
    if len(value) != 18:
        return False
    try:
        datetime.strptime(value[6:14], "%Y%m%d")
    except ValueError:
        return False
    return True


DLP_RULES = (
    ReplacementRule(
        "ID_CARD",
        "中国大陆身份证 18 位",
        re.compile(r"(^|[^0-9])([1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])([^0-9A-Za-z]|$)"),
        value_group=2,
        validator=_valid_id_card_date,
    ),
    ReplacementRule("EMAIL", "邮箱地址", re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")),
    ReplacementRule("PHONE_CN", "中国大陆手机号", re.compile(r"(^|[^0-9])(1[3-9]\d{9})([^0-9]|$)"), value_group=2),
    ReplacementRule(
        "PLATE_CN",
        "中国车牌",
        re.compile(r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼使领][A-Z][A-HJ-NP-Z0-9]{4,5}[A-HJ-NP-Z0-9挂学警港澳]"),
    ),
    ReplacementRule(
        "PRIVATE_KEY",
        "私钥块",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----.*?-----END (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----", re.S),
        status="candidate",
    ),
    ReplacementRule(
        "DB_CONN",
        "数据库连接串",
        re.compile(r"\b(?:mysql|postgres(?:ql)?|mongodb(?:\+srv)?|redis|amqp|oracle)://[^\s'\"<>]+", re.I),
        status="candidate",
    ),
    ReplacementRule("JWT", "JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"), status="candidate"),
    ReplacementRule("GITHUB_TOKEN", "GitHub Personal Access Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"), status="candidate"),
    ReplacementRule(
        "SECRET_KV_QUOTED",
        "带引号的凭据赋值",
        re.compile(r"(?i)['\"]?(?:api[*\-]?key|access[*\-]?(?:token|key)|secret(?:[*\-]?key)?|auth[*\-]?token|client[_\-]?secret|password|passwd|pwd|密码|口令|密钥|令牌)['\"]?\s*[:=]\s*['\"]([^'\"]{4,})['\"]"),
        value_group=1,
        scope=SCOPE_TEXT | SCOPE_FIELD,
        status="candidate",
    ),
    ReplacementRule(
        "PASSWORD_FUNC",
        "函数第一参数位置的明文口令",
        re.compile(r"(?i)(?P<name>\b[-.\w]*(?:set)?[-.*]?(?:pass[0-3]?|pwd[0-3]?|password[0-3]?|passwd[0-3]?)*?(?:test|dev|prod|off|online)?)\(['\"](?P<sensitive>[\w+\-/$*@^!():?=,#]{6,50})['\"]\)"),
        value_group=2,
        status="candidate",
    ),
)


def create_whitelist_fields(extra_fields: str | Iterable[str] | None = None) -> set[str]:
    """Merge user-supplied whitelist fields with the Go defaults."""

    fields = set(DEFAULT_WHITELIST_FIELDS)
    if extra_fields is None:
        return fields
    if isinstance(extra_fields, str):
        raw = extra_fields.strip()
        if not raw:
            return fields
        if raw.startswith("[") and raw.endswith("]"):
            try:
                values = json.loads(raw)
                fields.update(str(item).strip() for item in values if str(item).strip())
                return fields
            except json.JSONDecodeError:
                pass
        fields.update(part.strip() for part in raw.split(",") if part.strip())
        return fields
    fields.update(str(item).strip() for item in extra_fields if str(item).strip())
    return fields


def mask_text(value: str, stats: MaskStats | None = None, include_dlp: bool = True) -> tuple[str, MaskStats]:
    """Mask all string-level secret and DLP patterns in ``value``."""

    stats = stats or MaskStats()
    if not value:
        return value, stats

    for secret_pattern in SECRET_PATTERNS:
        value = _replace_groups(value, secret_pattern.pattern, secret_pattern.groups, SECRET_KEY_RULE, stats)
    if include_dlp:
        for rule in DLP_RULES:
            if rule.scope & SCOPE_TEXT:
                value = _replace_rule(value, rule, stats)
    return value, stats


def mask_json_bytes(
    data: bytes,
    whitelist_fields: set[str] | None = None,
    include_dlp: bool = True,
    json_indent: int | None = 2,
) -> tuple[bytes, MaskStats]:
    """Mask JSON bytes when possible, otherwise treat bytes as plain UTF-8 text."""

    stats = MaskStats()
    whitelist_fields = whitelist_fields or create_whitelist_fields()
    text = data.decode("utf-8")
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        masked, stats = mask_text(text, stats, include_dlp=include_dlp)
        return masked.encode("utf-8"), stats

    changed, masked_document = _mask_node(document, "", whitelist_fields, stats, include_dlp)
    if not changed:
        return data, stats
    if isinstance(masked_document, dict):
        masked_document[DESENSITIZE_RULE_HITS_FIELD] = dict(sorted(stats.rule_hits.items()))
    if json_indent is None:
        serialized = json.dumps(masked_document, ensure_ascii=False, separators=(",", ":"))
    else:
        serialized = json.dumps(masked_document, ensure_ascii=False, indent=json_indent)
    return f"{serialized}\n".encode("utf-8"), stats


def _mask_node(
    value: Any,
    path: str,
    whitelist_fields: set[str],
    stats: MaskStats,
    include_dlp: bool,
) -> tuple[bool, Any]:
    if isinstance(value, dict):
        changed = False
        result = dict(value)
        for key, child in value.items():
            current_path = _append_field_path(path, str(key))
            if current_path in whitelist_fields or str(key) in whitelist_fields:
                continue
            child_changed, masked_child = _mask_node(child, current_path, whitelist_fields, stats, include_dlp)
            if child_changed:
                result[key] = masked_child
                changed = True
        return changed, result
    if isinstance(value, list):
        changed = False
        result = list(value)
        for index, child in enumerate(value):
            child_changed, masked_child = _mask_node(child, f"{path}[{index}]", whitelist_fields, stats, include_dlp)
            if child_changed:
                result[index] = masked_child
                changed = True
        return changed, result
    if isinstance(value, str):
        before = stats.masked_secrets
        masked, _ = mask_text(value, stats, include_dlp=include_dlp)
        if include_dlp:
            masked = _mask_sensitive_field_value(masked, path, stats)
        return stats.masked_secrets != before, masked
    return False, value


def _mask_sensitive_field_value(value: str, path: str, stats: MaskStats) -> str:
    if not path:
        return value
    field = _field_name(path)
    canonical = f'{field}="{value}"'
    for rule in DLP_RULES:
        if not (rule.scope & SCOPE_FIELD):
            continue
        if rule.pattern.search(canonical):
            stats.add_hit(rule.rule_id)
            return MASK_VALUE
    return value


def _replace_rule(value: str, rule: ReplacementRule, stats: MaskStats) -> str:
    return _replace_groups(value, rule.pattern, (rule.value_group,), rule.rule_id, stats, rule.validator)


def _replace_groups(
    value: str,
    pattern: re.Pattern[str],
    groups: tuple[int, ...],
    rule_id: str,
    stats: MaskStats,
    validator: Validator | None = None,
) -> str:
    matches = list(pattern.finditer(value))
    if not matches:
        return value

    pieces: list[str] = []
    cursor = 0
    hits = 0
    for match in matches:
        spans: list[tuple[int, int]] = []
        for group in groups:
            try:
                start, end = match.span(group)
            except IndexError:
                continue
            if start < 0 or end <= start:
                continue
            candidate = value[start:end]
            if validator is not None and not validator(candidate):
                continue
            spans.append((start, end))
        if not spans:
            continue
        pieces.append(value[cursor : match.start()])
        inner = match.start()
        for start, end in sorted(spans):
            pieces.append(value[inner:start])
            pieces.append(MASK_VALUE)
            inner = end
            hits += 1
        pieces.append(value[inner : match.end()])
        cursor = match.end()

    if hits == 0:
        return value
    pieces.append(value[cursor:])
    stats.add_hit(rule_id, hits)
    return "".join(pieces)


def _append_field_path(parent: str, key: str) -> str:
    return key if not parent else f"{parent}.{key}"


def _field_name(path: str) -> str:
    if "." in path:
        path = path.rsplit(".", 1)[1]
    if "[" in path:
        path = path.split("[", 1)[0]
    return path
