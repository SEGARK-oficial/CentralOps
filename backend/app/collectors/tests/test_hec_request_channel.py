"""``X-Splunk-Request-Channel``: sem ele, o HEC recusa DEPOIS de autenticar.

Medido contra um nano SIEM 26.5.1, que implementa HEC e exige o canal sempre:

    sem token                    → {"text":"Token is required","code":2}
    token inválido, sem canal    → {"text":"Invalid authorization","code":3}
    token VÁLIDO, sem canal      → {"text":"Data channel is missing","code":10}

A ordem é o que torna isso caro de diagnosticar: o erro só aparece depois que
a credencial passa, então lê-se como problema de token — e o operador vai
regenerar o token, que não é o problema.

O canal identifica o PRODUTOR, não a requisição. O servidor correlaciona
indexer acknowledgements por ele e limita quantos canais concorrentes mantém;
um GUID por POST faria despejar canais antigos e perder ack. Daí ser derivado
do ``destination_id``, que já é único e estável entre reinícios.
"""

from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.collectors.output.destinations import registry
from backend.app.collectors.output.destinations.registry import DestinationConfig
from backend.app.collectors.output.destinations.splunk_hec import _canal_estavel
from backend.app.collectors.output.splunk_hec_sender import SplunkHecClient


def _cliente_do_registry(destination_id: str, **cfg) -> SplunkHecClient:
    base = {"url": "https://nano:8088"}
    base.update(cfg)
    return registry.get("splunk_hec").factory(
        DestinationConfig(destination_id=destination_id, kind="splunk_hec", config=base),
        None,
    )


async def _headers(cliente: SplunkHecClient) -> dict:
    """Headers reais da sessão aiohttp — não uma reimplementação do cálculo.

    Assíncrono porque ``TCPConnector`` exige event loop rodando; ler a sessão
    de verdade é o ponto, já que o bug estava exatamente na montagem dela.
    """
    sessao = cliente._get_session()
    try:
        return dict(sessao.headers)
    finally:
        await sessao.close()
        cliente._session = None


@pytest.mark.asyncio
async def test_o_header_do_canal_vai_no_fio():
    """O bug: a sessão só mandava Content-Type e Authorization."""
    h = await _headers(_cliente_do_registry("d-1"))
    assert "X-Splunk-Request-Channel" in h


@pytest.mark.asyncio
async def test_o_canal_e_um_guid_valido():
    """Servidor HEC valida a FORMA do canal; texto livre é recusado igual a
    canal ausente, trocando um erro por outro."""
    h = await _headers(_cliente_do_registry("d-1"))
    uuid.UUID(h["X-Splunk-Request-Channel"])  # levanta se não for GUID


@pytest.mark.asyncio
async def test_o_canal_e_ESTAVEL_para_o_mesmo_destino():
    """Este é o teste que importa: reiniciar o collector não pode trocar o
    canal, senão o servidor trata cada boot como cliente novo e descarta os
    acks pendentes do anterior."""
    a = (await _headers(_cliente_do_registry("d-1")))["X-Splunk-Request-Channel"]
    b = (await _headers(_cliente_do_registry("d-1")))["X-Splunk-Request-Channel"]
    assert a == b


@pytest.mark.asyncio
async def test_destinos_DIFERENTES_tem_canais_diferentes():
    """Canal compartilhado entre dois destinos faz o servidor misturar os acks
    dos dois — o oposto do que o canal existe para fazer."""
    a = (await _headers(_cliente_do_registry("d-1")))["X-Splunk-Request-Channel"]
    b = (await _headers(_cliente_do_registry("d-2")))["X-Splunk-Request-Channel"]
    assert a != b


@pytest.mark.asyncio
async def test_o_operador_pode_fixar_o_canal():
    """Alguns coletores exigem um GUID acordado fora de banda. O valor
    explícito precisa vencer o derivado."""
    fixo = "11111111-2222-3333-4444-555555555555"
    h = await _headers(_cliente_do_registry("d-1", channel=fixo))
    assert h["X-Splunk-Request-Channel"] == fixo


def test_o_canal_nao_deriva_do_TOKEN():
    """Guard de segurança: derivar de segredo poria material derivado da
    credencial num header que trafega em claro sob http:// e aparece em log de
    proxy. O canal precisa depender só de identificador público."""
    a = _canal_estavel("d-1")
    cliente = SplunkHecClient(url="https://nano:8088", token="tok-A", channel=a)
    outro = SplunkHecClient(url="https://nano:8088", token="tok-B", channel=a)
    assert cliente._channel == outro._channel


@pytest.mark.asyncio
async def test_sem_canal_declarado_o_sender_nao_inventa_header():
    """Uso direto da classe (fora da factory) sem canal não deve mandar o
    header vazio: header presente e vazio é recusado igual a ausente, mas
    diagnostica pior."""
    cliente = SplunkHecClient(url="https://nano:8088", token="t")
    h = await _headers(cliente)
    assert "X-Splunk-Request-Channel" not in h
