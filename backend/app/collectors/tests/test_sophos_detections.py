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


# ── Janela parada na última detection (incidente out/2026) ────────────────────
#
# ``from`` só aceita segundos inteiros: uma detection às 17:06:39.978 fazia o run
# seguinte começar em 17:06:39 e trazê-la de novo, ciclo após ciclo.

_STUCK_TS = "2026-10-15T17:06:39.978Z"


async def _detections_cycle(cursor: Dict[str, Any] | None, *items: Dict[str, Any]):
    """Ciclo completo (POST run → poll finished → results). Devolve
    (eventos, cursor gravado, ``from`` enviado no POST)."""
    import re

    results_re = re.compile(rf"^{re.escape(_BASE)}/run-1/results.*")
    with aioresponses() as m:
        m.post(_BASE, payload={"id": "run-1", "status": "running"}, status=200)
        m.get(f"{_BASE}/run-1", payload={"id": "run-1", "status": "finished"}, status=200)
        m.get(
            results_re,
            payload={"items": list(items), "pages": {"current": 1, "total": 1}},
            status=200,
        )
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor=cursor)
            events = [ev async for ev in SophosDetectionsCollector(ctx).collect()]
        calls = [c for calls in m.requests.values() for c in calls]
        body = next(c.kwargs["json"] for c in calls if "json" in c.kwargs)
    return events, ctx.cursor, body["from"]


@pytest.mark.asyncio
async def test_last_detection_does_not_come_back_every_cycle() -> None:
    stuck = dict(_detection(1), time=_STUCK_TS)
    start = {"run_id": None, "from_ts": "2026-10-15T17:00:00Z", "page": 1}

    emitted_1, cursor_1, _ = await _detections_cycle(start, stuck)
    assert [e["id"] for e in emitted_1] == ["det-0001"]
    assert cursor_1["from_ts"] == "2026-10-15T17:06:39Z"

    emitted_2, cursor_2, from_2 = await _detections_cycle(cursor_1, stuck)
    # A janela recomeça no mesmo segundo (nada se perde) …
    assert from_2 == "2026-10-15T17:06:39Z"
    # … mas a detection já emitida NÃO sai de novo.
    assert emitted_2 == []

    emitted_3, cursor_3, _ = await _detections_cycle(cursor_2, stuck)
    assert emitted_3 == []
    assert cursor_3 == cursor_2


@pytest.mark.asyncio
async def test_new_detection_in_the_same_second_is_emitted_old_one_skipped() -> None:
    stuck = dict(_detection(1), time=_STUCK_TS)
    late = dict(_detection(2), time="2026-10-15T17:06:39.990Z")
    start = {"run_id": None, "from_ts": "2026-10-15T17:00:00Z", "page": 1}

    _, cursor_1, _ = await _detections_cycle(start, stuck)
    emitted, cursor_2, _ = await _detections_cycle(cursor_1, stuck, late)

    assert [e["id"] for e in emitted] == ["det-0002"]
    assert cursor_2["boundary"]["keys"] == ["det-0001", "det-0002"]


@pytest.mark.asyncio
async def test_pending_run_cursor_keeps_the_boundary_memory() -> None:
    """Run que não termina no ciclo: o cursor gravado ao criar o run leva a
    memória da MESMA janela, senão a retomada reemitiria a detection."""
    import backend.app.collectors.vendors.sophos_detections as mod_det

    boundary = {"at": "2026-10-15T17:06:39Z", "keys": ["det-0001"]}
    with aioresponses() as m:
        m.post(_BASE, payload={"id": "run-1", "status": "running"}, status=200)
        m.get(f"{_BASE}/run-1", payload={"id": "run-1", "status": "running"}, status=200)
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(
                session,
                cursor={
                    "run_id": None,
                    "from_ts": "2026-10-15T17:06:39Z",
                    "page": 1,
                    "boundary": boundary,
                },
            )
            original = mod_det._MAX_POLL_ATTEMPTS
            mod_det._MAX_POLL_ATTEMPTS = 1
            try:
                events = [ev async for ev in SophosDetectionsCollector(ctx).collect()]
            finally:
                mod_det._MAX_POLL_ATTEMPTS = original

    assert events == []
    assert ctx.cursor == {
        "run_id": "run-1",
        "from_ts": "2026-10-15T17:06:39Z",
        "page": 1,
        "boundary": boundary,
    }


