"""WIRE CONTRACT + behaviour for kind ``elastic_bulk``.

Two surfaces contracted (mirrors test_wire_contract_splunk_hec.py):

1. The exact on-wire ``_bulk`` NDJSON bytes ``send_batch`` POSTs: alternating
   **action line** + **document line**, compact separators, ``ensure_ascii=False``,
   joined by ``\\n`` WITH a mandatory trailing newline (the ``_bulk`` API rejects a
   body without it). Captured from a fully-mocked session — no sockets.

2. The per-item response mapping → ``DeliveryResult``: the ``_bulk`` API returns
   ``items[].status``/``error`` per document, so a poison doc is rejected by
   itself (partial-batch) while the rest are accepted; a ``409`` version
   conflict (create with an existing ``_id``) is the IDEMPOTENCY signal and
   counts as accepted, not error.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.collectors.output.elastic_bulk_sender import (  # noqa: E402
    ElasticBulkClient,
    format_bulk_action,
)


def _mock_response(status: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _mock_session(response: MagicMock) -> MagicMock:
    session = MagicMock()
    session.closed = False
    session.post = MagicMock(return_value=response)
    session.get = MagicMock(return_value=response)
    session.close = AsyncMock()
    return session


async def _capture_wire(client: ElasticBulkClient, batch: list) -> str:
    """Drive REAL send_batch through a mocked session (errors=false happy path)
    and return the exact ``data=`` string handed to ``session.post``."""
    resp = _mock_response(200, {"errors": False, "items": []})
    session = _mock_session(resp)
    client._session = session
    result = await client.send_batch(batch)
    assert result.accepted == len(batch)
    return session.post.call_args.kwargs["data"]


_CANONICAL = {
    "_centralops": {"vendor": "sophos", "event_id": "evt-abc123"},
    "data": {"id": "evt-1", "severity": "Critical"},
}


def _client(index: str = "centralops") -> ElasticBulkClient:
    return ElasticBulkClient(
        url="https://es.test.local:9200/", secret="k", index=index, verify_tls=False
    )


# ── PART 1 — action line + format() dict ────────────────────────────────────


def test_action_uses_create_with_event_id() -> None:
    """event_id present → ``create`` + ``_id`` (idempotent dedup)."""
    assert format_bulk_action(_CANONICAL, index="centralops") == {
        "create": {"_index": "centralops", "_id": "evt-abc123"}
    }


def test_action_falls_back_to_index_without_event_id() -> None:
    """No event_id → ``index`` (cluster generates the id)."""
    assert format_bulk_action({"data": 1}, index="logs") == {"index": {"_index": "logs"}}
    assert format_bulk_action({"_centralops": {"event_id": ""}}, index="logs") == {
        "index": {"_index": "logs"}
    }


def test_format_returns_source_doc_verbatim() -> None:
    """format() = the document as indexed (the envelope), for preview/tap."""
    assert _client().format(_CANONICAL) == _CANONICAL


# ── PART 2 — on-wire NDJSON bytes ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_wire_single_event_exact_bytes() -> None:
    """One event → action line + doc line + TRAILING newline, byte-for-byte."""
    wire = await _capture_wire(_client(), [_CANONICAL])
    expected = (
        '{"create":{"_index":"centralops","_id":"evt-abc123"}}\n'
        '{"_centralops":{"vendor":"sophos","event_id":"evt-abc123"},'
        '"data":{"id":"evt-1","severity":"Critical"}}\n'
    )
    assert wire == expected
    # Mandatory trailing newline; exactly 2 lines of content.
    assert wire.endswith("\n")
    assert wire.count("\n") == 2


@pytest.mark.asyncio
async def test_wire_two_events_four_lines() -> None:
    """N events → 2N NDJSON lines (action, doc, action, doc) + trailing \\n."""
    ev2 = {"_centralops": {"event_id": "evt-2"}, "data": {"id": "evt-2"}}
    wire = await _capture_wire(_client(), [_CANONICAL, ev2])
    lines = wire.rstrip("\n").split("\n")
    assert len(lines) == 4
    # action/doc/action/doc interleave; each line independently parseable.
    assert json.loads(lines[0]) == {"create": {"_index": "centralops", "_id": "evt-abc123"}}
    assert json.loads(lines[2]) == {"create": {"_index": "centralops", "_id": "evt-2"}}
    assert json.loads(lines[3]) == ev2
    # The whole body is NOT a JSON array.
    with pytest.raises(json.JSONDecodeError):
        json.loads(wire)


@pytest.mark.asyncio
async def test_wire_unicode_raw_utf8_and_escaped_newline() -> None:
    """ensure_ascii=False (raw glyphs) + embedded newline escaped so it can't
    split a record into a spurious NDJSON line."""
    ev = {"_centralops": {"event_id": "u1"}, "msg": "café—日本語\nL2"}
    wire = await _capture_wire(_client(), [ev])
    assert "\\u" not in wire
    assert '"msg":"café—日本語\\nL2"' in wire
    # 1 event → action+doc+trailing = exactly 2 newlines (no extra from the \n in msg).
    assert wire.count("\n") == 2


@pytest.mark.asyncio
async def test_wire_empty_batch_no_post() -> None:
    client = _client()
    session = _mock_session(_mock_response(200, {"errors": False}))
    client._session = session
    result = await client.send_batch([])
    assert result.accepted == 0
    session.post.assert_not_called()


# ── PART 3 — per-item response → DeliveryResult──────────────────────


@pytest.mark.asyncio
async def test_all_accepted_when_no_errors() -> None:
    client = _client()
    client._session = _mock_session(_mock_response(200, {"errors": False, "items": []}))
    result = await client.send_batch([_CANONICAL, {"_centralops": {"event_id": "e2"}}])
    assert result.accepted == 2
    assert result.rejected == []


@pytest.mark.asyncio
async def test_partial_batch_one_poison_doc_rejected_rest_accepted() -> None:
    """doc 2 is a mapper error (400) → rejected alone; docs 1 and 3 accepted."""
    client = _client()
    body = {
        "errors": True,
        "items": [
            {"create": {"_index": "centralops", "_id": "a", "status": 201}},
            {"create": {"_index": "centralops", "_id": "b", "status": 400,
                        "error": {"type": "mapper_parsing_exception", "reason": "bad field"}}},
            {"create": {"_index": "centralops", "_id": "c", "status": 201}},
        ],
    }
    client._session = _mock_session(_mock_response(200, body))
    batch = [
        {"_centralops": {"event_id": "a"}},
        {"_centralops": {"event_id": "b"}},
        {"_centralops": {"event_id": "c"}},
    ]
    result = await client.send_batch(batch)
    assert result.accepted == 2
    assert len(result.rejected) == 1
    assert result.rejected[0].event_id == "b"
    assert result.rejected[0].error_kind == "schema_rejected"
    assert "bad field" in result.rejected[0].reason
    assert result.retryable is False


@pytest.mark.asyncio
async def test_version_conflict_is_idempotent_accept() -> None:
    """409 version_conflict (create on existing _id) = already delivered →
    counted ACCEPTED, never rejected (a retried batch must not DLQ)."""
    client = _client()
    body = {
        "errors": True,
        "items": [
            {"create": {"_index": "centralops", "_id": "a", "status": 409,
                        "error": {"type": "version_conflict_engine_exception"}}},
        ],
    }
    client._session = _mock_session(_mock_response(200, body))
    result = await client.send_batch([{"_centralops": {"event_id": "a"}}])
    assert result.accepted == 1
    assert result.rejected == []


@pytest.mark.asyncio
async def test_item_429_makes_batch_retryable() -> None:
    """A 429 at item level → batch retryable (accepted docs re-enter as 409)."""
    client = _client()
    body = {
        "errors": True,
        "items": [
            {"create": {"_index": "centralops", "_id": "a", "status": 201}},
            {"create": {"_index": "centralops", "_id": "b", "status": 429,
                        "error": {"type": "es_rejected_execution_exception"}}},
        ],
    }
    client._session = _mock_session(_mock_response(200, body))
    result = await client.send_batch([
        {"_centralops": {"event_id": "a"}}, {"_centralops": {"event_id": "b"}}
    ])
    assert result.retryable is True
    assert result.accepted == 1


@pytest.mark.asyncio
async def test_retryable_with_accepted_is_idempotent_via_create_id() -> None:
    """locks the dispatcher-guard premise (pipeline.py ~1085): elastic_bulk
    returns retryable=True WITH accepted>0 on item-level 429, which trips the
    'whole-batch retry would DUPLICATE accepted events' guard. It is SAFE *only*
    because the accepted docs were written with create+_id, so the autoretry
    re-sends them as 409 (idempotent), never duplicating. If a future change drops
    create+_id (e.g. switches to index without _id), this test fails — the guard
    would then be a real duplication bug."""
    client = _client()
    batch = [{"_centralops": {"event_id": "a"}}, {"_centralops": {"event_id": "b"}}]

    # cycle 1: doc a accepted (201), doc b throttled (429) → retryable, accepted=1
    body1 = {"errors": True, "items": [
        {"create": {"_index": "centralops", "_id": "a", "status": 201}},
        {"create": {"_index": "centralops", "_id": "b", "status": 429,
                    "error": {"type": "es_rejected_execution_exception"}}},
    ]}
    session = _mock_session(_mock_response(200, body1))
    client._session = session
    r1 = await client.send_batch(batch)
    assert r1.retryable is True and r1.accepted == 1
    # BOTH action lines used create+_id → the whole-batch retry is 409-idempotent.
    wire = session.post.call_args.kwargs["data"]
    assert wire.count('"create":{"_index"') == 2
    assert '"_id":"a"' in wire and '"_id":"b"' in wire

    # cycle 2 (the autoretry re-sends the WHOLE batch): a now 409 (already there),
    # b now 201 → accepted=2, NO duplication, NO rejected.
    body2 = {"errors": True, "items": [
        {"create": {"_index": "centralops", "_id": "a", "status": 409,
                    "error": {"type": "version_conflict_engine_exception"}}},
        {"create": {"_index": "centralops", "_id": "b", "status": 201}},
    ]}
    client._session = _mock_session(_mock_response(200, body2))
    r2 = await client.send_batch(batch)
    assert r2.accepted == 2 and r2.rejected == [] and r2.retryable is False


@pytest.mark.asyncio
async def test_short_items_array_retryable_no_silent_loss() -> None:
    """Um items[] mais CURTO que o lote (resposta truncada) NÃO
    pode descartar eventos silenciosamente — reconcilia p/ retry do lote."""
    client = _client()
    body = {"errors": True, "items": [
        {"create": {"_index": "centralops", "_id": "a", "status": 201}},
        # falta o item do 2º evento (resposta truncada)
    ]}
    client._session = _mock_session(_mock_response(200, body))
    result = await client.send_batch([
        {"_centralops": {"event_id": "a"}}, {"_centralops": {"event_id": "b"}}
    ])
    assert result.retryable is True
    assert result.accepted == 0
    assert result.rejected == []  # nada perdido nem falso-aceito


@pytest.mark.asyncio
async def test_request_level_503_retryable() -> None:
    client = _client()
    client._session = _mock_session(_mock_response(503, {}))
    result = await client.send_batch([_CANONICAL])
    assert result.retryable is True
    assert result.accepted == 0


@pytest.mark.asyncio
async def test_auth_401_rejects_whole_batch_non_retryable() -> None:
    client = _client()
    client._session = _mock_session(_mock_response(401, {}))
    result = await client.send_batch([_CANONICAL])
    assert result.retryable is False
    assert len(result.rejected) == 1
    assert result.rejected[0].error_kind == "auth"


# ── PART 4 — auth header schemes + test() probe ─────────────────────────────


def test_api_key_header() -> None:
    c = ElasticBulkClient(url="https://es:9200", secret="abc", auth_scheme="api_key")
    assert c._auth_header() == "ApiKey abc"


def test_basic_auth_header_is_base64() -> None:
    import base64

    c = ElasticBulkClient(url="https://es:9200", secret="user:pass", auth_scheme="basic")
    assert c._auth_header() == "Basic " + base64.b64encode(b"user:pass").decode()


def test_no_secret_no_auth_header() -> None:
    assert ElasticBulkClient(url="https://es:9200", secret=None)._auth_header() is None


@pytest.mark.asyncio
async def test_probe_ok_on_cluster_health_200() -> None:
    client = _client()
    client._session = _mock_session(_mock_response(200, {"status": "green"}))
    result = await client.test()
    assert result.ok is True
    assert "green" in result.detail


@pytest.mark.asyncio
async def test_probe_fails_on_401() -> None:
    client = _client()
    client._session = _mock_session(_mock_response(401, {}))
    result = await client.test()
    assert result.ok is False


# ── PART 5 — erase() wire contract (delete by _id via _bulk)─────────────


@pytest.mark.asyncio
async def test_erase_empty_event_ids_no_request() -> None:
    """erase([]) → immediate success without contacting the cluster."""
    client = _client()
    session = _mock_session(_mock_response(200, {}))
    client._session = session
    result = await client.erase([])
    assert result.ok
    assert result.erased == []
    assert result.failed == []
    session.post.assert_not_called()


@pytest.mark.asyncio
async def test_erase_builds_delete_action_ndjson() -> None:
    """erase() sends ONE delete-action line per id (no doc line, unlike index/create)."""
    client = _client()
    # _bulk delete: errors=False → all deleted.
    body = {
        "errors": False,
        "items": [
            {"delete": {"_index": "centralops", "_id": "id-1", "status": 200}},
            {"delete": {"_index": "centralops", "_id": "id-2", "status": 200}},
        ],
    }
    session = _mock_session(_mock_response(200, body))
    client._session = session

    result = await client.erase(["id-1", "id-2"])

    # Verify it's a success.
    assert result.ok
    assert set(result.erased) == {"id-1", "id-2"}
    assert result.failed == []

    # Verify the exact wire: 2 delete-action lines (no doc line) + trailing newline.
    wire: str = session.post.call_args.kwargs["data"]
    lines = wire.rstrip("\n").split("\n")
    assert len(lines) == 2, f"expected 2 action-only lines, got {len(lines)}: {lines}"
    action1 = json.loads(lines[0])
    action2 = json.loads(lines[1])
    assert action1 == {"delete": {"_index": "centralops", "_id": "id-1"}}
    assert action2 == {"delete": {"_index": "centralops", "_id": "id-2"}}
    assert wire.endswith("\n")


@pytest.mark.asyncio
async def test_erase_404_counts_as_erased_idempotent() -> None:
    """404 from delete = document already gone = idempotent → counted as erased."""
    client = _client()
    body = {
        "errors": True,
        "items": [
            {"delete": {"_index": "centralops", "_id": "id-1", "status": 200}},
            {"delete": {"_index": "centralops", "_id": "id-gone", "status": 404,
                        "error": {"type": "not_found"}}},
        ],
    }
    session = _mock_session(_mock_response(200, body))
    client._session = session

    result = await client.erase(["id-1", "id-gone"])
    assert result.ok
    assert set(result.erased) == {"id-1", "id-gone"}
    assert result.failed == []


@pytest.mark.asyncio
async def test_erase_partial_failure_captured() -> None:
    """A 500-status item is captured in failed without raising."""
    client = _client()
    body = {
        "errors": True,
        "items": [
            {"delete": {"_index": "centralops", "_id": "id-ok", "status": 200}},
            {"delete": {"_index": "centralops", "_id": "id-bad", "status": 500,
                        "error": {"reason": "shard failure"}}},
        ],
    }
    session = _mock_session(_mock_response(200, body))
    client._session = session

    result = await client.erase(["id-ok", "id-bad"])
    assert not result.ok
    assert result.erased == ["id-ok"]
    assert result.failed == ["id-bad"]


@pytest.mark.asyncio
async def test_erase_request_level_503_retryable_captured() -> None:
    """HTTP 503 at request level → all ids go to failed (not raised)."""
    client = _client()
    client._session = _mock_session(_mock_response(503, {}))
    result = await client.erase(["id-1"])
    assert not result.ok
    assert result.failed == ["id-1"]
    assert result.erased == []


def test_elastic_bulk_declares_erasure_capability() -> None:
    """DestinationRegistration for elastic_bulk must include 'erasure' capability."""
    from backend.app.collectors.output.destinations import registry as _registry

    reg = _registry.get("elastic_bulk")
    assert "erasure" in reg.capabilities, (
        "elastic_bulk must declare 'erasure' capability for right-to-erasure"
    )


def test_elastic_bulk_declares_erasure_by_query_capability() -> None:
    """elastic_bulk deve declarar 'erasure_by_query' para cobrir dados ENTREGUES."""
    from backend.app.collectors.output.destinations import registry as _registry

    reg = _registry.get("elastic_bulk")
    assert "erasure_by_query" in reg.capabilities, (
        "elastic_bulk deve declarar 'erasure_by_query' para purge LGPD via _delete_by_query"
    )


# ── PART 6 — erase(filter=) via _delete_by_query───────────────────────────


def _mock_post_response(status: int, body: dict) -> MagicMock:
    """Constrói um mock de resposta que suporta json() e context manager."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


