"""O botão "Testar conexão" do ClickHouse, que antes só sabia dizer "conectei".

O ``test()` era um ``SELECT 1``. Isso pega servidor fora do ar e credencial
errada, e não pega nenhuma das três coisas que de fato quebraram um destino em
produção: tabela inexistente, nome qualificado no campo errado, e formato de
linha incompatível com as colunas.

A terceira é a que importa, porque é a única que **não** falha na entrega: o
ClickHouse aceita, responde 200, e grava linhas vazias. Não há erro para
investigar depois, então a verificação tem que acontecer antes.

O passo do catálogo é INCONCLUSIVO e não falha quando o usuário não tem
permissão de leitura, porque o usuário de ingestão costuma ser INSERT-only por
desenho. Reprovar um destino correto por causa disso seria trocar um silêncio
por um falso vermelho.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors.output.clickhouse_sender import ClickHouseClient


def _cliente(**kwargs) -> ClickHouseClient:
    base = dict(
        url="http://ch:8123", password="s", database="nanosiem", table="ocsf_logs_raw"
    )
    base.update(kwargs)
    return ClickHouseClient(**base)


def _com_respostas(cliente: ClickHouseClient, respostas: list) -> ClickHouseClient:
    """Injeta respostas na ordem em que ``test()`` faz as consultas.

    Mockar ``_consulta`` (e não a sessão aiohttp) é deliberado: o alvo do teste é
    a lógica dos três passos, não a plumbing HTTP, que já tem cobertura própria.
    """
    fila = list(respostas)

    async def _falsa(_sql: str):
        return fila.pop(0)

    cliente._consulta = _falsa  # type: ignore[method-assign]
    return cliente


# ── passo 1: conectividade ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_porta_nativa_reprova_e_devolve_a_dica_do_proprio_clickhouse() -> None:
    """O servidor nativo responde 4xx com o texto que diz qual porta usar.

    Este é o erro do incidente, e o botão teria pego. Ele só não existia no
    formulário de criação.
    """
    banner = (
        "Port 9000 is for clickhouse-client program\r\n"
        "You must use port 8123 for HTTP.\r\n"
    )
    resultado = await _com_respostas(_cliente(), [(400, banner)]).test()

    assert resultado.ok is False
    assert "8123" in resultado.detail


@pytest.mark.asyncio
async def test_credencial_invalida_reprova_no_primeiro_passo() -> None:
    resultado = await _com_respostas(_cliente(), [(401, "")]).test()

    assert resultado.ok is False
    assert "credencial" in resultado.detail


# ── passo 2: banco e tabela ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_tabela_inexistente_reprova_citando_o_alvo() -> None:
    resultado = await _com_respostas(_cliente(), [(200, "1\n"), (200, "")]).test()

    assert resultado.ok is False
    assert "nanosiem.ocsf_logs_raw" in resultado.detail


@pytest.mark.asyncio
async def test_a_mensagem_de_tabela_ausente_avisa_do_ponto_no_campo_errado() -> None:
    """Porque é o erro que produz exatamente este sintoma."""
    resultado = await _com_respostas(_cliente(), [(200, "1\n"), (200, "")]).test()

    assert "nome qualificado" in resultado.detail


@pytest.mark.asyncio
async def test_sem_permissao_de_catalogo_o_passo_e_inconclusivo_e_nao_reprova() -> None:
    """Usuário de ingestão INSERT-only não lê ``system.columns``, e isso é normal."""
    resultado = await _com_respostas(
        _cliente(username="nanosiem_ingest"),
        [(200, "1\n"), (403, "Code: 497. DB::Exception: ACCESS_DENIED")],
    ).test()

    assert resultado.ok is True
    assert "NÃO rodou" in resultado.detail
    assert "GRANT SHOW COLUMNS" in resultado.detail
    assert "nanosiem_ingest" in resultado.detail


# ── passo 3: a forma da linha contra as colunas ───────────────────────

@pytest.mark.asyncio
async def test_flat_contra_tabela_wrapper_reprova_explicando_a_linha_vazia() -> None:
    """O modo de falha que motivou tudo isto, barrado antes de chegar no fio."""
    resultado = await _com_respostas(
        _cliente(payload="ocsf"), [(200, "1\n"), (200, "event\nsource_type\n")]
    ).test()

    assert resultado.ok is False
    assert "VAZIAS" in resultado.detail
    assert "row_shape=wrapped" in resultado.detail


@pytest.mark.asyncio
async def test_wrapped_contra_tabela_wrapper_aprova() -> None:
    resultado = await _com_respostas(
        _cliente(
            payload="ocsf",
            row_shape="wrapped",
            event_key="event",
            row_fields={"source_type": "meu_feed"},
        ),
        [(200, "1\n"), (200, "event\nsource_type\ntimestamp\n")],
    ).test()

    assert resultado.ok is True
    assert "wrapped" in resultado.detail


@pytest.mark.asyncio
async def test_wrapped_com_coluna_de_rotulo_inexistente_reprova_nomeando_ela() -> None:
    resultado = await _com_respostas(
        _cliente(
            payload="ocsf",
            row_shape="wrapped",
            event_key="event",
            row_fields={"feed": "meu_feed"},
        ),
        [(200, "1\n"), (200, "event\nsource_type\n")],
    ).test()

    assert resultado.ok is False
    assert "'feed'" in resultado.detail


@pytest.mark.asyncio
async def test_flat_com_colunas_parciais_aprova_mas_lista_o_que_se_perde() -> None:
    """Passar em silêncio aqui seria o mesmo erro numa dose menor."""
    resultado = await _com_respostas(
        _cliente(payload="ocsf", table="ocsf_events"),
        [(200, "1\n"), (200, "class_uid\ncategory_uid\n")],
    ).test()

    assert resultado.ok is True
    assert "descartada" in resultado.detail
    assert "time" in resultado.detail


@pytest.mark.asyncio
async def test_flat_com_todas_as_colunas_aprova_limpo() -> None:
    resultado = await _com_respostas(
        _cliente(payload="ocsf", table="ocsf_events"),
        [(200, "1\n"), (200, "class_uid\ntime\n")],
    ).test()

    assert resultado.ok is True
    assert "todas as chaves têm coluna" in resultado.detail


# ── serialização do nome: identificador hostil na config ──────────────

def test_literal_sql_escapa_aspas_e_barra() -> None:
    """A config é de admin, mas o custo do cinto é uma linha."""
    assert ClickHouseClient._literal_sql("o'brien") == "o\\'brien"
    assert ClickHouseClient._literal_sql("a\\b") == "a\\\\b"
