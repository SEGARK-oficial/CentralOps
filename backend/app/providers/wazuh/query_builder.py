"""Helpers to build and validate Wazuh Query Language (WQL) filters."""

from __future__ import annotations

import re


_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_VALID_WQL_OPERATOR_RE = re.compile(r"(=|!=|<|>|~)")
_SIMPLE_TERM_RE = re.compile(r"[^A-Za-z0-9._ -]+")
_SIMPLE_WQL_FIELDS = ("id", "name", "ip", "os.name", "version", "group")


def _validate_raw_query(query: str) -> str:
    normalized = query.strip()
    if not normalized:
        raise ValueError("WQL query must not be empty")
    if _CONTROL_CHAR_RE.search(normalized):
        raise ValueError("WQL query contains unsupported control characters")
    if len(normalized) > 500:
        raise ValueError("WQL query is too long")
    if not _VALID_WQL_OPERATOR_RE.search(normalized):
        raise ValueError("WQL query must contain at least one valid operator")
    return normalized


def build_agent_query(query: str | None, *, mode: str = "simple") -> str | None:
    if query is None:
        return None

    normalized = query.strip()
    if not normalized:
        return None

    normalized_mode = mode.strip().lower() if mode else "simple"
    if normalized_mode == "wql":
        return _validate_raw_query(normalized)
    if normalized_mode != "simple":
        raise ValueError("Unsupported query mode")

    cleaned = _SIMPLE_TERM_RE.sub(" ", normalized)
    tokens = [token for token in cleaned.split() if token]
    if not tokens:
        raise ValueError("Search term must contain letters or numbers")

    built_query = ";".join(
        f"({','.join(f'{field}~{token}' for field in _SIMPLE_WQL_FIELDS)})"
        for token in tokens
    )
    if len(built_query) > 500:
        raise ValueError("Search term is too long")
    return built_query
