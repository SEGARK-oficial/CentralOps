"""Ingestão OCSF direta: o fio, as barreiras e o destino ``nano``.

Estes testes existem por causa de um incidente concreto, e cada um trava uma das
peças que falharam nele.

Um destino ClickHouse apontado para um nano SIEM entregou **zero** eventos e
acumulou 10.884 na DLQ. Três erros de configuração somados a um gap de produto:

1. porta 9000 (protocolo nativo TCP) num sink que fala HTTP;
2. o nome qualificado inteiro no campo ``table``, com ``database`` no default,
   o que produz ``` `default`.`banco.tabela` ``` e nunca resolve;
3. a tabela do nano tem duas colunas, ``event`` e ``source_type``, e **nenhuma**
   forma de payload do produto produzia essa linha.

O item 3 é o que assusta. Medido contra um ClickHouse real: as chaves emitidas
não intersectam as colunas, ``input_format_skip_unknown_fields`` (default 1 no
servidor) descarta todas, e o INSERT responde **HTTP 200** gravando a quantidade
certa de linhas, todas vazias. Produtor conta entregue, consumidor conta
recebido, dado não existe.
"""

from __future__ import annotations

import json
import os
import urllib.parse

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
from pydantic import ValidationError

import backend.app.collectors.output.destinations as _destinos  # noqa: F401  (registros)
from backend.app.collectors.output.clickhouse_sender import ClickHouseClient
from backend.app.collectors.output.destinations import registry
from backend.app.collectors.output.destinations.clickhouse import ClickHouseConfig
from backend.app.collectors.output.destinations.nano import NanoConfig


ENVELOPE = {
    "normalized": {
        "class_uid": 4001,
        "category_uid": 4,
        "type_uid": 400101,
        "activity_id": 1,
        "severity_id": 1,
        "time": 1781234567890,
        "src_endpoint": {"ip": "198.51.100.7"},
        "metadata": {"product": {"name": "centralops"}, "version": "1.8.0"},
    },
    "raw": {"vendor_field": "x"},
    "_centralops": {"event_id": "evt-123", "organization_id": 7, "integration_id": 42},
}


