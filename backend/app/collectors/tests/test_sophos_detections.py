"""Sophos Detections collector — testes unitários do paradigma async 2-step.

Cobre:
- Criação de run quando cursor não tem run_id.
- Poll de status quando cursor já tem run_id (status queued → sem yield).
- Paginação de resultados quando run está finished.
- Avanço de cursor após paginar tudo (run_id=None, from_ts atualizado).
- extract_message_id com fallback.

Usa o mock interno ``_aiohttp_mock`` para mockar chamadas HTTP.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock

import aiohttp
import pytest
from ._aiohttp_mock import aioresponses

from ..base import CollectorContext
from ..vendors.sophos_detections import SophosDetectionsCollector

_BASE = "https://api-eu03.central.sophos.com/detections/v1/queries/detections"


class _NoopDomainLimiter:
    def slot(self, domain: str) -> Any:
        class _Ctx:
            async def __aenter__(self_inner) -> None:
                return None

            async def __aexit__(self_inner, *a: Any) -> bool:
                return False

        return _Ctx()


class _NoopRateLimiter:
    async def acquire(self, tenant_id: int, vendor: str) -> None:
        return None

    async def backoff(self, vendor: str, retry_after: int) -> None:
        return None


def _ctx(
    session: aiohttp.ClientSession,
    cursor: Dict[str, Any] | None = None,
) -> CollectorContext:
    return CollectorContext(
        integration_id=42,
        organization_id=7,
        platform="sophos",
        headers={"Authorization": "Bearer tok", "X-Region": "eu03", "X-Tenant-ID": "t-1"},
        session=session,
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
    )


def _detection(n: int) -> Dict[str, Any]:
    return {
        "id": f"det-{n:04d}",
        "detectionRule": "WIN-MITRE-T1055",
        "severity": 5,
        "type": "Threat",
        "time": "2026-04-23T10:00:00Z",
        "device": {"id": f"dev-{n}", "type": "computer", "entity": f"HOST-{n}"},
        "sensor": {"id": "SophosSensorID", "type": "cloud", "source": "Sophos", "version": "1.18.1"},
    }


# ── Testes ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_creates_run_when_no_cursor_run_id() -> None:
    """Sem run_id no cursor, POST /detections deve ser chamado.

    Após criar o run, o cursor deve ter run_id preenchido.
    Como o run retorna status "running" (não finished), nenhum evento
    é emitido — mas o run_id está persistido para o próximo ciclo.
    """
    with aioresponses() as m:
        # Step 1: cria run.
        m.post(
            _BASE,
            payload={"id": "run-abc", "status": "running", "resultCount": 0},
            status=200,
        )
        # Step 2: poll status — ainda não finished (max attempts = 1 neste teste).
        # Sobrescrevemos _MAX_POLL_ATTEMPTS para não dormir 3×5s em testes.
        m.get(
            f"{_BASE}/run-abc",
            payload={"id": "run-abc", "status": "running"},
            status=200,
        )
        m.get(f"{_BASE}/run-abc", payload={"id": "run-abc", "status": "running"}, status=200)
        m.get(f"{_BASE}/run-abc", payload={"id": "run-abc", "status": "running"}, status=200)

        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={})
            collector = SophosDetectionsCollector(ctx)

            # Sobrescreve POLL_ATTEMPTS para acelerar o teste.
            import backend.app.collectors.vendors.sophos_detections as mod_det
            original = mod_det._MAX_POLL_ATTEMPTS
            mod_det._MAX_POLL_ATTEMPTS = 1
            try:
                events: List[Dict[str, Any]] = []
                async for ev in collector.collect():
                    events.append(ev)
            finally:
                mod_det._MAX_POLL_ATTEMPTS = original

    # Run criado, mas não finished → sem eventos emitidos.
    assert events == []
    # Cursor deve ter run_id salvo para retomada.
    assert ctx.cursor is not None
    assert ctx.cursor.get("run_id") == "run-abc"


@pytest.mark.asyncio
async def test_collect_polls_when_cursor_has_run_id() -> None:
    """Cursor com run_id existente → pula criação de run e faz poll direto.

    Status "queued" → não finished → nenhum evento emitido.
    """
    with aioresponses() as m:
        # Somente poll — sem POST de criação.
        m.get(
            f"{_BASE}/existing-run",
            payload={"id": "existing-run", "status": "queued"},
            status=200,
        )
        m.get(f"{_BASE}/existing-run", payload={"id": "existing-run", "status": "queued"}, status=200)
        m.get(f"{_BASE}/existing-run", payload={"id": "existing-run", "status": "queued"}, status=200)

        async with aiohttp.ClientSession() as session:
            ctx = _ctx(
                session,
                cursor={"run_id": "existing-run", "from_ts": "2026-04-23T09:00:00Z", "page": 1},
            )
            collector = SophosDetectionsCollector(ctx)

            import backend.app.collectors.vendors.sophos_detections as mod_det
            original = mod_det._MAX_POLL_ATTEMPTS
            mod_det._MAX_POLL_ATTEMPTS = 1
            original_sleep = mod_det._POLL_INTERVAL_SECONDS
            mod_det._POLL_INTERVAL_SECONDS = 0.0
            try:
                events: List[Dict[str, Any]] = []
                async for ev in collector.collect():
                    events.append(ev)
            finally:
                mod_det._MAX_POLL_ATTEMPTS = original
                mod_det._POLL_INTERVAL_SECONDS = original_sleep

    # Sem eventos — run ainda em andamento.
    assert events == []
    # run_id preservado no cursor (não resetado para None).
    assert ctx.cursor is not None
    assert ctx.cursor.get("run_id") == "existing-run"


@pytest.mark.asyncio
async def test_collect_paginates_results_when_finished() -> None:
    """Run finished → pagina resultados e emite todos os eventos.

    2 páginas: primeira com página cheia (PAGE_SIZE itens), segunda com 1
    (página incompleta → fim de paginação).

    A condição de fim é ``len(items) < _RESULTS_PAGE_SIZE OR page >= total``.
    Página 1 precisa ter exatamente PAGE_SIZE itens para não disparar o
    break prematuro; a página 2 tem 1 item (incompleta) → fim.
    """
    import re

    import backend.app.collectors.vendors.sophos_detections as mod_det

    results_url_re = re.compile(
        r"https://api-eu03\.central\.sophos\.com/detections/v1/queries/detections/run-fin/results.*"
    )

    original_sleep = mod_det._POLL_INTERVAL_SECONDS
    original_page_size = mod_det._RESULTS_PAGE_SIZE
    mod_det._POLL_INTERVAL_SECONDS = 0.0
    # Reduz page_size para 2 para facilitar o teste sem 100 fixtures.
    mod_det._RESULTS_PAGE_SIZE = 2

    try:
        with aioresponses() as m:
            # Poll: run já finished.
            m.get(
                f"{_BASE}/run-fin",
                payload={"id": "run-fin", "status": "finished", "resultCount": 3},
                status=200,
            )
            # Página 1: 2 itens (= page_size, página cheia → continua).
            m.get(
                results_url_re,
                payload={
                    "items": [_detection(1), _detection(2)],
                    "pages": {"current": 1, "size": 2, "total": 2, "items": 2},
                },
                status=200,
            )
            # Página 2: 1 item (< page_size → fim de paginação).
            m.get(
                results_url_re,
                payload={
                    "items": [_detection(3)],
                    "pages": {"current": 2, "size": 2, "total": 2, "items": 1},
                },
                status=200,
            )

            async with aiohttp.ClientSession() as session:
                ctx = _ctx(
                    session,
                    cursor={"run_id": "run-fin", "from_ts": "2026-04-23T09:00:00Z", "page": 1},
                )
                collector = SophosDetectionsCollector(ctx)

                events: List[Dict[str, Any]] = []
                async for ev in collector.collect():
                    events.append(ev)
    finally:
        mod_det._POLL_INTERVAL_SECONDS = original_sleep
        mod_det._RESULTS_PAGE_SIZE = original_page_size

    assert len(events) == 3
    assert events[0]["id"] == "det-0001"
    assert events[2]["id"] == "det-0003"


@pytest.mark.asyncio
async def test_collect_advances_cursor_after_results() -> None:
    """Após paginar todos os resultados, cursor deve ter run_id=None
    e from_ts atualizado para o timestamp mais recente dos eventos.
    """
    import re

    detection_ts = "2026-04-23T15:00:00Z"
    results_url_re = re.compile(
        r"https://api-eu03\.central\.sophos\.com/detections/v1/queries/detections/run-done/results.*"
    )

    with aioresponses() as m:
        m.get(
            f"{_BASE}/run-done",
            payload={"id": "run-done", "status": "finished", "resultCount": 1},
            status=200,
        )
        ev = _detection(99)
        ev["time"] = detection_ts
        m.get(
            results_url_re,
            payload={
                "items": [ev],
                "pages": {"current": 1, "size": 100, "total": 1, "items": 1},
            },
            status=200,
        )

        async with aiohttp.ClientSession() as session:
            ctx = _ctx(
                session,
                cursor={"run_id": "run-done", "from_ts": "2026-04-23T09:00:00Z", "page": 1},
            )
            collector = SophosDetectionsCollector(ctx)

            import backend.app.collectors.vendors.sophos_detections as mod_det
            original_sleep = mod_det._POLL_INTERVAL_SECONDS
            mod_det._POLL_INTERVAL_SECONDS = 0.0
            try:
                events: List[Dict[str, Any]] = []
                async for ev_out in collector.collect():
                    events.append(ev_out)
            finally:
                mod_det._POLL_INTERVAL_SECONDS = original_sleep

    assert len(events) == 1

    # Cursor final: run_id resetado + from_ts avançado.
    assert ctx.cursor is not None
    assert ctx.cursor.get("run_id") is None, (
        "run_id deve ser None após paginar tudo — próximo ciclo cria run novo"
    )
    assert ctx.cursor.get("from_ts") == detection_ts, (
        f"from_ts deve avançar para {detection_ts!r} (timestamp mais recente do ciclo)"
    )
    assert ctx.cursor.get("page") == 1


def test_extract_message_id_uses_detection_id() -> None:
    """extract_message_id deve retornar o campo ``id`` do evento."""
    ctx = _ctx(MagicMock())  # type: ignore[arg-type]
    collector = SophosDetectionsCollector(ctx)

    assert collector.extract_message_id({"id": "abc-123"}) == "abc-123"
    # Fallback: sem id retorna string vazia (pipeline chama compute_message_id).
    assert collector.extract_message_id({}) == ""
    assert collector.extract_message_id({"other": "field"}) == ""
