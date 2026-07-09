"""Helpers to build Wazuh indexer alert queries and aggregations."""

from __future__ import annotations

import re
from typing import Any, Dict


SEVERITY_LEVEL_MAP: dict[str, list[int]] = {
    "critical": [15, 16],
    "high": [12, 13, 14],
    "medium": [7, 8, 9, 10, 11],
    "low": [4, 5, 6],
    "info": [1, 2, 3],
}

SEVERITY_KEYS = tuple(SEVERITY_LEVEL_MAP.keys())
DEFAULT_ALERT_QUERY_FIELDS = [
    "rule.description^4",
    "agent.name^3",
    "agent.id^4",
    "rule.id^5",
    "full_log",
]
DEFAULT_DESCRIPTION_QUERY_FIELDS = [
    "rule.description^5",
    "full_log^2",
]
DEFAULT_HIGHLIGHT_FIELDS = {
    "rule.description": {
        "number_of_fragments": 2,
        "fragment_size": 180,
    },
    "full_log": {
        "number_of_fragments": 1,
        "fragment_size": 240,
    },
}

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_QUERY_STRING_SPECIAL_CHARS = set('\\+-=&|><!(){}[]^"~:/')


def _normalize_value(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if _CONTROL_CHAR_RE.search(normalized):
        raise ValueError(f"{field_name} contains unsupported control characters")
    if len(normalized) > 500:
        raise ValueError(f"{field_name} is too long")
    return normalized


def _contains_wildcards(value: str) -> bool:
    return "*" in value or "?" in value


def _escape_query_string(value: str, *, preserve_wildcards: bool = False) -> str:
    escaped: list[str] = []
    for char in value:
        if char in {"\n", "\r", "\t"}:
            escaped.append(" ")
            continue
        if char in _QUERY_STRING_SPECIAL_CHARS or (char in {"*", "?"} and not preserve_wildcards):
            escaped.append(f"\\{char}")
            continue
        escaped.append(char)
    return "".join(escaped)


def _build_text_clause(field: str, value: str, *, exact: bool = False) -> dict[str, Any]:
    if _contains_wildcards(value):
        return {
            "bool": {
                "should": [
                    {
                        "wildcard": {
                            f"{field}.keyword": {
                                "value": value,
                                "case_insensitive": True,
                                "boost": 4,
                            }
                        }
                    },
                    {
                        "query_string": {
                            "fields": [field],
                            "query": _escape_query_string(value, preserve_wildcards=True),
                            "default_operator": "AND",
                            "analyze_wildcard": True,
                            "allow_leading_wildcard": True,
                            "lenient": True,
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        }

    if exact:
        return {
            "bool": {
                "should": [
                    {"term": {f"{field}.keyword": value}},
                    {"term": {field: value}},
                ],
                "minimum_should_match": 1,
            }
        }

    return {"match": {field: {"query": value, "operator": "and"}}}


def _build_description_clause(value: str, *, mode: str = "smart") -> dict[str, Any]:
    normalized_mode = (mode or "smart").strip().lower()
    if normalized_mode not in {"smart", "exact", "contains"}:
        raise ValueError("Unsupported description_mode filter")

    use_wildcards = _contains_wildcards(value) or normalized_mode == "contains"
    should_clauses: list[dict[str, Any]] = []

    if use_wildcards:
        if normalized_mode == "contains" and "*" not in value:
            wildcard_value = f"*{value}*"
        else:
            wildcard_value = value if _contains_wildcards(value) else f"*{value}*"
        should_clauses.append(
            {
                "wildcard": {
                    "rule.description.keyword": {
                        "value": wildcard_value,
                        "case_insensitive": True,
                        "boost": 8,
                    }
                }
            }
        )
        should_clauses.append(
            {
                "query_string": {
                    "fields": DEFAULT_DESCRIPTION_QUERY_FIELDS,
                    "query": _escape_query_string(wildcard_value, preserve_wildcards=True),
                    "default_operator": "AND",
                    "analyze_wildcard": True,
                    "allow_leading_wildcard": True,
                    "lenient": True,
                }
            }
        )
        return {"bool": {"should": should_clauses, "minimum_should_match": 1}}

    should_clauses.append(
        {
            "match_phrase": {
                "rule.description": {
                    "query": value,
                    "boost": 8,
                }
            }
        }
    )
    should_clauses.append(
        {
            "term": {
                "rule.description.keyword": {
                    "value": value,
                    "boost": 10,
                }
            }
        }
    )

    if normalized_mode != "exact":
        should_clauses.append(
            {
                "match": {
                    "rule.description": {
                        "query": value,
                        "operator": "and",
                        "boost": 4,
                    }
                }
            }
        )
        should_clauses.append(
            {
                "multi_match": {
                    "query": value,
                    "fields": DEFAULT_DESCRIPTION_QUERY_FIELDS,
                    "type": "best_fields",
                    "operator": "and",
                }
            }
        )

    return {"bool": {"should": should_clauses, "minimum_should_match": 1}}


def _build_rule_id_clause(value: str) -> dict[str, Any]:
    if _contains_wildcards(value):
        return {
            "bool": {
                "should": [
                    {
                        "wildcard": {
                            "rule.id.keyword": {
                                "value": value,
                                "case_insensitive": True,
                                "boost": 8,
                            }
                        }
                    },
                    {
                        "query_string": {
                            "fields": ["rule.id"],
                            "query": _escape_query_string(value, preserve_wildcards=True),
                            "default_operator": "AND",
                            "analyze_wildcard": True,
                            "allow_leading_wildcard": True,
                            "lenient": True,
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        }

    return {
        "bool": {
            "should": [
                {"term": {"rule.id.keyword": value}},
                {"term": {"rule.id": value}},
            ],
            "minimum_should_match": 1,
        }
    }


def _build_user_clause(value: str) -> dict[str, Any]:
    return {
        "bool": {
            "should": [
                _build_text_clause("data.srcuser", value),
                _build_text_clause("data.dstuser", value),
                _build_text_clause("data.user", value),
            ],
            "minimum_should_match": 1,
        }
    }


def _build_level_clause(value: str) -> dict[str, Any]:
    try:
        level = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("level must be a number") from exc
    if level < 0:
        raise ValueError("level must be a positive number")
    return {"term": {"rule.level": level}}


def _build_highlight(filters: dict[str, Any]) -> dict[str, Any] | None:
    if not filters.get("description") and not filters.get("query"):
        return None
    return {
        "pre_tags": ["<em>"],
        "post_tags": ["</em>"],
        "fields": DEFAULT_HIGHLIGHT_FIELDS,
    }


def _build_severity_clause(severity: str) -> dict[str, Any] | None:
    levels = SEVERITY_LEVEL_MAP.get(severity.lower())
    if not levels:
        raise ValueError("Unsupported severity filter")
    return {"terms": {"rule.level": levels}}


def build_alert_query(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    filters = filters or {}
    filter_clauses: list[dict[str, Any]] = []
    must_clauses: list[dict[str, Any]] = []

    severity = _normalize_value(filters.get("severity"), field_name="severity")
    level = _normalize_value(filters.get("level"), field_name="level")
    hostname = _normalize_value(filters.get("hostname"), field_name="hostname")
    agent_id = _normalize_value(filters.get("agent_id"), field_name="agent_id")
    rule_id = _normalize_value(filters.get("rule_id"), field_name="rule_id")
    rule_group = _normalize_value(filters.get("rule_group"), field_name="rule_group")
    decoder = _normalize_value(filters.get("decoder"), field_name="decoder")
    src_ip = _normalize_value(filters.get("src_ip"), field_name="src_ip")
    dst_ip = _normalize_value(filters.get("dst_ip"), field_name="dst_ip")
    username = _normalize_value(filters.get("username"), field_name="username")
    description = _normalize_value(filters.get("description"), field_name="description")
    description_mode = _normalize_value(filters.get("description_mode"), field_name="description_mode") or "smart"
    query = _normalize_value(filters.get("query"), field_name="query")
    time_from = _normalize_value(filters.get("time_from"), field_name="time_from")
    time_to = _normalize_value(filters.get("time_to"), field_name="time_to")

    if severity:
        severity_clause = _build_severity_clause(severity)
        if severity_clause:
            filter_clauses.append(severity_clause)
    if level:
        filter_clauses.append(_build_level_clause(level))

    if time_from or time_to:
        range_query: Dict[str, Any] = {}
        if time_from:
            range_query["gte"] = time_from
        if time_to:
            range_query["lte"] = time_to
        filter_clauses.append({"range": {"timestamp": range_query}})

    if hostname:
        must_clauses.append(_build_text_clause("agent.name", hostname))
    if agent_id:
        must_clauses.append(_build_text_clause("agent.id", agent_id, exact=True))
    if rule_id:
        must_clauses.append(_build_rule_id_clause(rule_id))
    if rule_group:
        must_clauses.append(_build_text_clause("rule.groups", rule_group))
    if decoder:
        must_clauses.append(_build_text_clause("decoder.name", decoder))
    if src_ip:
        must_clauses.append(_build_text_clause("data.srcip", src_ip, exact=True))
    if dst_ip:
        must_clauses.append(_build_text_clause("data.dstip", dst_ip, exact=True))
    if username:
        must_clauses.append(_build_user_clause(username))
    if description:
        must_clauses.append(_build_description_clause(description, mode=description_mode))
    if query:
        must_clauses.append(
            {
                "query_string": {
                    "query": query,
                    "fields": DEFAULT_ALERT_QUERY_FIELDS,
                    "default_operator": "AND",
                    "analyze_wildcard": True,
                    "allow_leading_wildcard": True,
                    "lenient": True,
                }
            }
        )

    if not filter_clauses and not must_clauses:
        return {"match_all": {}}

    body: dict[str, Any] = {"bool": {}}
    if filter_clauses:
        body["bool"]["filter"] = filter_clauses
    if must_clauses:
        body["bool"]["must"] = must_clauses
    return body


def build_alert_search_body(filters: dict[str, Any] | None = None, *, size: int = 100, offset: int = 0) -> dict[str, Any]:
    body = {
        "size": size,
        "from": offset,
        "track_total_hits": True,
        "sort": [{"timestamp": {"order": "desc"}}],
        "query": build_alert_query(filters),
    }
    highlight = _build_highlight(filters or {})
    if highlight:
        body["highlight"] = highlight
    return body


def build_alert_aggregation_body(filters: dict[str, Any] | None = None, *, interval: str = "day") -> dict[str, Any]:
    filters = filters or {}
    severity_filters = {
        key: {"terms": {"rule.level": levels}}
        for key, levels in SEVERITY_LEVEL_MAP.items()
    }

    histogram: dict[str, Any] = {
        "field": "timestamp",
        "calendar_interval": interval,
        "min_doc_count": 0,
    }
    if filters.get("time_from") and filters.get("time_to"):
        histogram["extended_bounds"] = {
            "min": filters["time_from"],
            "max": filters["time_to"],
        }

    return {
        "size": 0,
        "track_total_hits": True,
        "query": build_alert_query(filters),
        "aggs": {
            "severity": {"filters": {"filters": severity_filters}},
            "latest_timestamp": {"max": {"field": "timestamp"}},
            "top_hosts": {"terms": {"field": "agent.name.keyword", "size": 5}},
            "top_rules": {
                "terms": {"field": "rule.id.keyword", "size": 5},
                "aggs": {
                    "description": {"terms": {"field": "rule.description.keyword", "size": 1}},
                },
            },
            "top_mitre_ids": {"terms": {"field": "rule.mitre.id.keyword", "size": 5}},
            "top_agent_groups": {"terms": {"field": "agent.groups.keyword", "size": 5}},
            "timeline": {
                "date_histogram": histogram,
                "aggs": {
                    "severity": {"filters": {"filters": severity_filters}},
                },
            },
        },
    }