@pytest.mark.asyncio
async def test_erase_by_filter_org_id_emits_delete_by_query() -> None:
    """erase(filter={organization_id}) → POST {index}/_delete_by_query com term query correta."""
    client = _client(index="centralops")
    dbq_body = {"deleted": 42, "failures": [], "timed_out": False}
    resp = _mock_post_response(200, dbq_body)
    session = _mock_session(resp)
    client._session = session

    result = await client.erase([], filter={"organization_id": 99})

    assert result.ok
    assert "42" in result.detail
    assert "99" in result.detail

    # Verifica endpoint: POST .../centralops/_delete_by_query
    call_args = session.post.call_args
    url_called = call_args[0][0] if call_args[0] else call_args.kwargs.get("url", "")
    # Se não há positional, a URL pode ser kwarg — verifica no call_args completo.
    # A _mock_session usa session.post(url, data=...) — 1º arg posicional.
    assert "centralops/_delete_by_query" in url_called, (
        f"URL deve incluir '{{index}}/_delete_by_query', mas chamou: {url_called!r}"
    )

    # Verifica body: {"query": {"term": {"_centralops.organization_id": 99}}}
    raw_data = call_args.kwargs.get("data") or (call_args[1].get("data") if call_args[1] else None)
    body = json.loads(raw_data)
    assert body == {
        "query": {"term": {"_centralops.organization_id": 99}}
    }, f"body inesperado: {body}"


