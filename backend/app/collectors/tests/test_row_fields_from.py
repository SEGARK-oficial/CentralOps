"""``row_fields_from``: um destino, N tenants, o rótulo certo em cada linha.

O que estes testes protegem não é a derivação em si — é o conjunto de modos de
falha SILENCIOSOS ao redor dela. Rótulo derivado erra sem exceção: a entrega
responde 200, a contagem de linhas fecha, e o dado aterrissa no balde do tenant
errado (ou em nenhum). Cada teste aqui existe porque a alternativa é descobrir
na tabela do cliente.
"""

from __future__ import annotations

import pytest

from backend.app.collectors.output.payload_shape import (
    ROW_FIELD_NAO_RESOLVIDO,
    derivar_row_fields,
    derivar_valor,
    render_row,
    slugificar_rotulo,
)


def _envelope(**meta):
    base = {
        "vendor": "sophos",
        "platform": "sophos",
        "integration_id": 7,
        "organization_id": 3,
        "organization_slug": "acme-corp",
        "stream": "alerts",
        "event_type": "sophos.alert",
    }
    base.update(meta)
    return {"_centralops": base, "normalized": {"class_uid": 2004}, "raw": {}}


# ── A derivação, origem por origem ───────────────────────────────────────────

@pytest.mark.parametrize(
    "origem,esperado",
    [
        ("organization", "acme_corp"),
        ("vendor", "sophos"),
        ("platform", "sophos"),
        ("integration", "integration_7"),
        ("stream", "alerts"),
        ("event_type", "sophos_alert"),
        ("organization_vendor", "acme_corp_sophos"),
        ("organization_stream", "acme_corp_alerts"),
    ],
)
def test_cada_origem_do_enum_resolve(origem, esperado):
    """Membro do enum sem implementação cairia no ``return ""`` final e viraria
    ``unresolved`` em produção — verde na config, rótulo perdido no fio."""
    assert derivar_valor(_envelope(), origem) == esperado


def test_o_enum_declarado_e_o_implementado_sao_o_MESMO_conjunto():
    """Guard contra o par mais fácil de dessincronizar.

    O ``Literal`` é o que a UI mostra e o schema aceita; o ``if/elif`` é o que
    de fato resolve. Adicionar ao primeiro sem o segundo passa em toda revisão:
    o operador escolhe a origem na lista, salva sem erro, e recebe
    ``unresolved`` em cada linha.
    """
    from typing import get_args

    from backend.app.collectors.output.payload_shape import RowFieldSource

    env = _envelope()
    nao_implementadas = [
        origem for origem in get_args(RowFieldSource) if not derivar_valor(env, origem)
    ]
    assert nao_implementadas == [], (
        f"origens no Literal sem ramo em derivar_valor: {nao_implementadas}"
    )


# ── Slug: o rótulo precisa sobreviver ao que humano digita ───────────────────

@pytest.mark.parametrize(
    "bruto,esperado",
    [
        ("Acme Corp", "acme_corp"),
        ("  Zaffari S/A  ", "zaffari_s_a"),
        ("Ação & Cia", "a_o_cia"),
        ("já-slug_ok", "j_slug_ok"),
        ("...", ""),
    ],
)
def test_slug_normaliza_o_que_o_nano_recusaria(bruto, esperado):
    """O nome da org é campo de texto livre. Espaço e maiúscula no
    ``source_type`` quebram o escopo das regras do lado do nano."""
    assert slugificar_rotulo(bruto) == esperado


# ── Precedência: derivado → literal → sentinela ──────────────────────────────

def test_derivado_ganha_do_literal_na_mesma_coluna():
    linha = derivar_row_fields(
        _envelope(), {"source_type": "organization"}, estaticos={"source_type": "fixo"}
    )
    assert linha == {"source_type": "acme_corp"}


def test_sem_slug_cai_no_id_e_NAO_no_nome():
    """``customer_name`` é editável na tela. Se o fallback fosse o nome, um
    rename mudaria o rótulo de todo evento NOVO e partiria o histórico do lado
    do consumidor em dois, sem erro em lugar nenhum."""
    env = _envelope(organization_slug=None, customer_name="Acme Corp")
    assert derivar_valor(env, "organization") == "org_3"


def test_sem_origem_nenhuma_cai_no_literal():
    env = _envelope(organization_slug=None, organization_id=None)
    linha = derivar_row_fields(
        env, {"source_type": "organization"}, estaticos={"source_type": "reserva"}
    )
    assert linha == {"source_type": "reserva"}