# ── Janela dimensionada e orçamento do ciclo (incidente nov/2026) ─────────────
#
# Um tenant ficou 47 h atrasado sem um único erro: o run cobria "do cursor até
# agora", o watermark só andava quando o run inteiro era lido, e ele nunca era —
# 2.500 detections por ciclo contra um run que crescia a cada recomeço.

import re as _re
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

import backend.app.collectors.vendors.sophos_detections as _mod


def _iso(dt: _dt) -> str:
    return dt.astimezone(_tz.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ago(**kw: float) -> str:
    return _iso(_dt.now(_tz.utc) - _td(**kw))


def _secs(a: str, b: str) -> float:
    parse = lambda s: _dt.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
    return (parse(b) - parse(a)).total_seconds()


def _results_re(run_id: str) -> "_re.Pattern[str]":
    return _re.compile(rf"^{_re.escape(_BASE)}/{_re.escape(run_id)}/results.*")


async def _cycle(cursor, script, *, bounded: bool = True, monkeypatch=None, **consts):
    """Roda UM ciclo contra um roteiro de respostas, na ordem.

    ``script``: ``("post", payload)``, ``("run", run_id, payload)`` (poll) ou
    ``("results", run_id, payload_ou_status)``. Devolve (eventos, ctx, corpos
    dos POST, [(run_id, page, pageSize)] dos pedidos de resultado).
    """
    for name, value in consts.items():
        monkeypatch.setattr(_mod, name, value)
    with aioresponses() as m:
        for step in script:
            if step[0] == "post":
                m.post(_BASE, payload=step[1], status=200)
            elif step[0] == "run":
                if isinstance(step[2], int):
                    m.get(f"{_BASE}/{step[1]}", status=step[2], body="{}")
                else:
                    m.get(f"{_BASE}/{step[1]}", payload=step[2], status=200)
            elif isinstance(step[2], int):
                m.get(_results_re(step[1]), status=step[2], body='{"error":"x"}')
            else:
                m.get(_results_re(step[1]), payload=step[2], status=200)
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor=cursor)
            ctx.bounded_per_cycle = bounded
            events = [ev async for ev in SophosDetectionsCollector(ctx).collect()]
        posts, fetched = [], []
        for (method, url), calls in m.requests.items():
            for call in calls:
                if method == "POST":
                    posts.append(call.kwargs["json"])
                elif url.path.endswith("/results"):
                    fetched.append(
                        (url.path.split("/")[-2], int(url.query["page"]), int(url.query["pageSize"]))
                    )
    return events, ctx, posts, fetched


def _page(*items: Dict[str, Any], total: int | None = 1, **pages: Any) -> Dict[str, Any]:
    meta: Dict[str, Any] = dict(pages)
    if total is not None:
        meta["total"] = total
    return {"items": list(items), "pages": meta}


def _det(n: int, time: str) -> Dict[str, Any]:
    return dict(_detection(n), time=time)


@pytest.mark.asyncio
async def test_run_grande_demais_e_abandonado_e_refeito_com_janela_menor(monkeypatch) -> None:
    """O defeito de produção: run que não cabe nunca termina, e o watermark não anda.

    Com 100 mil resultados contra um teto de 20 mil, a janela de 10 h encolhe na
    proporção (com folga) ANTES de qualquer página ser lida.
    """
    start = _ago(hours=10)
    inside = _iso(_dt.now(_tz.utc) - _td(hours=9, minutes=30))
    events, ctx, posts, fetched = await _cycle(
        {"run_id": None, "from_ts": start, "page": 1},
        [
            ("post", {"id": "run-gigante", "status": "running"}),
            ("run", "run-gigante", {"status": "finished", "resultCount": 100_000}),
            ("post", {"id": "run-menor", "status": "running"}),
            ("run", "run-menor", {"status": "finished", "resultCount": 1}),
            ("results", "run-menor", _page(_det(1, inside))),
        ],
    )

    assert [e["id"] for e in events] == ["det-0001"]
    assert [run for run, _, _ in fetched] == ["run-menor"], (
        "nenhuma página do run gigante pode ser lida — ele seria abandonado no meio"
    )
    assert len(posts) == 2 and posts[1]["from"] == start
    # 10 h × 20.000/100.000 × 0,8 de folga = 1 h 36 min.
    assert _secs(start, posts[1]["to"]) == pytest.approx(5760, abs=5)
    # Janela truncada lida até o fim: o watermark anda até o FIM dela…
    assert ctx.cursor["run_id"] is None
    assert ctx.cursor["from_ts"] == posts[1]["to"]
    # … e o ciclo diz que ainda há passado por ler.
    assert ctx.hit_cycle_cap is True