@pytest.mark.asyncio
async def test_erase_by_filter_shard_failures_go_to_failed() -> None:
    """Falhas de shard no _delete_by_query → resultado partial (failed não-vazio)."""
    client = _client()
    dbq_body = {
        "deleted": 5,
        "failures": [{"shard": 0, "reason": "disk full"}, {"shard": 1, "reason": "disk full"}],
        "timed_out": False,
    }
    resp = _mock_post_response(200, dbq_body)
    client._session = _mock_session(resp)

    result = await client.erase([], filter={"organization_id": 7})

    assert not result.ok
    assert result.erased  # parcialmente apagados
    assert result.failed  # shard failures
    assert "2 falhas de shard" in result.detail


@pytest.mark.asyncio
async def test_erase_by_filter_503_captured_as_failed() -> None:
    """HTTP 503 no _delete_by_query → failed com detail descritivo, sem raise."""
    client = _client()
    resp = _mock_post_response(503, {})
    client._session = _mock_session(resp)

    result = await client.erase([], filter={"organization_id": 3})

    assert not result.ok
    assert result.failed
    assert "503" in result.detail


@pytest.mark.asyncio
async def test_erase_combines_ids_and_filter_results() -> None:
    """erase(event_ids, filter=) combina resultado do _bulk delete com _delete_by_query."""
    client = _client(index="centralops")

    # Configuramos o mock para retornar respostas diferentes por URL chamada.
    bulk_resp = _mock_post_response(
        200,
        {
            "errors": False,
            "items": [
                {"delete": {"_index": "centralops", "_id": "id-1", "status": 200}},
            ],
        },
    )
    dbq_resp = _mock_post_response(200, {"deleted": 10, "failures": [], "timed_out": False})

    call_count = 0
    responses = [bulk_resp, dbq_resp]

    def _side_effect(url: str, **kwargs: object) -> MagicMock:
        nonlocal call_count
        r = responses[call_count]
        call_count += 1
        return r

    session = MagicMock()
    session.closed = False
    session.post = MagicMock(side_effect=_side_effect)
    session.close = AsyncMock()
    client._session = session

    result = await client.erase(["id-1"], filter={"organization_id": 5})

    # Dois POSTs: 1º _bulk delete, 2º _delete_by_query.
    assert session.post.call_count == 2

    # Resultado combinado: id-1 apagado via _bulk + marcador da query.
    assert "id-1" in result.erased
    assert any("deleted:10" in e for e in result.erased)
    assert result.failed == []
    assert result.ok


@pytest.mark.asyncio
async def test_erase_filter_without_organization_id_no_query() -> None:
    """filter sem organization_id não emite _delete_by_query (filter parcial/futuro)."""
    client = _client()
    # Apenas event_ids — filter vazio → só _bulk delete, sem _delete_by_query.
    resp = _mock_post_response(
        200,
        {
            "errors": False,
            "items": [{"delete": {"_index": "centralops", "_id": "id-x", "status": 200}}],
        },
    )
    session = _mock_session(resp)
    client._session = session

    result = await client.erase(["id-x"], filter={})

    # Apenas 1 POST (_bulk delete), sem _delete_by_query.
    assert session.post.call_count == 1
    assert result.ok
    assert "id-x" in result.erased


@pytest.mark.asyncio
async def test_erase_no_ids_no_filter_is_noop() -> None:
    """erase([], filter=None) → noop, sem POST, resultado ok vazio."""
    client = _client()
    session = _mock_session(_mock_response(200, {}))
    client._session = session

    result = await client.erase([])

    assert result.ok
    assert result.erased == []
    assert result.failed == []
    session.post.assert_not_called()
