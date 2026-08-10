"""Auto-cura de cursor opaco em 4xx — contrato transversal aos coletores.

Vários coletores persistem um token opaco do vendor para continuar a paginação.
Quando esse token expira o vendor responde 4xx, e o que transformava isso num
feed PARADO era o caminho de erro do pipeline: ao levantar, ``cursor_before`` é
regravado byte a byte, o token morto volta ao banco e o ciclo seguinte o reenvia
— indefinidamente, até um reset manual.

Observado em produção com o XDR do Sophos: ``400`` em
``/detections/v1/queries/detections/{run_id}/results?page=6`` com o cursor
zerado, ciclo após ciclo.

Cada teste aqui prova as duas metades para um coletor: (a) um 4xx seguindo token
opaco NÃO levanta e deixa o cursor sem o token, e (b) um 4xx SEM token opaco
continua levantando — senão um erro de configuração (janela inválida, permissão,
tenant errado) seria engolido e reportado como ciclo bem-sucedido.
"""

from __future__ import annotations

import re
from typing import Any, Dict
from unittest.mock import MagicMock

import aiohttp
import pytest
from ._aiohttp_mock import aioresponses

from ..base import CollectorContext
from ..vendors._cursor_selfheal import is_stale_cursor_response


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


def _ctx(session, platform: str, cursor: Dict[str, Any] | None, headers=None):
    ctx = CollectorContext(
        integration_id=42,
        organization_id=7,
        platform=platform,
        headers=headers or {"Authorization": "Bearer x"},
        session=session,
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
    )
    ctx.bounded_per_cycle = True
    return ctx


async def _drain(collector) -> list:
    return [ev async for ev in collector.collect()]


class TestPredicado:
    """O predicado é o que separa "token morreu" de "erro de verdade"."""

    @pytest.mark.parametrize("status", [400, 403, 404, 410, 422])
    def test_4xx_com_token_opaco_e_cursor_morto(self, status):
        assert is_stale_cursor_response(status, has_opaque_token=True)

    @pytest.mark.parametrize("status", [400, 403, 404, 422])
    def test_mesmo_4xx_sem_token_nao_e(self, status):
        """Sem token em jogo, o 4xx é erro real e precisa subir."""
        assert not is_stale_cursor_response(status, has_opaque_token=False)

    def test_401_fica_de_fora(self):
        """401 é recuperação de credencial — o pipeline renova e repete."""
        assert not is_stale_cursor_response(401, has_opaque_token=True)

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_5xx_fica_de_fora(self, status):
        """Erro do servidor merece retry com o cursor INTACTO."""
        assert not is_stale_cursor_response(status, has_opaque_token=True)

    @pytest.mark.parametrize("status", [200, 201, 204, 302])
    def test_sucesso_nao_e_cursor_morto(self, status):
        assert not is_stale_cursor_response(status, has_opaque_token=True)


class TestSophosDetections:
    """O caso que quebrou em produção."""

    URL = re.compile(r"^https://api-eu03\.central\.sophos\.com/detections/v1/.*$")

    @pytest.mark.asyncio
    async def test_400_no_results_descarta_run_id(self):
        from ..vendors.sophos_detections import SophosDetectionsCollector

        with aioresponses() as mocked:
            # poll do status: run terminado
            mocked.get(self.URL, payload={"status": "finished"})
            # results: o run morreu
            mocked.get(self.URL, status=400, body='{"error":"invalid run"}')
            async with aiohttp.ClientSession() as session:
                ctx = _ctx(
                    session, "sophos",
                    {"run_id": "run-morto", "from_ts": "2026-08-08T12:00:00Z", "page": 6},
                    headers={"Authorization": "Bearer x", "X-Region": "eu03"},
                )
                events = await _drain(SophosDetectionsCollector(ctx))

        assert events == []
        assert ctx.cursor["run_id"] is None, (
            "o run morto tem de sair do cursor, senão o próximo ciclo o reenvia"
        )
        assert ctx.cursor["from_ts"] == "2026-08-08T12:00:00Z", (
            "a janela precisa sobreviver — é de onde o run novo será criado"
        )