class _ConfigFalsa:
    """Mínimo que ``factory`` consome, sem tocar no banco nem no cofre."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.secret_ref = None


def _cliente_nano(**overrides) -> ClickHouseClient:
    cfg = {"url": "http://nano.interno:8123", "source_type": "centralops_sophos"}
    cfg.update(overrides)
    return registry.get("nano").factory(_ConfigFalsa(cfg), None)


# ── o fio ─────────────────────────────────────────────────────────────

def test_a_linha_do_nano_tem_exatamente_event_e_source_type() -> None:
    """O teste que prova que a configuração do incidente passaria a funcionar."""
    linha = json.loads(_cliente_nano()._serialize([ENVELOPE]))

    assert set(linha) == {"event", "source_type"}
    assert linha["event"] == ENVELOPE["normalized"]
    assert linha["source_type"] == "centralops_sophos"


def test_o_envelope_canonico_nao_vaza_para_dentro_do_evento() -> None:
    linha = json.loads(_cliente_nano()._serialize([ENVELOPE]))

    assert "_centralops" not in linha["event"]
    assert "raw" not in linha["event"]
    assert "_centralops" not in linha


def test_a_linha_casa_com_o_exemplo_da_documentacao_do_nano() -> None:
    """Chave a chave contra o exemplo publicado, não contra a nossa intenção.

    O exemplo verbatim da doc do nano é
    ``{"event": {"class_uid": 4001, …}, "source_type": "my_firewall"}``.
    """
    exemplo_da_doc = {
        "event": {
            "class_uid": 4001,
            "category_uid": 4,
            "type_uid": 400101,
            "activity_id": 1,
            "severity_id": 1,
            "time": 1781234567890,
            "src_endpoint": {"ip": "198.51.100.7"},
            "metadata": {"product": {"name": "centralops"}, "version": "1.8.0"},
        },
        "source_type": "centralops_sophos",
    }

    assert json.loads(_cliente_nano()._serialize([ENVELOPE])) == exemplo_da_doc


def test_lote_multiplo_e_ndjson_sem_virgula_nem_newline_final() -> None:
    corpo = _cliente_nano()._serialize([ENVELOPE, ENVELOPE])
    linhas = corpo.split("\n")

    assert len(linhas) == 2
    assert not corpo.endswith("\n")
    assert all(json.loads(l) for l in linhas)


def test_evento_sem_normalizado_vira_objeto_vazio_e_nao_o_envelope() -> None:
    """Entregar o envelope a quem declarou esperar OCSF é a falha silenciosa."""
    linha = json.loads(_cliente_nano()._serialize([{"raw": {"a": 1}, "_centralops": {}}]))

    assert linha == {"event": {}, "source_type": "centralops_sophos"}


# ── o endpoint ────────────────────────────────────────────────────────

def _params(cliente: ClickHouseClient) -> dict:
    return {
        k: v[0]
        for k, v in urllib.parse.parse_qs(
            urllib.parse.urlparse(cliente._endpoint()).query
        ).items()
    }


def test_o_nano_aponta_para_a_tabela_do_caminho_http() -> None:
    """``ocsf_logs_native_raw`` é do protocolo NATIVO na 9000, não do caminho HTTP."""
    assert _params(_cliente_nano())["query"] == (
        "INSERT INTO `nanosiem`.`ocsf_logs_raw` FORMAT JSONEachRow"
    )


def test_o_nano_desliga_o_skip_para_falhar_alto() -> None:
    """A forma da linha é 100% controlada pelo preset e casa com as colunas.

    Logo, campo desconhecido significa que o schema do outro lado mudou. Com o
    skip ligado isso viraria 200 com linha vazia; com 0 vira erro 117 explícito.
    """
    assert _params(_cliente_nano())["input_format_skip_unknown_fields"] == "0"


def test_skip_desligado_manda_zero_explicito_em_vez_de_omitir() -> None:
    """O bug que tornava o controle decorativo.

    O código só ACRESCENTAVA o parâmetro quando ligado. Desligar apenas o omitia
    da query string, e como o default do SERVIDOR é 1, o toggle não tinha efeito
    nenhum: quem desligava continuava com os campos desconhecidos descartados.
    """
    ligado = ClickHouseClient("http://ch:8123", "s", skip_unknown_fields=True)
    desligado = ClickHouseClient("http://ch:8123", "s", skip_unknown_fields=False)

    assert _params(ligado)["input_format_skip_unknown_fields"] == "1"
    assert _params(desligado)["input_format_skip_unknown_fields"] == "0"


def test_chaves_emitidas_reflete_a_config_e_nao_uma_lista_fixa() -> None:
    """``test()`` compara isto com as colunas reais; se mentir, o teste mente."""
    assert _cliente_nano().chaves_emitidas() == {"event", "source_type"}

    flat = ClickHouseClient("http://ch:8123", "s", payload="ocsf")
    assert flat.chaves_emitidas() == {"class_uid", "time"}


# ── as barreiras do destino nano ──────────────────────────────────────

@pytest.mark.parametrize(
    "config, trecho_esperado",
    [
        ({"url": "http://h:9000", "source_type": "x"}, "porta 9000"),
        ({"url": "h:8123", "source_type": "x"}, "http://"),
        ({"url": "http://h:8123", "source_type": ""}, "obrigatório"),
        ({"url": "http://h:8123", "source_type": "meu feed"}, "espaço"),
        ({"url": "http://h:8123", "source_type": "MeuFeed"}, "minúsculo"),
        (
            {"url": "http://h:8123", "source_type": "x", "table": "nanosiem.ocsf_logs_raw"},
            "o banco vai em 'database'",
        ),
    ],
)
def test_o_nano_recusa_no_save_o_que_falharia_na_entrega(config, trecho_esperado) -> None:
    with pytest.raises(ValidationError) as erro:
        NanoConfig(**config)

    assert trecho_esperado in str(erro.value)


def test_a_mensagem_da_porta_diz_qual_usar() -> None:
    """Mensagem de erro que não diz o que fazer só troca um mistério por outro."""
    with pytest.raises(ValidationError) as erro:
        NanoConfig(url="http://h:9000", source_type="x")

    assert "8123" in str(erro.value)


def test_a_mensagem_do_source_type_entrega_a_forma_correta() -> None:
    with pytest.raises(ValidationError) as erro:
        NanoConfig(url="http://h:8123", source_type="CentralOps_Sophos")

    assert "centralops_sophos" in str(erro.value)


def test_a_mensagem_da_tabela_com_ponto_entrega_os_dois_campos() -> None:
    with pytest.raises(ValidationError) as erro:
        NanoConfig(url="http://h:8123", source_type="x", table="nanosiem.ocsf_logs_raw")

    texto = str(erro.value)
    assert "database='nanosiem'" in texto and "table='ocsf_logs_raw'" in texto


def test_o_nano_pede_so_dois_campos() -> None:
    """Plug and play é isto: onde, e com que rótulo. O resto tem default certo."""
    schema = registry.get("nano").describe()["config_schema"]

    assert sorted(schema["required"]) == ["source_type", "url"]

    cfg = NanoConfig(url="http://nano.interno:8123", source_type="meu_feed")
    assert cfg.database == "nanosiem"
    assert cfg.table == "ocsf_logs_raw"
    assert cfg.username == "nanosiem_ingest"


def test_o_nano_exige_credencial_no_catalogo() -> None:
    reg = registry.get("nano")

    assert reg.required_secrets == ("clickhouse_ingest_password",)
    assert "test" in reg.capabilities


def test_o_nano_reusa_o_cliente_do_clickhouse() -> None:
    """Se um dia virar sender próprio, este teste falha e a decisão vira consciente.

    Duplicar o sender para mudar um dicionário abriria a porta para um kind por
    produto que fala ClickHouse, com o catálogo apodrecendo em cópias.
    """
    assert isinstance(_cliente_nano(), ClickHouseClient)


# ── as barreiras do destino clickhouse genérico ───────────────────────

@pytest.mark.parametrize(
    "config, trecho_esperado",
    [
        ({"url": "http://h:9000"}, "porta 9000"),
        ({"url": "ch:8123"}, "http://"),
        ({"url": "http://h:8123", "table": "nanosiem.ocsf_logs_raw"}, "o banco vai em 'database'"),
        ({"url": "http://h:8123", "database": "a.b"}, "não pode conter ponto"),
        ({"url": "http://h:8123", "row_fields": {"source_type": "x"}}, "só tem efeito"),
        (
            {"url": "http://h:8123", "row_shape": "wrapped", "event_key": "  "},
            "event_key é obrigatório",
        ),
        (
            {"url": "http://h:8123", "row_shape": "wrapped", "row_fields": {"event": "x"}},
            "não pode redefinir",
        ),
        (
            {"url": "http://h:8123", "row_shape": "wrapped", "row_fields": {"a b": "x"}},
            "nome de coluna inválido",
        ),
    ],
)
def test_o_clickhouse_recusa_no_save(config, trecho_esperado) -> None:
    with pytest.raises(ValidationError) as erro:
        ClickHouseConfig(**config)

    assert trecho_esperado in str(erro.value)


def test_a_config_do_incidente_corrigida_passa_a_validar() -> None:
    cfg = ClickHouseConfig(
        url="http://198.51.100.10:8123",
        database="nanosiem",
        table="ocsf_logs_raw",
        username="nanosiem_ingest",
        payload="ocsf",
        row_shape="wrapped",
        event_key="event",
        row_fields={"source_type": "centralops_sophos"},
        verify_tls=False,
    )

    cliente = registry.get("clickhouse").factory(
        _ConfigFalsa(cfg.model_dump()), None
    )
    linha = json.loads(cliente._serialize([ENVELOPE]))

    assert linha == {"event": ENVELOPE["normalized"], "source_type": "centralops_sophos"}


# ── compatibilidade: nada muda para quem já entrega ───────────────────

def test_config_sem_os_campos_novos_cai_nos_defaults_antigos() -> None:
    cfg = ClickHouseConfig(url="http://ch:8123", table="centralops_events")

    assert cfg.row_shape == "flat"
    assert cfg.event_key == "event"
    assert cfg.row_fields == {}


@pytest.mark.parametrize(
    "payload, esperado_key",
    [("envelope", None), ("ocsf", "normalized")],
)
def test_o_fio_flat_continua_byte_identico(payload, esperado_key) -> None:
    cliente = ClickHouseClient(
        "http://ch:8123", "s", database="analytics", table="eventos", payload=payload
    )
    esperado = ENVELOPE if esperado_key is None else ENVELOPE[esperado_key]

    assert json.loads(cliente._serialize([ENVELOPE])) == esperado
