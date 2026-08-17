"""O stream `sophos.siem_event` precisa classificar POR EVENTO.

O default carimbava `class_uid: 0` em 100% do stream, com a justificativa de que
"uma MappingDefinition carrega UM class_uid". A premissa era falsa — `class_uid`
aceita `value_map` como qualquer outra regra — e o custo foi medido: uma
detecção de PUA (`Event::Endpoint::CorePuaDetection`, severity medium) chegava
ao destino como Base Event, invisível para toda regra e todo painel que filtra
Detection Finding.

A tabela abaixo é a taxonomia REAL do vendor, extraída de 200 eventos de
produção de dois tenants. Os valores de `type`/`group`/`severity` são do
fabricante; hostname, id e IP são sintéticos.
"""

from __future__ import annotations

import json

import pytest

from backend.app.collectors.normalize.engine import default_engine
from backend.app.collectors.normalize.defaults import load_default_rules

#: 0 = Base Event (telemetria), 2004 = Detection Finding.
#: Cada linha foi observada em produção. Um `type` novo do fabricante NÃO
#: aparece aqui — quem cobre esse caso é a auditoria por `unmapped.event_group`
#: descrita no `_comment` do mapping, não este teste.
TAXONOMIA_REAL = [
    # (group, type, class_uid esperado)
    ("APPLICATION_CONTROL", "Event::Endpoint::Application::Blocked", 2004),
    ("PUA", "Event::Endpoint::CorePuaDetection", 2004),
    ("WEB", "Event::Endpoint::WebControlViolation", 2004),
    ("PERIPHERALS", "Event::Endpoint::Device::Blocked", 2004),
    ("PERIPHERALS", "Event::Endpoint::Device::AlertedOnly", 2004),
    ("MALWARE", "Event::Endpoint::Threat::Detected", 2004),
    ("UPDATING", "Event::Endpoint::UpdateSuccess", 0),
    ("UPDATING", "Event::Endpoint::UpdateFailure", 0),
    ("UPDATING", "Event::Endpoint::UpdateRebootRequired", 0),
    ("PROTECTION", "Event::Endpoint::SavScanComplete", 0),
    ("PROTECTION", "Event::Endpoint::ServiceNotRunning", 0),
    ("CONNECTIVITY", "Event::Firewall::Reconnected", 0),
    ("CONNECTIVITY", "Event::Firewall::LostConnectionToSophosCentral", 0),
    ("AD_SYNC", "Event::ADSync::Success", 0),
]

#: Chaves que os mapas de classe compartilham. Divergência entre eles quebra a
#: identidade `type_uid == class_uid*100 + activity_id` sem erro nenhum.
ALVOS_POR_GRUPO = (
    "normalized.class_uid",
    "normalized.class_name",
    "normalized.category_uid",
    "normalized.category_name",
    "normalized.activity_id",
    "normalized.activity_name",
    "normalized.type_uid",
)


@pytest.fixture(scope="module")
def regras():
    # ``load_default_rules`` (importlib.resources) e não ``Path(__file__)``:
    # na imagem compilada não existe árvore de fonte.
    return load_default_rules("sophos", "sophos.siem_event")


def _evento(group, tipo, *, endpoint_id="11111111-2222-3333-4444-555555555555"):
    """Evento sintético com a forma real do feed SIEM v1."""
    return {
        "type": tipo,
        "group": group,
        "severity": "medium",
        "name": f"amostra de {tipo}",
        "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "created_at": "2026-08-17T03:15:29.195Z",
        "when": "2026-08-17T03:15:24.000Z",
        "endpoint_id": endpoint_id,
        "endpoint_type": "server",
        "location": "host-exemplo",
        "source_info": {"ip": "198.51.100.10"},
        "source": "n/a",
        "customer_id": "99999999-8888-7777-6666-555555555555",
    }


def _normalizado(regras, evento):
    saida = default_engine.apply("v-test", regras, evento, dsl_version=2).output
    return saida.get("normalized", saida)


# ── A correção em si ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("group,tipo,esperado", TAXONOMIA_REAL)
def test_a_taxonomia_real_do_vendor_classifica(regras, group, tipo, esperado):
    assert _normalizado(regras, _evento(group, tipo))["class_uid"] == esperado


def test_uma_deteccao_nao_sai_como_Base_Event(regras):
    """O caso concreto que motivou tudo: PUA detectada, severity medium, e o
    destino recebia Base Event."""
    n = _normalizado(regras, _evento("PUA", "Event::Endpoint::CorePuaDetection"))
    assert n["class_uid"] == 2004
    assert n["class_name"] == "Detection Finding"
    assert n["category_uid"] == 2


def test_o_stream_nao_virou_finding_inteiro(regras):
    """O erro oposto é igualmente caro: carimbar 2004 em telemetria polui toda
    contagem de detecção no destino. Update de agente não é achado."""
    n = _normalizado(regras, _evento("UPDATING", "Event::Endpoint::UpdateSuccess"))
    assert n["class_uid"] == 0
    assert n["class_name"] == "Base Event"


# ── As invariantes que um value_map editado à mão quebra em silêncio ─────────

