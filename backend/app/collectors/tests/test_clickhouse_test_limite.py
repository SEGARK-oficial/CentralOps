"""O ``test()`` do ClickHouse não pode deixar "ok" ser lido como "entregando".

Incidente que originou estes testes: um destino nano com os TRÊS passos verdes
— conexão, tabela, colunas — perdia ~90% dos lotes. A tabela de aterrissagem do
nano é ``Engine=Null`` e alimenta uma cadeia de materialized views; a cadeia
roda com a permissão de quem INSERE, e uma tabela intermediária sem ``SELECT``
derrubava cada INSERT com ``ACCESS_DENIED`` (497). Nada disso é observável por
leitura, então nenhum dos três passos podia pegar.

O que dá para consertar não é a cobertura — não existe INSERT em seco no
ClickHouse — é a LEITURA do resultado. Todo sucesso precisa dizer o que não
prova e apontar a DLQ como fonte de verdade da entrega.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors.output.clickhouse_sender import (
    LIMITE_DO_TESTE,
    ClickHouseClient,
)


def _cliente(**kwargs) -> ClickHouseClient:
    base = dict(
        url="http://ch:8123", password="s", database="nanosiem", table="ocsf_logs_raw"
    )
    base.update(kwargs)
    return ClickHouseClient(**base)


def _com_respostas(cliente: ClickHouseClient, respostas: list) -> ClickHouseClient:
    """Mesma injeção do ``test_clickhouse_test_probe``: troca ``_consulta``, não
    a sessão aiohttp. O alvo é a lógica dos passos, não a plumbing HTTP."""
    fila = list(respostas)

    async def _falsa(_sql: str):
        return fila.pop(0)

    cliente._consulta = _falsa  # type: ignore[method-assign]
    return cliente


@pytest.mark.asyncio
async def test_o_sucesso_wrapped_declara_que_nao_executa_insert() -> None:
    cli = _com_respostas(
        _cliente(payload="ocsf", row_shape="wrapped", row_fields={"source_type": "x"}),
        [(200, "1"), (200, "event\nsource_type\n")],
    )

    resultado = await cli.test()

    assert resultado.ok is True
    assert "NÃO executa INSERT" in resultado.detail
    assert "DLQ" in resultado.detail


@pytest.mark.asyncio
async def test_o_sucesso_flat_tambem_declara() -> None:
    """A ressalva não pode depender da forma da linha: o furo é do INSERT,
    não do wrapper."""
    cli = _com_respostas(
        _cliente(payload="ocsf", row_shape="flat"),
        [(200, "1"), (200, "class_uid\ntime\n")],
    )

    resultado = await cli.test()

    assert resultado.ok is True
    assert LIMITE_DO_TESTE in resultado.detail


@pytest.mark.asyncio
async def test_a_FALHA_nao_carrega_a_ressalva() -> None:
    """Numa falha o operador já tem uma ação concreta; pendurar a ressalva ali
    dilui a mensagem que importa e treina a pessoa a ignorar o texto."""
    cli = _com_respostas(
        _cliente(payload="ocsf", row_shape="wrapped", row_fields={"nao_existe": "x"}),
        [(200, "1"), (200, "event\nsource_type\n")],
    )

    resultado = await cli.test()

    assert resultado.ok is False
    assert LIMITE_DO_TESTE not in resultado.detail


def test_a_ressalva_nomeia_o_mecanismo_e_nao_so_avisa():
    """"Pode falhar" não ajuda ninguém. O texto precisa dizer O QUE não é
    coberto (MV/constraint/cota) e ONDE está a verdade (DLQ) — senão o
    operador não sabe o que fazer com o aviso."""
    for termo in ("INSERT", "materialized view", "constraint", "cota", "DLQ"):
        assert termo in LIMITE_DO_TESTE, termo