def test_sem_origem_e_sem_literal_emite_sentinela_consultavel():
    """String vazia seria indistinguível de coluna nunca preenchida. O
    sentinela responde ``WHERE source_type = 'unresolved'``."""
    env = _envelope(organization_slug=None, organization_id=None)
    linha = derivar_row_fields(env, {"source_type": "organization"})
    assert linha == {"source_type": ROW_FIELD_NAO_RESOLVIDO}
    assert ROW_FIELD_NAO_RESOLVIDO != ""


def test_composto_incompleto_nao_emite_meio_rotulo():
    """``acme_`` e ``acme`` parecem o mesmo tenant numa listagem e são chaves
    diferentes na tabela. Faltando uma parte, o valor inteiro cai na reserva."""
    env = _envelope(vendor="")
    linha = derivar_row_fields(
        env, {"source_type": "organization_vendor"}, estaticos={"source_type": "reserva"}
    )
    assert linha == {"source_type": "reserva"}


def test_origem_desconhecida_nao_derruba_a_entrega():
    """Config salva por uma versão mais nova, ou enum renomeado, chega aqui pelo
    banco. Levantar aqui pararia a entrega de TODOS os eventos do destino; o
    guard que recusa origem inválida vive no schema, na escrita."""
    assert derivar_valor(_envelope(), "inventado") == ""
    assert derivar_valor(_envelope(), None) == ""


def test_bool_nao_vira_rotulo():
    """``bool`` é ``int`` em Python. Sem o guard, um campo trocado sairia como
    ``true`` — um rótulo plausível o bastante para ninguém notar."""
    env = _envelope(vendor=True)
    assert derivar_valor(env, "vendor") == ""


def test_envelope_sem_centralops_nao_levanta():
    assert derivar_valor({"normalized": {}}, "organization") == ""


# ── O caminho de renderização ────────────────────────────────────────────────

def test_render_row_aplica_a_derivacao_no_modo_wrapped():
    linha = render_row(
        _envelope(),
        payload="ocsf",
        row_shape="wrapped",
        event_key="event",
        row_fields_from={"source_type": "organization_vendor"},
    )
    assert linha == {"event": {"class_uid": 2004}, "source_type": "acme_corp_sophos"}


def test_render_row_preserva_a_ordem_de_declaracao():
    """Há teste de contrato de fio que compara bytes serializados. Sobrescrever
    chave existente num dict não a move, e isto trava esse comportamento."""
    linha = render_row(
        _envelope(),
        row_shape="wrapped",
        row_fields={"source_type": "fixo", "tier": "prod"},
        row_fields_from={"source_type": "organization"},
    )
    assert list(linha.keys()) == ["event", "source_type", "tier"]
    assert linha["source_type"] == "acme_corp"


def test_flat_ignora_a_derivacao_inteira():
    """Sem isto, uma config incoerente injetaria colunas de rótulo no topo de
    uma linha que já mapeia campo-por-coluna."""
    linha = render_row(
        _envelope(), payload="ocsf", row_shape="flat",
        row_fields_from={"source_type": "organization"},
    )
    assert linha == {"class_uid": 2004}


def test_row_fields_from_vazio_nao_muda_um_byte():
    """O caminho quente de quem não usa a feature precisa sair idêntico."""
    env = _envelope()
    antes = render_row(env, row_shape="wrapped", row_fields={"a": "b"})
    depois = render_row(env, row_shape="wrapped", row_fields={"a": "b"}, row_fields_from={})
    assert antes == depois
    # O corpo continua indo por REFERÊNCIA — o teste de fio do HEC compara
    # identidade e uma cópia aqui o quebraria.
    assert depois["event"] is env


def test_dois_tenants_no_MESMO_destino_saem_rotulados_diferente():
    """O ponto da feature em uma linha. Se isto falhar, a stack compartilhada
    mistura clientes numa única faceta e o escopo das detecções deixa de valer.
    """
    a = render_row(
        _envelope(organization_slug="acme"), payload="ocsf", row_shape="wrapped",
        row_fields_from={"source_type": "organization_vendor"},
    )
    b = render_row(
        _envelope(organization_slug="beta", vendor="wazuh"), payload="ocsf",
        row_shape="wrapped", row_fields_from={"source_type": "organization_vendor"},
    )
    assert a["source_type"] == "acme_sophos"
    assert b["source_type"] == "beta_wazuh"
