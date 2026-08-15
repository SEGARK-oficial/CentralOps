"""Emitir OCSF 1.8 puro é configuração, não um destino novo.

Um consumidor nativo de OCSF (nano, Cribl, Tenzir, ou uma tabela cujo DDL saiu
do schema OCSF) não quer o envelope canônico do CentralOps: o namespace
``_centralops`` ou é descartado em silêncio, e o operador acha que entregou
enquanto o dado chega mutilado, ou derruba o lote inteiro por coluna
desconhecida.

Antes disto só o webhook sabia escolher, por um campo de texto livre chamado
``body``. HEC e ClickHouse mandavam o envelope sempre. Estes testes travam o
comportamento nos três, e travam também a compatibilidade: destino já
configurado com ``body="normalized"`` continua entregando OCSF.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors.output.payload_shape import (
    normalizar_shape,
    render_payload,
)
from backend.app.collectors.output.splunk_hec_sender import format_hec_event


ENVELOPE = {
    "normalized": {
        "class_uid": 4001,
        "activity_id": 1,
        "severity_id": 3,
        "metadata": {"version": "1.8.0"},
        "src_endpoint": {"ip": "198.51.100.7"},
    },
    "raw": {"vendor_field": "x"},
    "_centralops": {
        "event_id": "evt-123",
        "organization_id": 7,
        "integration_id": 42,
    },
}


# ── a primitiva ───────────────────────────────────────────────────────

def test_ocsf_entrega_so_o_evento_normalizado() -> None:
    saida = render_payload(ENVELOPE, "ocsf")

    assert saida == ENVELOPE["normalized"]
    assert "_centralops" not in saida, (
        "O namespace interno vazou para um consumidor que pediu OCSF puro."
    )
    assert "raw" not in saida


def test_envelope_continua_sendo_o_default() -> None:
    """Mudar o default silenciosamente quebraria todo destino existente."""
    assert render_payload(ENVELOPE) == ENVELOPE
    assert render_payload(ENVELOPE, "envelope") == ENVELOPE


def test_nome_historico_normalized_continua_valendo() -> None:
    """Config já gravada não pode mudar de comportamento sem migração."""
    assert render_payload(ENVELOPE, "normalized") == ENVELOPE["normalized"]
    assert normalizar_shape("normalized") == "ocsf"


@pytest.mark.parametrize("lixo", ["", "OCSF ", None, 42, "envelop", "json"])
def test_valor_invalido_cai_no_envelope(lixo) -> None:
    """Menos o "OCSF " com espaço, que é erro de digitação e deve ser aceito."""
    esperado = ENVELOPE["normalized"] if str(lixo).strip().lower() == "ocsf" else ENVELOPE
    assert render_payload(ENVELOPE, lixo) == esperado


def test_evento_sem_normalizado_nao_vira_envelope_disfarcado() -> None:
    """Entregar o envelope a quem pediu OCSF é pior que entregar vazio.

    Um campo a mais numa tabela estrita derruba o lote inteiro, e o operador
    procura o defeito no consumidor em vez de na origem.
    """
    sem_normalizado = {"raw": {"a": 1}, "_centralops": {"event_id": "x"}}

    assert render_payload(sem_normalizado, "ocsf") == {}


# ── os três destinos ──────────────────────────────────────────────────

def test_hec_em_modo_ocsf_embala_so_o_evento() -> None:
    wrapper = format_hec_event(ENVELOPE, sourcetype="ocsf", payload="ocsf")

    assert wrapper["event"] == ENVELOPE["normalized"]
    assert wrapper["sourcetype"] == "ocsf"


def test_hec_mantem_o_event_id_indexado_mesmo_em_ocsf() -> None:
    """O id vai em ``fields``, que é metadado de transporte do HEC.

    Ele não entra dentro do evento, então não contamina o OCSF entregue, e sem
    ele o indexer perde a única chave de deduplicação que existe.
    """
    wrapper = format_hec_event(ENVELOPE, payload="ocsf")

    assert wrapper["fields"] == {"_centralops_event_id": "evt-123"}
    assert "_centralops" not in wrapper["event"]


def test_hec_default_continua_mandando_o_envelope() -> None:
    wrapper = format_hec_event(ENVELOPE)

    assert wrapper["event"] == ENVELOPE


def test_clickhouse_em_modo_ocsf_grava_a_linha_ocsf() -> None:
    from backend.app.collectors.output.clickhouse_sender import ClickHouseClient

    cliente = ClickHouseClient(
        "http://clickhouse:8123", "senha", database="nanosiem",
        table="ocsf_logs_native_raw", username="ingest", payload="ocsf",
    )

    assert cliente.format(ENVELOPE) == ENVELOPE["normalized"]


def test_clickhouse_default_continua_gravando_o_envelope() -> None:
    from backend.app.collectors.output.clickhouse_sender import ClickHouseClient

    cliente = ClickHouseClient("http://clickhouse:8123", "senha")

    assert cliente.format(ENVELOPE) == ENVELOPE


# ── o contrato que a UI lê ────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["webhook", "clickhouse", "splunk_hec"])
def test_os_tres_expoem_o_formato_como_lista_no_catalogo(kind: str) -> None:
    """Sem ``enum`` no JSON Schema a UI desenha caixa de texto.

    Aí um valor errado não dá erro: cai no ramo default e o destino recebe o
    formato que ninguém pediu. A lista fechada é o que torna isso impossível.
    """
    import backend.app.collectors.output.destinations as _  # dispara registros
    from backend.app.collectors.output.destinations import registry

    prop = registry.get(kind).describe()["config_schema"]["properties"]["payload"]
    # ``Optional[...]`` vira anyOf; a UI resolve o ramo não-nulo.
    opcoes = prop.get("enum") or [
        ramo["enum"] for ramo in prop.get("anyOf", []) if ramo.get("enum")
    ][0]

    assert sorted(opcoes) == ["envelope", "ocsf"]
