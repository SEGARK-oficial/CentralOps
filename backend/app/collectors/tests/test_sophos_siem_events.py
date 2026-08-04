"""Sophos SIEM v1 — ``/siem/v1/events``.

Cobre o que distingue este stream dos outros três coletores Sophos: paginação
por cursor opaco (``has_more``/``next_cursor``), teto por ciclo com cursor
resumível, auto-cura de cursor rejeitado, e a tolerância deliberada a variações
de nome de campo (o contrato do endpoint não pôde ser verificado contra a
documentação oficial — ver docstring do módulo do coletor).
"""

from __future__ import annotations

import re
from typing import Any, Dict
from unittest.mock import MagicMock

import aiohttp
import pytest
from ._aiohttp_mock import aioresponses

from ..base import CollectorContext
from ..vendors.sophos_siem import (
    SophosSiemEventsCollector,
    SophosSiemRateLimitedError,
    _extract_cursor,
    _extract_items,
)

_URL_RE = re.compile(r"^https://api-eu03\.central\.sophos\.com/siem/v1/events(\?.*)?$")


class _NoopDomainLimiter:
    def slot(self, domain):
        class _Ctx:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()


class _NoopRateLimiter:
    async def acquire(self, tenant_id, vendor):
        return None

    async def backoff(self, vendor, retry_after):
        return None


def _ctx(
    session: aiohttp.ClientSession,
    cursor: Dict[str, Any] | None = None,
    bounded: bool = True,
) -> CollectorContext:
    ctx = CollectorContext(
        integration_id=42,
        organization_id=7,
        platform="sophos",
        headers={"Authorization": "Bearer x", "X-Region": "eu03"},
        session=session,
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
    )
    ctx.bounded_per_cycle = bounded
    return ctx


async def _drain(collector) -> list:
    return [ev async for ev in collector.collect()]


class TestPaginacaoPorCursor:
    @pytest.mark.asyncio
    async def test_pagina_ate_has_more_falso(self) -> None:
        pages = [
            {
                "items": [{"id": "e1", "created_at": "2026-04-23T10:00:00Z"}],
                "has_more": True,
                "next_cursor": "c2",
            },
            {
                "items": [{"id": "e2", "created_at": "2026-04-23T11:00:00Z"}],
                "has_more": False,
                "next_cursor": None,
            },
        ]
        with aioresponses() as mocked:
            for page in pages:
                mocked.get(_URL_RE, payload=page)
            async with aiohttp.ClientSession() as session:
                # Cursor explícito ANTERIOR aos eventos: com o cold start de 12h
                # os eventos deste fixture cairiam fora da janela e o watermark
                # corretamente não retrocederia, mascarando o que se quer testar.
                ctx = _ctx(session, cursor={"from_ts": "2026-04-23T00:00:00Z"})
                events = await _drain(SophosSiemEventsCollector(ctx))

        assert [e["id"] for e in events] == ["e1", "e2"]
        # Cursor final: janela avança para o evento mais recente, paginação zera.
        assert ctx.cursor["from_ts"] == "2026-04-23T11:00:00Z"
        assert ctx.cursor["page_cursor"] is None

    @pytest.mark.asyncio
    async def test_watermark_nunca_retrocede(self) -> None:
        """Evento mais antigo que a janela não puxa o watermark para trás."""
        with aioresponses() as mocked:
            mocked.get(
                _URL_RE,
                payload={"items": [{"id": "antigo", "created_at": "2020-01-01T00:00:00Z"}],
                         "has_more": False},
            )
            async with aiohttp.ClientSession() as session:
                ctx = _ctx(session, cursor={"from_ts": "2026-04-23T00:00:00Z"})
                await _drain(SophosSiemEventsCollector(ctx))

        assert ctx.cursor["from_ts"] == "2026-04-23T00:00:00Z"

    @pytest.mark.asyncio
    async def test_has_more_sem_cursor_nao_pagina_infinitamente(self) -> None:
        """``has_more`` verdadeiro mas sem cursor: paramos em vez de repetir.

        O contrato do endpoint não foi confirmado; repetir a mesma requisição
        indefinidamente seria pior que reler a janela no próximo ciclo.
        """
        with aioresponses() as mocked:
            mocked.get(
                _URL_RE,
                payload={"items": [{"id": "e1", "created_at": "2026-04-23T10:00:00Z"}],
                         "has_more": True},
            )
            async with aiohttp.ClientSession() as session:
                ctx = _ctx(session)
                events = await _drain(SophosSiemEventsCollector(ctx))

        assert len(events) == 1
        assert ctx.cursor["page_cursor"] is None