@pytest.mark.asyncio
async def test_janela_truncada_sem_eventos_tambem_avanca(monkeypatch) -> None:
    """Trecho quieto no meio do atraso não pode prender a janela.

    Sem evento, "maior horário visto" continua igual ao início: se o watermark
    dependesse dele, a mesma janela vazia seria consultada para sempre.
    """
    start, end = _ago(hours=5), _ago(hours=4)
    events, ctx, posts, _ = await _cycle(
        {"run_id": "run-w", "from_ts": start, "to_ts": end, "span_s": 3600, "page": 1},
        [
            ("run", "run-w", {"status": "finished", "resultCount": 0}),
            ("results", "run-w", _page()),
        ],
    )

    assert events == [] and posts == []
    assert ctx.cursor["from_ts"] == end
    assert ctx.cursor["span_s"] == 7200, "janela com folga dobra a largura da próxima"
    assert "to_ts" not in ctx.cursor
    assert ctx.hit_cycle_cap is True


@pytest.mark.asyncio
async def test_janela_cheia_nao_dobra_a_largura(monkeypatch) -> None:
    """Dobrar depois de uma janela quase no teto garantiria um run abandonado."""
    start, end = _ago(hours=5), _ago(hours=4)
    _, ctx, _, _ = await _cycle(
        {"run_id": "run-w", "from_ts": start, "to_ts": end, "span_s": 3600, "page": 1},
        [
            ("run", "run-w", {"status": "finished", "resultCount": 15_000}),
            ("results", "run-w", _page(_det(1, start))),
        ],
    )
    assert ctx.cursor["span_s"] == 3600


@pytest.mark.asyncio
async def test_janela_que_alcanca_o_presente_volta_ao_cursor_de_sempre(monkeypatch) -> None:
    """Fim do atraso: a largura aprendida sai do cursor e o ciclo não alega backlog."""
    start = _ago(minutes=5)
    _, ctx, posts, _ = await _cycle(
        {"run_id": None, "from_ts": start, "span_s": 3600, "page": 1},
        [
            ("post", {"id": "run-1", "status": "running"}),
            ("run", "run-1", {"status": "finished", "resultCount": 0}),
            ("results", "run-1", _page()),
        ],
    )
    assert _secs(posts[0]["to"], _ago()) < 5, "a janela vai até agora"
    assert ctx.cursor == {"run_id": None, "from_ts": start, "page": 1}
    assert ctx.hit_cycle_cap is False


@pytest.mark.asyncio
async def test_backfill_nao_dimensiona_a_janela(monkeypatch) -> None:
    """O backfill chama ``collect()`` uma vez: truncar a janela cortaria o job."""
    start = _ago(hours=10)
    events, ctx, posts, fetched = await _cycle(
        {"run_id": None, "from_ts": start, "page": 1},
        [
            ("post", {"id": "run-bf", "status": "running"}),
            ("run", "run-bf", {"status": "finished", "resultCount": 100_000}),
            ("results", "run-bf", _page(_det(1, start))),
        ],
        bounded=False,
    )
    assert len(posts) == 1 and [run for run, _, _ in fetched] == ["run-bf"]
    assert len(events) == 1 and ctx.hit_cycle_cap is False


@pytest.mark.asyncio
async def test_maior_horario_visto_atravessa_os_ciclos_do_mesmo_run(monkeypatch) -> None:
    """A ordem dos resultados não é garantida. Se o run leva dois ciclos e o
    segundo só traz eventos ANTIGOS, o watermark não pode voltar para eles —
    senão o run seguinte relê quase tudo, para sempre."""
    start = "2026-10-15T10:00:00Z"
    novo, velho = "2026-10-15T16:00:00Z", "2026-10-15T11:00:00Z"
    consts = dict(_MAX_PAGES_PER_CYCLE=1, _RESULTS_PAGE_SIZE=2)

    events_1, ctx_1, _, _ = await _cycle(
        {"run_id": "run-2c", "from_ts": start, "page": 1},
        [
            ("run", "run-2c", {"status": "finished", "resultCount": 3}),
            ("results", "run-2c", _page(_det(1, novo), _det(2, novo), total=2)),
        ],
        monkeypatch=monkeypatch, **consts,
    )
    assert len(events_1) == 2 and ctx_1.hit_cycle_cap is True
    assert ctx_1.cursor["page"] == 2 and ctx_1.cursor["from_ts"] == start

    events_2, ctx_2, _, fetched = await _cycle(
        ctx_1.cursor,
        [
            ("run", "run-2c", {"status": "finished", "resultCount": 3}),
            ("results", "run-2c", _page(_det(3, velho), total=2)),
        ],
        monkeypatch=monkeypatch, **consts,
    )
    assert [e["id"] for e in events_2] == ["det-0003"]
    assert fetched == [("run-2c", 2, 2)]
    assert ctx_2.cursor["from_ts"] == novo