@pytest.mark.parametrize("group,tipo,_esperado", TAXONOMIA_REAL)
def test_a_identidade_type_uid_vale_evento_a_evento(regras, group, tipo, _esperado):
    """`type_uid == class_uid*100 + activity_id` é identidade do OCSF, não
    convenção. Sete mapas paralelos com chaves divergentes é a forma mais fácil
    de quebrá-la, e nenhum validador de sintaxe pega."""
    n = _normalizado(regras, _evento(group, tipo))
    assert n["type_uid"] == n["class_uid"] * 100 + n["activity_id"], (
        f"{tipo}: {n['class_uid']}*100+{n['activity_id']} != {n['type_uid']}"
    )


def test_os_mapas_de_classe_tem_o_MESMO_conjunto_de_chaves(regras):
    """Guard estrutural sobre a causa raiz da divergência.

    Sete regras leem `group` e precisam concordar. Acrescentar um grupo ao
    `class_uid` e esquecer do `type_uid` produz um evento com classe 2004 e
    type_uid 0 — que passa em qualquer teste que olhe só uma das duas.
    """
    por_alvo = {
        r["target"]: set((r.get("value_map") or {}).keys())
        for r in regras["rules"]
        if r.get("target") in ALVOS_POR_GRUPO
    }
    assert set(por_alvo) == set(ALVOS_POR_GRUPO), "regra de classe sumiu do mapping"

    referencia = por_alvo["normalized.class_uid"]
    assert referencia, "class_uid voltou a ser const — o stream inteiro vira uma classe só"
    divergentes = {
        alvo: sorted(chaves ^ referencia)
        for alvo, chaves in por_alvo.items()
        if chaves != referencia
    }
    assert divergentes == {}, f"chaves divergentes vs class_uid: {divergentes}"


@pytest.mark.parametrize("group,tipo,esperado", TAXONOMIA_REAL)
def test_finding_sempre_tem_titulo(regras, group, tipo, esperado):
    """`finding_info` é content-required da 2004. Um finding sem título chega ao
    destino como linha anônima."""
    n = _normalizado(regras, _evento(group, tipo))
    if esperado == 2004:
        assert n["finding_info"]["title"]


def test_so_emite_classe_que_o_manifesto_conhece(regras):
    """Se um dia a validação OCSF for fail-closed, uma classe fora do manifesto
    quarentena 100% do stream."""
    from backend.app.collectors.normalize.ocsf.classes import CLASS_NAMES

    emitidas = {
        v
        for r in regras["rules"]
        if r.get("target") == "normalized.class_uid"
        for v in list((r.get("value_map") or {}).values()) + [r.get("default")]
        if v is not None
    }
    assert emitidas <= set(CLASS_NAMES), f"fora do manifesto: {emitidas - set(CLASS_NAMES)}"


# ── O caminho de auditoria que o `_comment` promete ──────────────────────────

def test_grupo_desconhecido_cai_em_Base_Event_com_o_grupo_PRESERVADO(regras):
    """A rede de segurança contra a defasagem do value_map.

    Grupo novo do fabricante cai em Base Event — em silêncio, e não há como
    evitar. O que evita o silêncio VIRAR perda é o grupo cru chegar ao destino:
    `SELECT unmapped.event_group WHERE class_uid = 0` devolve o censo. Se
    alguém remover essa regra, o único caminho de auditoria some junto.
    """
    n = _normalizado(regras, _evento("GRUPO_QUE_A_SOPHOS_AINDA_NAO_INVENTOU", "Event::X::Y"))
    assert n["class_uid"] == 0
    assert n["unmapped"]["event_group"] == "GRUPO_QUE_A_SOPHOS_AINDA_NAO_INVENTOU"
    assert n["unmapped"]["event_type"] == "Event::X::Y"


def test_o_tipo_exato_sobrevive_em_event_code(regras):
    """`group` decide a CLASSE; só `type` diz qual evento foi. Sem event_code
    nenhuma regra a jusante distingue um siem_event de outro."""
    n = _normalizado(regras, _evento("PUA", "Event::Endpoint::CorePuaDetection"))
    assert n["metadata"]["event_code"] == "Event::Endpoint::CorePuaDetection"


# ── O `location` que não é hostname ──────────────────────────────────────────

def test_serial_de_appliance_nao_entra_como_hostname(regras):
    """Na família `Event::Firewall::*` o `location` carrega o SERIAL do
    appliance. `hostname_t` só aceita hostname, e um serial ali contamina toda
    busca e correlação por host."""
    firewall = _evento("CONNECTIVITY", "Event::Firewall::Reconnected", endpoint_id=None)
    firewall["location"] = "X133009DB7GDTB6"
    n = _normalizado(regras, firewall)
    assert n["device"].get("hostname") is None
    assert n["device"]["serial_number"] == "X133009DB7GDTB6"


def test_endpoint_continua_com_hostname(regras):
    """O outro lado da mesma regra — sem isto o fix custaria o hostname de todo
    evento de endpoint, que é a maioria do stream."""
    n = _normalizado(regras, _evento("UPDATING", "Event::Endpoint::UpdateSuccess"))
    assert n["device"]["hostname"] == "host-exemplo"
    assert n["device"].get("serial_number") is None


def test_o_json_do_default_continua_parseavel(regras):
    """Barato e pega o erro mais comum ao editar um arquivo de 300 linhas."""
    assert json.dumps(regras)
    assert len(regras["rules"]) > 30