class TestAutoCuraDeCursor:
    @pytest.mark.asyncio
    async def test_400_com_cursor_descarta_a_chave_e_encerra_limpo(self) -> None:
        """400 paginando NÃO pode levantar.

        No caminho de exceção o pipeline regrava ``cursor_before`` byte-a-byte,
        então a chave morta voltaria e o feed ficaria travado até um reset
        manual. Saindo limpo, a escrita final persiste ``page_cursor=None``.
        """
        with aioresponses() as mocked:
            mocked.get(_URL_RE, status=400, body='{"error":"invalid cursor"}')
            async with aiohttp.ClientSession() as session:
                ctx = _ctx(session, cursor={"from_ts": "2026-04-23T09:00:00Z",
                                            "page_cursor": "expirado"})
                events = await _drain(SophosSiemEventsCollector(ctx))

        assert events == []
        assert ctx.cursor["page_cursor"] is None
        assert ctx.cursor["from_ts"] == "2026-04-23T09:00:00Z"

    @pytest.mark.asyncio
    async def test_400_sem_cursor_propaga(self) -> None:
        """Sem cursor de paginação, o 400 é um erro real — deve subir."""
        with aioresponses() as mocked:
            mocked.get(_URL_RE, status=400, body='{"error":"bad request"}')
            async with aiohttp.ClientSession() as session:
                ctx = _ctx(session)
                with pytest.raises(aiohttp.ClientResponseError):
                    await _drain(SophosSiemEventsCollector(ctx))

    @pytest.mark.asyncio
    async def test_cursor_com_timestamp_invalido_cai_no_lookback(self) -> None:
        """Cursor envenenado se cura sozinho, sem reset manual."""
        with aioresponses() as mocked:
            mocked.get(_URL_RE, payload={"items": [], "has_more": False})
            async with aiohttp.ClientSession() as session:
                ctx = _ctx(session, cursor={"from_ts": "lixo-nao-parseavel"})
                await _drain(SophosSiemEventsCollector(ctx))

        assert ctx.cursor["from_ts"].endswith("Z")
        assert ctx.cursor["from_ts"] != "lixo-nao-parseavel"


class TestTetoPorCiclo:
    @pytest.mark.asyncio
    async def test_teto_salva_cursor_resumivel_e_preserva_a_janela(self) -> None:
        """Ao bater o teto, ``from_ts`` NÃO avança.

        O feed não garante ordenação por data; mover a janela para o evento
        mais recente já lido pularia as páginas ainda não coletadas.
        """
        from ..vendors import sophos_siem

        with aioresponses() as mocked:
            for i in range(sophos_siem._MAX_PAGES_PER_CYCLE):
                mocked.get(
                    _URL_RE,
                    payload={
                        "items": [{"id": f"e{i}", "created_at": "2026-04-23T12:00:00Z"}],
                        "has_more": True,
                        "next_cursor": f"c{i + 1}",
                    },
                )
            async with aiohttp.ClientSession() as session:
                ctx = _ctx(session, cursor={"from_ts": "2026-04-23T00:00:00Z"})
                events = await _drain(SophosSiemEventsCollector(ctx))

        assert len(events) == sophos_siem._MAX_PAGES_PER_CYCLE
        assert ctx.cursor["from_ts"] == "2026-04-23T00:00:00Z", "janela não pode avançar"
        assert ctx.cursor["page_cursor"], "cursor precisa ser resumível"


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_429_levanta_erro_de_rate_limit_com_retry_after(self) -> None:
        with aioresponses() as mocked:
            mocked.get(_URL_RE, status=429, headers={"Retry-After": "17"}, body="{}")
            async with aiohttp.ClientSession() as session:
                with pytest.raises(SophosSiemRateLimitedError) as exc:
                    await _drain(SophosSiemEventsCollector(_ctx(session)))
        assert exc.value.retry_after == 17