@pytest.mark.asyncio
async def test_orcamento_de_tempo_encerra_o_ciclo_depois_da_primeira_pagina(monkeypatch) -> None:
    """Ciclo mais longo que a cadência faz o agendamento seguinte ser pulado.

    A 1ª página é sempre lida (ciclo sem progresso seria poison-loop); a partir
    dela, estourou o orçamento, o ciclo para e deixa a retomada no cursor.
    """
    start = "2026-10-15T10:00:00Z"
    events, ctx, _, fetched = await _cycle(
        {"run_id": "run-t", "from_ts": start, "page": 1},
        [
            ("run", "run-t", {"status": "finished", "resultCount": 4}),
            ("results", "run-t", _page(_det(1, start), _det(2, start), total=2)),
            ("results", "run-t", _page(_det(3, start), _det(4, start), total=2)),
        ],
        monkeypatch=monkeypatch, _CYCLE_TIME_BUDGET_SECONDS=0.0, _RESULTS_PAGE_SIZE=2,
    )
    assert len(events) == 2 and fetched == [("run-t", 1, 2)]
    assert ctx.cursor["run_id"] == "run-t" and ctx.cursor["page"] == 2
    assert ctx.hit_cycle_cap is True


@pytest.mark.asyncio
async def test_run_morto_no_meio_do_ciclo_nao_avanca_o_watermark(monkeypatch) -> None:
    """4xx depois de páginas já lidas: as que faltavam NÃO foram lidas. Avançar
    para o maior horário visto as pularia; o certo é reler a mesma janela."""
    start, novo = "2026-10-15T10:00:00Z", "2026-10-15T16:00:00Z"
    events, ctx, _, _ = await _cycle(
        {"run_id": "run-m", "from_ts": start, "span_s": 900, "page": 1},
        [
            ("run", "run-m", {"status": "finished", "resultCount": 4}),
            ("results", "run-m", _page(_det(1, novo), _det(2, novo), total=2)),
            ("results", "run-m", 400),
        ],
        monkeypatch=monkeypatch, _RESULTS_PAGE_SIZE=2,
    )
    assert len(events) == 2
    assert ctx.cursor == {"run_id": None, "from_ts": start, "page": 1, "span_s": 900}


@pytest.mark.asyncio
async def test_run_expirado_no_poll_preserva_o_que_foi_aprendido(monkeypatch) -> None:
    """404 no status: o run novo não pode nascer do tamanho do que expirou."""
    start = "2026-10-15T10:00:00Z"
    _, ctx, _, _ = await _cycle(
        {"run_id": "run-x", "from_ts": start, "page": 7, "span_s": 900, "max_page_size": 2000},
        [("run", "run-x", 404)],
    )
    assert ctx.cursor == {
        "run_id": None, "from_ts": start, "page": 1, "span_s": 900, "max_page_size": 2000,
    }


@pytest.mark.asyncio
async def test_tamanho_de_pagina_e_aprendido_da_api_e_usado_no_run_seguinte(monkeypatch) -> None:
    """A API diz o maior ``pageSize`` que aceita. O run em curso mantém o dele
    (trocar mudaria a numeração das páginas); o seguinte usa o aprendido, até o
    teto próprio do coletor."""
    start = _ago(minutes=30)
    _, ctx_1, _, fetched_1 = await _cycle(
        {"run_id": None, "from_ts": start, "page": 1},
        [
            ("post", {"id": "run-a", "status": "running"}),
            ("run", "run-a", {"status": "finished", "resultCount": 1}),
            ("results", "run-a", _page(_det(1, start), maxSize=2000)),
        ],
    )
    assert fetched_1 == [("run-a", 1, 50)]
    assert ctx_1.cursor["max_page_size"] == 2000

    _, _, _, fetched_2 = await _cycle(
        ctx_1.cursor,
        [
            ("post", {"id": "run-b", "status": "running"}),
            ("run", "run-b", {"status": "finished", "resultCount": 1}),
            ("results", "run-b", _page(_det(2, start), maxSize=2000)),
        ],
    )
    assert fetched_2 == [("run-b", 1, _mod._PAGE_SIZE_CEILING)]


