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
    normalizar_row_shape,
    normalizar_shape,
    render_payload,
    render_row,
    wrap_payload,
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
    # Tabela com nome NEUTRO de propósito. Antes este teste usava
    # ``ocsf_logs_native_raw``, que é a tabela do sink NATIVO do Tenzir na porta
    # 9000 e é coluna-wrapper: travar a combinação flat contra aquele nome
    # insinuava que ela funciona, e ela grava linha vazia. O comportamento
    # testado continua certo, e é para tabela com uma coluna por campo OCSF.
    from backend.app.collectors.output.clickhouse_sender import ClickHouseClient

    cliente = ClickHouseClient(
        "http://clickhouse:8123", "senha", database="analytics",
        table="ocsf_events", username="ingest", payload="ocsf",
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


# ── o segundo eixo: a forma da linha ──────────────────────────────────

def test_wrapped_aninha_o_ocsf_sob_a_chave_configurada() -> None:
    """A linha que uma tabela coluna-wrapper espera, e que antes era impossível."""
    linha = render_row(
        ENVELOPE,
        payload="ocsf",
        row_shape="wrapped",
        event_key="event",
        row_fields={"source_type": "meu_feed"},
    )

    assert set(linha) == {"event", "source_type"}
    assert linha["event"] == ENVELOPE["normalized"]
    assert linha["source_type"] == "meu_feed"
    assert "_centralops" not in linha["event"]


def test_wrapped_nao_copia_o_payload() -> None:
    """Contrato de identidade, não detalhe.

    O wrapper do HEC aninha por REFERÊNCIA e há teste de fio que compara com
    ``is``. Copiar aqui quebraria aquilo e ainda pagaria uma cópia de dicionário
    por evento no caminho quente, sem ganho nenhum.
    """
    linha = render_row(ENVELOPE, payload="ocsf", row_shape="wrapped", event_key="event")

    assert linha["event"] is ENVELOPE["normalized"]


def test_flat_continua_sendo_o_default() -> None:
    """Config existente não tem ``row_shape``; o fio dela não pode mudar."""
    assert render_row(ENVELOPE) == ENVELOPE
    assert render_row(ENVELOPE, payload="ocsf") == render_payload(ENVELOPE, "ocsf")
    assert render_row(ENVELOPE, payload="ocsf", row_shape="flat") == ENVELOPE["normalized"]


def test_row_fields_nao_sobrescreve_a_chave_do_evento() -> None:
    """Segunda linha de defesa: o schema já recusa, mas config antiga vem do banco."""
    linha = wrap_payload({"a": 1}, event_key="event", row_fields={"event": "sequestro"})

    assert linha == {"event": {"a": 1}}


def test_ordem_das_chaves_e_deterministica() -> None:
    """Há teste de contrato de fio que compara BYTES, então a ordem é contrato."""
    import json

    linha = wrap_payload(
        {"a": 1}, event_key="event", row_fields={"b": "2", "c": "3", "a": "1"}
    )

    assert json.dumps(linha, separators=(",", ":")) == '{"event":{"a":1},"b":"2","c":"3","a":"1"}'


@pytest.mark.parametrize("lixo", ["", "WRAPPED ", None, 42, "wrap", "flat"])
def test_row_shape_invalido_cai_em_flat(lixo) -> None:
    """Cair em ``flat`` é o conservador aqui: é o comportamento que já existia.

    O oposto de ``normalizar_shape``, que cai no envelope. A regra em comum é
    "lixo na config nunca muda o fio de quem já estava entregando".
    """
    esperado = "wrapped" if str(lixo).strip().lower() == "wrapped" else "flat"

    assert normalizar_row_shape(lixo) == esperado


def test_hec_continua_byte_identico_depois_de_generalizar_a_primitiva() -> None:
    """O HEC passou a chamar ``wrap_payload`` em vez de montar o dict na mão.

    Se a ordem das chaves mudasse, quebraria o contrato de fio do Splunk sem
    nenhum teste falhando de forma óbvia. Esta string é a saída congelada.
    """
    import json

    wrapper = format_hec_event(
        ENVELOPE, sourcetype="ocsf", index="idx", source="src", host="h", payload="ocsf"
    )

    assert list(wrapper) == ["event", "sourcetype", "index", "source", "host", "fields"]
    assert json.dumps(wrapper, separators=(",", ":")) == (
        '{"event":{"class_uid":4001,"activity_id":1,"severity_id":3,'
        '"metadata":{"version":"1.8.0"},"src_endpoint":{"ip":"198.51.100.7"}},'
        '"sourcetype":"ocsf","index":"idx","source":"src","host":"h",'
        '"fields":{"_centralops_event_id":"evt-123"}}'
    )


@pytest.mark.parametrize("kind", ["webhook", "clickhouse"])
def test_os_sinks_expoem_row_shape_como_lista(kind: str) -> None:
    """Mesmo motivo do ``payload``: texto livre aqui grava linha vazia em silêncio."""
    import backend.app.collectors.output.destinations as _  # dispara registros
    from backend.app.collectors.output.destinations import registry

    props = registry.get(kind).describe()["config_schema"]["properties"]

    assert sorted(props["row_shape"]["enum"]) == ["flat", "wrapped"]
    # ``dict[str, str]`` precisa sair assim para o JsonSchemaForm renderizar o
    # editor de pares chave/valor em vez de pular o campo.
    assert props["row_fields"]["type"] == "object"
    assert props["row_fields"]["additionalProperties"] == {"type": "string"}