class TestOkta:
    URL = re.compile(r"^https://acme\.okta\.com/api/v1/logs.*$")

    @pytest.mark.asyncio
    async def test_400_seguindo_next_url_volta_para_a_janela(self, monkeypatch):
        from ..vendors import okta as okta_mod

        monkeypatch.setattr(
            okta_mod.OktaSystemLogCollector, "_load_conn",
            lambda self: {"base_url": "https://acme.okta.com", "token": "t"},
        )
        with aioresponses() as mocked:
            mocked.get(self.URL, status=400, body='{"errorCode":"E0000001"}')
            async with aiohttp.ClientSession() as session:
                ctx = _ctx(session, "okta", {
                    "next_url": "https://acme.okta.com/api/v1/logs?after=morto",
                    "since": "2026-08-08T00:00:00Z",
                })
                events = await _drain(okta_mod.OktaSystemLogCollector(ctx))

        assert events == []
        assert "next_url" not in ctx.cursor
        assert ctx.cursor["since"] == "2026-08-08T00:00:00Z", (
            "sem o piso temporal o Okta não teria de onde retomar"
        )

    @pytest.mark.asyncio
    async def test_400_na_primeira_pagina_propaga(self, monkeypatch):
        """Sem next_url, o 4xx é erro real (token SSWS, domínio, escopo)."""
        from ..vendors import okta as okta_mod

        monkeypatch.setattr(
            okta_mod.OktaSystemLogCollector, "_load_conn",
            lambda self: {"base_url": "https://acme.okta.com", "token": "t"},
        )
        with aioresponses() as mocked:
            mocked.get(self.URL, status=403, body='{"errorCode":"E0000006"}')
            async with aiohttp.ClientSession() as session:
                ctx = _ctx(session, "okta", None)
                with pytest.raises(aiohttp.ClientResponseError):
                    await _drain(okta_mod.OktaSystemLogCollector(ctx))


class TestCrowdStrike:
    URL = re.compile(r"^https://api\.crowdstrike\.com/.*$")

    @pytest.mark.asyncio
    async def test_400_seguindo_after_descarta_o_token(self, monkeypatch):
        from ..vendors.crowdstrike import CrowdStrikeDetectionsCollector

        monkeypatch.setattr(
            CrowdStrikeDetectionsCollector, "_load_base_url",
            lambda self: "https://api.crowdstrike.com",
        )
        with aioresponses() as mocked:
            mocked.post(self.URL, status=400, body='{"errors":[{"message":"bad after"}]}')
            async with aiohttp.ClientSession() as session:
                ctx = _ctx(session, "crowdstrike", {
                    "created_after": "2026-08-08T00:00:00Z", "after": "tok-morto",
                })
                events = await _drain(CrowdStrikeDetectionsCollector(ctx))

        assert events == []
        assert ctx.cursor["after"] is None
        assert ctx.cursor["created_after"] == "2026-08-08T00:00:00Z"


class TestEntraId:
    URL = re.compile(r"^https://graph\.microsoft\.com/.*$")

    @pytest.mark.asyncio
    async def test_400_seguindo_nextlink_volta_para_o_filter(self):
        from ..vendors.entra_id import EntraSignInsCollector

        with aioresponses() as mocked:
            mocked.get(self.URL, status=400, body='{"error":{"code":"BadRequest"}}')
            async with aiohttp.ClientSession() as session:
                ctx = _ctx(session, "entra_id", {
                    "createdDateTime": "2026-08-08T00:00:00Z",
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/morto",
                })
                events = await _drain(EntraSignInsCollector(ctx))

        assert events == []
        assert ctx.cursor["@odata.nextLink"] is None
        assert ctx.cursor["createdDateTime"] == "2026-08-08T00:00:00Z"


class TestCoberturaDaAuditoria:
    """Trava o inventário: quem persiste token opaco tem de usar a primitiva.

    Sem isto, um coletor novo (ou um refactor) reintroduz o poison-loop e
    ninguém percebe até um feed parar em produção.
    """

    #: (módulo, campo do cursor que carrega token opaco do vendor)
    OPAQUE_TOKEN_COLLECTORS = [
        ("sophos", "pageFromKey"),
        ("sophos_siem", "page_cursor"),
        ("sophos_detections", "run_id"),
        ("crowdstrike", "after"),
        ("okta", "next_url"),
        ("entra_id", "@odata.nextLink"),
    ]

    @pytest.mark.source_only
    @pytest.mark.parametrize("module,field", OPAQUE_TOKEN_COLLECTORS)
    def test_coletor_com_token_opaco_trata_4xx(self, module, field):
        # ``source_only``: este teste LÊ O FONTE do coletor
        # (``Path(mod.__file__).read_text()``). Na imagem de produção
        # ``compose/cython-build.sh`` compila ``app/collectors`` inteiro e REMOVE
        # os ``.py``, então ``__file__`` aponta para um ``.so`` e a leitura
        # explode. Sem o marcador, o gate compilado do build reprova — e como o
        # gate roda dentro do ``docker compose build``, o efeito é que NENHUMA
        # imagem sobe. Mesma disciplina de ``test_adr0015_inflight_matcher.py``.
        import importlib
        from pathlib import Path

        mod = importlib.import_module(f"..vendors.{module}", package=__package__)
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert field in src, f"{module} deixou de usar {field}? atualize este inventário"
        # Sophos alerts/siem trazem a cura inline (nasceram antes da primitiva);
        # os demais usam o helper. Qualquer uma das duas satisfaz o contrato.
        tem_cura = "is_stale_cursor_response" in src or "stale_page_key" in src or "stale_cursor" in src
        assert tem_cura, (
            f"{module} persiste o token opaco {field!r} mas não trata 4xx — um token "
            f"expirado travaria o stream até reset manual"
        )
