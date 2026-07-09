"""Isolamento de poison event no sender Splunk HEC.

Prova com transporte HTTP fake/stub que:

(a) 1 poison event no meio do lote → exatamente esse vai para rejected e os
    demais são accepted (fallback individual após rejeição do lote).
(b) A capability registrada é "at_least_once" e NÃO "idempotent".
(c) 5xx no lote → retryable=True (sem fallback individual — erro transitório).
(d) 4xx no lote grande (> _MAX_INDIVIDUAL_FALLBACK) → todo lote rejected sem
    explosão de POSTs individuais.
(e) Fallback individual com 5xx num item individual → resultado retryable.

Estratégia: transporte HTTP stub via ``client._session = _stub_session(...)``
(idêntico ao padrão de test_wire_contract_elastic_bulk.py). O SplunkHecClient
é o sender REAL — nenhuma função do módulo é mockada.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.collectors.output.splunk_hec_sender import (  # noqa: E402
    SplunkHecClient,
    _MAX_INDIVIDUAL_FALLBACK,
)
from backend.app.collectors.output.destinations import registry  # noqa: E402


# ── Helpers de stub de transporte ────────────────────────────────────────────


def _resp(status: int, body: dict) -> MagicMock:
    """Stub de aiohttp.ClientResponse para uso como async context manager."""
    r = MagicMock()
    r.status = status
    r.json = AsyncMock(return_value=body)
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=None)
    return r


class _SequentialSession:
    """Sessão stub que retorna respostas em sequência por chamada a post().

    Uso: _SequentialSession([resp1, resp2, resp3]) → 1ª chamada retorna resp1,
    2ª retorna resp2, etc. Permite simular: batch-fail + individuais.
    """

    def __init__(self, responses: list[MagicMock]) -> None:
        self._queue = list(responses)
        self._calls: list[tuple[str, str]] = []  # (url, data)
        self.closed = False

    def post(self, url: str, *, data: str) -> MagicMock:
        self._calls.append((url, data))
        return self._queue.pop(0)

    async def close(self) -> None:
        self.closed = True


def _client() -> SplunkHecClient:
    return SplunkHecClient(
        url="https://splunk.test.local:8088",
        token="tok",
        index="centralops",
        sourcetype="centralops",
        verify_tls=False,
    )


def _ev(event_id: str, data: Any = None) -> dict[str, Any]:
    return {"_centralops": {"event_id": event_id}, "data": data or {}}


# ── (b) Capability────────────────────────────────────────────────


def test_capability_is_at_least_once_not_idempotent() -> None:
    """A capability registrada deve ser 'at_least_once',
    nunca 'idempotent' (o HEC não tem dedup nativo no sender)."""
    reg = registry.get("splunk_hec")
    assert "at_least_once" in reg.capabilities
    assert "idempotent" not in reg.capabilities, (
        "capability 'idempotent' é enganosa para splunk_hec: "
        "reentrega de lote pode duplicar eventos no Splunk"
    )


# ── (c) 5xx no lote → retryable sem fallback individual ─────────────────────


@pytest.mark.asyncio
async def test_batch_5xx_is_retryable_no_individual_fallback() -> None:
    """503 no lote inteiro → retryable=True, NENHUM POST individual feito.

    O fallback individual só é disparado em 4xx determinístico — não em
    erros transitórios (5xx/429), que devem ser retentados como lote inteiro.
    """
    session = _SequentialSession([_resp(503, {})])
    c = _client()
    c._session = session  # type: ignore[assignment]

    result = await c.send_batch([_ev("a"), _ev("b"), _ev("c")])

    assert result.retryable is True
    assert result.accepted == 0
    assert result.rejected == []
    # Apenas 1 POST (o do lote) — nenhum fallback individual.
    assert len(session._calls) == 1


@pytest.mark.asyncio
async def test_batch_429_is_retryable_no_individual_fallback() -> None:
    """429 no lote → retryable=True, sem fallback individual."""
    session = _SequentialSession([_resp(429, {})])
    c = _client()
    c._session = session  # type: ignore[assignment]

    result = await c.send_batch([_ev("x"), _ev("y")])

    assert result.retryable is True
    assert len(session._calls) == 1  # só o lote


# ── (a) Poison event no meio → fallback individual isola ────────────────────


@pytest.mark.asyncio
async def test_one_poison_in_middle_isolated_to_rejected() -> None:
    """3 eventos — o do meio é poison (400 no individual).

    Fluxo esperado:
    1. POST do lote [a, b, c] → 400 (lote rejeitado).
    2. Fallback: POST individual de 'a' → 200 (aceito).
    3. Fallback: POST individual de 'b' → 400 (poison — rejected).
    4. Fallback: POST individual de 'c' → 200 (aceito).

    Resultado: accepted=2, rejected=[b], retryable=False.
    """
    batch_fail = _resp(400, {"text": "Invalid data format", "code": 6})
    ind_a_ok = _resp(200, {"text": "Success", "code": 0})
    ind_b_fail = _resp(400, {"text": "Bad field in event b", "code": 6})
    ind_c_ok = _resp(200, {"text": "Success", "code": 0})

    session = _SequentialSession([batch_fail, ind_a_ok, ind_b_fail, ind_c_ok])
    c = _client()
    c._session = session  # type: ignore[assignment]

    result = await c.send_batch([_ev("a"), _ev("b"), _ev("c")])

    assert result.accepted == 2
    assert len(result.rejected) == 1
    assert result.rejected[0].event_id == "b"
    assert result.rejected[0].error_kind == "schema_rejected"
    assert "Bad field" in result.rejected[0].reason
    assert result.retryable is False
    # Total: 1 lote + 3 individuais = 4 POSTs.
    assert len(session._calls) == 4


@pytest.mark.asyncio
async def test_all_events_poison_all_rejected() -> None:
    """Todos os eventos do lote são poison → accepted=0, rejected=[todos]."""
    batch_fail = _resp(400, {"text": "bad", "code": 6})
    ind_a_fail = _resp(400, {"text": "err a", "code": 6})
    ind_b_fail = _resp(400, {"text": "err b", "code": 6})

    session = _SequentialSession([batch_fail, ind_a_fail, ind_b_fail])
    c = _client()
    c._session = session  # type: ignore[assignment]

    result = await c.send_batch([_ev("a"), _ev("b")])

    assert result.accepted == 0
    assert len(result.rejected) == 2
    assert {r.event_id for r in result.rejected} == {"a", "b"}
    assert result.retryable is False


@pytest.mark.asyncio
async def test_single_event_4xx_goes_to_rejected_directly() -> None:
    """Lote de 1 evento → 4xx no lote → fallback individual → 4xx → rejected."""
    batch_fail = _resp(403, {"text": "Token disabled", "code": 4})
    ind_fail = _resp(403, {"text": "Token disabled", "code": 4})

    session = _SequentialSession([batch_fail, ind_fail])
    c = _client()
    c._session = session  # type: ignore[assignment]

    result = await c.send_batch([_ev("solo")])

    assert result.accepted == 0
    assert len(result.rejected) == 1
    assert result.rejected[0].event_id == "solo"
    assert result.rejected[0].error_kind == "auth"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_poison_first_event_rest_accepted() -> None:
    """Poison no início: o primeiro evento é rejeitado, os demais aceitos."""
    batch_fail = _resp(400, {"text": "bad first", "code": 6})
    ind_1_fail = _resp(400, {"text": "bad first", "code": 6})
    ind_2_ok = _resp(200, {"text": "Success", "code": 0})
    ind_3_ok = _resp(200, {"text": "Success", "code": 0})

    session = _SequentialSession([batch_fail, ind_1_fail, ind_2_ok, ind_3_ok])
    c = _client()
    c._session = session  # type: ignore[assignment]

    result = await c.send_batch([_ev("p1"), _ev("p2"), _ev("p3")])

    assert result.accepted == 2
    assert len(result.rejected) == 1
    assert result.rejected[0].event_id == "p1"
    assert result.retryable is False


# ── (d) Lote grande → rejected no atacado (sem explosão de POSTs) ─────────


@pytest.mark.asyncio
async def test_large_batch_4xx_rejected_wholesale_no_individual_posts() -> None:
    """Lote > _MAX_INDIVIDUAL_FALLBACK + 4xx → todo lote rejected,
    NENHUM POST individual (evita explosão de N POSTs)."""
    batch_fail = _resp(400, {"text": "invalid", "code": 6})

    session = _SequentialSession([batch_fail])
    c = _client()
    c._session = session  # type: ignore[assignment]

    # Lote maior que o limite de fallback individual.
    big_batch = [_ev(f"ev-{i}") for i in range(_MAX_INDIVIDUAL_FALLBACK + 1)]
    result = await c.send_batch(big_batch)

    assert result.accepted == 0
    assert len(result.rejected) == len(big_batch)
    assert result.retryable is False
    # Apenas 1 POST (o do lote) — sem fallback individual.
    assert len(session._calls) == 1


# ── (e) Individual 5xx → resultado retryable ────────────────────────────────


@pytest.mark.asyncio
async def test_individual_5xx_in_fallback_makes_result_retryable() -> None:
    """Fallback individual: um evento recebe 503 → resultado retryable=True.

    Os já-aceitos podem ser re-enviados (at_least_once — sem dedup automático).
    """
    batch_fail = _resp(400, {"text": "bad batch", "code": 6})
    ind_a_ok = _resp(200, {"text": "Success", "code": 0})
    ind_b_transient = _resp(503, {})

    session = _SequentialSession([batch_fail, ind_a_ok, ind_b_transient])
    c = _client()
    c._session = session  # type: ignore[assignment]

    result = await c.send_batch([_ev("a"), _ev("b")])

    # Retryable porque 'b' recebeu 503 no individual.
    assert result.retryable is True
    # 'a' foi aceito no individual antes do 503 de 'b'.
    assert result.accepted == 1
    # Nenhum rejected determinístico.
    assert result.rejected == []


# ── Teste de auth 401 com fallback ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_401_triggers_fallback_classified_as_auth() -> None:
    """401 no lote → fallback individual → 401 por item → error_kind='auth'."""
    batch_fail = _resp(401, {"text": "Unauthorized", "code": 3})
    ind_fail = _resp(401, {"text": "Unauthorized", "code": 3})

    session = _SequentialSession([batch_fail, ind_fail])
    c = _client()
    c._session = session  # type: ignore[assignment]

    result = await c.send_batch([_ev("only")])

    assert result.accepted == 0
    assert len(result.rejected) == 1
    assert result.rejected[0].error_kind == "auth"
    assert result.retryable is False


# ── event_id no fallback sem _centralops usa '?'────────────────────────


@pytest.mark.asyncio
async def test_fallback_event_without_id_uses_question_mark() -> None:
    """Evento sem _centralops.event_id → rejected com event_id='?' no fallback."""
    ev_no_id = {"data": "payload sem event_id"}
    batch_fail = _resp(400, {"text": "bad", "code": 6})
    ind_fail = _resp(400, {"text": "bad", "code": 6})

    session = _SequentialSession([batch_fail, ind_fail])
    c = _client()
    c._session = session  # type: ignore[assignment]

    result = await c.send_batch([ev_no_id])

    assert len(result.rejected) == 1
    assert result.rejected[0].event_id == "?"


# ── Batch vazio não faz POST ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_batch_no_post_no_fallback() -> None:
    """Lote vazio → ok(0) imediato, nenhum POST (nem lote nem fallback)."""
    session = _SequentialSession([])
    c = _client()
    c._session = session  # type: ignore[assignment]

    result = await c.send_batch([])

    assert result.accepted == 0
    assert result.rejected == []
    assert result.retryable is False
    assert len(session._calls) == 0
