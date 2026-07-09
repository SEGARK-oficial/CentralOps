"""Testes do TokenScrubFilter — defesa em profundidade contra leak de PAT em logs."""

from __future__ import annotations

import io
import json
import logging

import pytest

from backend.app.core.logging_config import (
    TokenScrubFilter,
    _scrub_pat,
    configure_logging,
)


SAMPLE_TOKEN = "copsk_aB3xK7zY9MmRTpFqZqVm5e8XvU4jWkH7c0n1L2gIo67Y"


def test_scrub_pat_replaces_token():
    s = f"failed with token={SAMPLE_TOKEN} and id=42"
    out = _scrub_pat(s)
    assert "copsk_aB" not in out
    assert "copsk_[REDACTED]" in out
    assert "id=42" in out


def test_scrub_pat_no_match_passes_through():
    s = "no token here"
    assert _scrub_pat(s) == s


def test_scrub_pat_short_prefix_not_matched():
    """copsk_x (sem entropia suficiente) NÃO deve ser substituído como token real."""
    s = "string copsk_x sem token"
    # Regex exige >= 20 chars do alfabeto urlsafe — copsk_x não bate.
    assert _scrub_pat(s) == s


def test_filter_scrubs_message():
    f = TokenScrubFilter()
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0,
        msg=f"got {SAMPLE_TOKEN}", args=(), exc_info=None,
    )
    f.filter(record)
    assert "copsk_aB" not in record.msg
    assert "REDACTED" in record.msg


def test_filter_scrubs_args_tuple():
    f = TokenScrubFilter()
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0,
        msg="auth=%s id=%d", args=(SAMPLE_TOKEN, 42), exc_info=None,
    )
    f.filter(record)
    assert SAMPLE_TOKEN not in record.args
    assert record.args[1] == 42


def test_filter_scrubs_args_dict():
    f = TokenScrubFilter()
    # logging.LogRecord aceita dict apenas via args=({...},) — wrap em tuple.
    # Mas o loop interno não processa essa forma; testamos branch dict puro
    # construindo LogRecord normalmente e sobrescrevendo args após.
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0,
        msg="auth=%(token)s", args=({"token": SAMPLE_TOKEN},), exc_info=None,
    )
    # O LogRecord normaliza args=({...},) → record.args == {"token": ...}
    # Forçamos branch isinstance(record.args, dict).
    record.args = {"token": SAMPLE_TOKEN}
    f.filter(record)
    assert SAMPLE_TOKEN not in record.args["token"]
    assert "REDACTED" in record.args["token"]


def test_configure_logging_attaches_filter_idempotently():
    configure_logging()
    root = logging.getLogger()
    count1 = sum(1 for f in root.filters if isinstance(f, TokenScrubFilter))
    configure_logging()
    count2 = sum(1 for f in root.filters if isinstance(f, TokenScrubFilter))
    assert count1 == 1
    assert count2 == 1


def test_full_logger_pipeline_redacts_token(caplog):
    """End-to-end: emite log via logger normal, captura output, verifica scrub."""
    configure_logging()
    root = logging.getLogger()
    # Substitui handler stdout por buffer
    buf = io.StringIO()
    original_streams = []
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler):
            original_streams.append((h, h.stream))
            h.stream = buf

    try:
        log = logging.getLogger("test.pat.leak")
        log.warning("auth header sent: Authorization=Bearer %s", SAMPLE_TOKEN)
        log.error("url with token: https://api/?t=%s", SAMPLE_TOKEN)
        # Extras nominativos (já cobertos pelo formatter)
        log.info("creating token", extra={"client_secret": SAMPLE_TOKEN})

        output = buf.getvalue()
        assert SAMPLE_TOKEN not in output, f"Token leaked: {output!r}"
        assert "REDACTED" in output
    finally:
        for h, s in original_streams:
            h.stream = s