class TestContratoOficialDoEndpoint:
    """Fixa o que a spec OAS 3.0 oficial define, e que difere dos outros streams.

    Verificado em developer.sophos.com/docs/siem-v1/1 (rota /events + tipo
    LegacyEventEntity). Cada asserção aqui corresponde a uma linha da doc — e a
    primeira delas cobre um bug real: a implementação inicial mandava ISO string
    em ``from_date``, que a API define como Unix timestamp inteiro.
    """

    def test_iso_para_epoch_em_segundos(self):
        from ..vendors.sophos_siem import _iso_to_epoch_seconds

        # 2026-07-30T12:00:00Z
        assert _iso_to_epoch_seconds("2026-07-30T12:00:00Z") == 1785412800
        # offset não-UTC converte para o mesmo instante
        assert _iso_to_epoch_seconds("2026-07-30T09:00:00-03:00") == 1785412800
        # naive é assumido UTC
        assert _iso_to_epoch_seconds("2026-07-30T12:00:00") == 1785412800

    @pytest.mark.asyncio
    async def test_from_date_vai_como_inteiro_e_nao_iso(self) -> None:
        capturado: dict = {}

        with aioresponses() as mocked:
            mocked.get(_URL_RE, payload={"items": [], "has_more": False})
            async with aiohttp.ClientSession() as session:
                ctx = _ctx(session, cursor={"from_ts": "2026-07-30T12:00:00Z"})
                await _drain(SophosSiemEventsCollector(ctx))
                for req, calls in mocked.requests.items():
                    capturado["url"] = str(req[1])

        url = capturado["url"]
        assert "from_date=1785412800" in url, (
            f"from_date precisa ser epoch inteiro; URL foi {url}"
        )
        assert "2026-07-30T12" not in url, "ISO vazou para o query param"

    @pytest.mark.asyncio
    async def test_cursor_suprime_from_date(self) -> None:
        """A doc: 'from_date ... Ignored if cursor is set'."""
        capturado: dict = {}
        with aioresponses() as mocked:
            mocked.get(_URL_RE, payload={"items": [], "has_more": False})
            async with aiohttp.ClientSession() as session:
                ctx = _ctx(session, cursor={"from_ts": "2026-07-30T12:00:00Z",
                                            "page_cursor": "abc"})
                await _drain(SophosSiemEventsCollector(ctx))
                for req, _ in mocked.requests.items():
                    capturado["url"] = str(req[1])

        assert "cursor=abc" in capturado["url"]
        assert "from_date" not in capturado["url"]

    def test_limit_respeita_o_minimo_de_200(self):
        """A doc define 200 <= limit <= 1000. Abaixo de 200 é rejeitado."""
        from ..vendors import sophos_siem

        assert 200 <= sophos_siem._PAGE_SIZE <= 1000

    def test_cold_start_cabe_na_retencao_de_24h(self):
        """from_date 'Must be within last 24 hours' — um lookback maior seria
        recusado ou silenciosamente truncado."""
        from ..vendors import sophos_siem

        assert sophos_siem._COLD_START_LOOKBACK.total_seconds() < 24 * 3600


class TestToleranciaDeContrato:
    """O contrato do endpoint não pôde ser verificado na doc oficial.

    Estes testes fixam a tolerância deliberada: uma diferença de nomenclatura
    não pode virar "coletou zero" em silêncio, porque num feed de retenção 24h
    isso é perda definitiva.
    """

    @pytest.mark.parametrize("chave", ["items", "events", "data"])
    def test_itens_reconhecidos_por_nome_alternativo(self, chave):
        assert _extract_items({chave: [{"id": "x"}]}) == [{"id": "x"}]

    def test_payload_sem_lista_conhecida_devolve_vazio(self):
        assert _extract_items({"resultado": [{"id": "x"}]}) == []

    @pytest.mark.parametrize("chave", ["next_cursor", "nextCursor", "cursor"])
    def test_cursor_reconhecido_por_nome_alternativo(self, chave):
        cursor, has_more = _extract_cursor({chave: "abc", "has_more": True})
        assert cursor == "abc"
        assert has_more is True

    def test_has_more_camelcase(self):
        _, has_more = _extract_cursor({"next_cursor": "abc", "hasMore": True})
        assert has_more is True

    def test_sem_has_more_nao_continua(self):
        _, has_more = _extract_cursor({"next_cursor": "abc"})
        assert has_more is False


class TestIdentidadeDoEvento:
    def test_dedupe_pelo_id_puro(self):
        """Eventos deste feed são imutáveis: compor com data reintroduziria o
        mesmo evento a cada re-leitura de janela."""
        c = SophosSiemEventsCollector.__new__(SophosSiemEventsCollector)
        assert c.extract_message_id({"id": "evt-1", "created_at": "x"}) == "evt-1"
        # ``id`` é o único identificador do LegacyEventEntity — não há fallback
        # para ``event_id``, que não existe no schema oficial.
        assert c.extract_message_id({"event_id": "evt-2"}) == ""

    def test_watermark_le_from_ts(self):
        wm = SophosSiemEventsCollector.watermark_at({"from_ts": "2026-04-23T10:00:00Z"})
        assert wm is not None
        assert wm.year == 2026 and wm.hour == 10

    def test_watermark_ignora_janela_de_backfill(self):
        """Backfill recupera passado de propósito — não é atraso."""
        assert SophosSiemEventsCollector.watermark_at(
            {"backfill_from_ts": "2020-01-01T00:00:00Z"}
        ) is None