@pytest.mark.asyncio
async def test_4xx_na_primeira_pagina_com_pagina_grande_volta_ao_padrao_e_fixa(monkeypatch) -> None:
    """Se a página grande for recusada, o coletor não pode ficar alternando
    entre reaprender o máximo e tomar o mesmo 4xx."""
    start = _ago(minutes=30)
    _, ctx_1, _, fetched_1 = await _cycle(
        {"run_id": None, "from_ts": start, "page": 1, "max_page_size": 2000},
        [
            ("post", {"id": "run-a", "status": "running"}),
            ("run", "run-a", {"status": "finished", "resultCount": 10}),
            ("results", "run-a", 400),
        ],
    )
    assert fetched_1 == [("run-a", 1, 500)]
    assert ctx_1.cursor == {"run_id": None, "from_ts": start, "page": 1, "max_page_size": 50}

    _, ctx_2, _, fetched_2 = await _cycle(
        ctx_1.cursor,
        [
            ("post", {"id": "run-b", "status": "running"}),
            ("run", "run-b", {"status": "finished", "resultCount": 1}),
            ("results", "run-b", _page(_det(1, start), maxSize=2000)),
        ],
    )
    assert fetched_2 == [("run-b", 1, 50)]
    assert ctx_2.cursor["max_page_size"] == 50, "o máximo anunciado não é reaprendido"


@pytest.mark.asyncio
async def test_sem_total_de_paginas_o_fim_sai_do_result_count(monkeypatch) -> None:
    """``pages.total`` é opcional na especificação. Sem ele, o coletor parava na
    1ª página e avançava o watermark — o resto do run sumia sem erro."""
    start = "2026-10-15T10:00:00Z"
    events, ctx, _, fetched = await _cycle(
        {"run_id": "run-s", "from_ts": start, "page": 1},
        [
            ("run", "run-s", {"status": "finished", "resultCount": 3}),
            ("results", "run-s", _page(_det(1, start), _det(2, start), total=None)),
            ("results", "run-s", _page(_det(3, start), total=None)),
        ],
        monkeypatch=monkeypatch, _RESULTS_PAGE_SIZE=2,
    )
    assert [e["id"] for e in events] == ["det-0001", "det-0002", "det-0003"]
    assert fetched == [("run-s", 1, 2), ("run-s", 2, 2)]
    assert ctx.cursor["run_id"] is None


@pytest.mark.asyncio
async def test_backfill_ignora_a_largura_aprendida_no_cursor(monkeypatch) -> None:
    """Mesmo com ``span_s`` no cursor, o run do backfill vai até agora."""
    start = _ago(hours=10)
    _, ctx, posts, _ = await _cycle(
        {"run_id": None, "from_ts": start, "span_s": 600, "page": 1},
        [
            ("post", {"id": "run-bf", "status": "running"}),
            ("run", "run-bf", {"status": "finished", "resultCount": 1}),
            ("results", "run-bf", _page(_det(1, start))),
        ],
        bounded=False,
    )
    assert _secs(posts[0]["to"], _ago()) < 5
    assert ctx.hit_cycle_cap is False


@pytest.mark.asyncio
async def test_run_pendente_mantem_o_tamanho_de_pagina_com_que_nasceu(monkeypatch) -> None:
    """Run criado com página de 50 e retomado na página 7: pedir a 7 com 500
    pularia 3.000 detections — a numeração depende do tamanho."""
    start = "2026-10-15T10:00:00Z"
    _, _, _, fetched = await _cycle(
        {"run_id": "run-p", "from_ts": start, "page": 7, "max_page_size": 2000},
        [
            ("run", "run-p", {"status": "finished", "resultCount": 301}),
            ("results", "run-p", _page(_det(1, start), total=7)),
        ],
    )
    assert fetched == [("run-p", 7, 50)]
