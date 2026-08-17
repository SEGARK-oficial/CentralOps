"""O nano com UMA stack para todos os clientes.

O destino ``nano`` foi desenhado com ``source_type`` literal, o que custa um
destino, uma rota e um segredo por cliente. Estes testes cobrem o caminho
alternativo — rótulo derivado do evento — e, principalmente, os guards que
impedem a config meio-preenchida, que é a que falha em silêncio.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.collectors.output.destinations.nano import NanoConfig, _factory
from backend.app.collectors.output.destinations.registry import DestinationConfig


def _envelope(slug="acme", vendor="sophos"):
    return {
        "_centralops": {
            "organization_slug": slug,
            "organization_id": 3,
            "vendor": vendor,
            "platform": vendor,
            "integration_id": 7,
            "stream": "alerts",
            "event_type": f"{vendor}.alert",
        },
        "normalized": {"class_uid": 2004, "time": 1},
        "raw": {"x": 1},
    }


def _cliente(**cfg):
    base = {"url": "http://nano.interno:8123"}
    base.update(cfg)
    return _factory(
        DestinationConfig(destination_id="d1", kind="nano", config=base, secret_ref=None)
    )


# ── Config ───────────────────────────────────────────────────────────────────

def test_os_dois_rotulos_vazios_sao_recusados_no_save():
    """Sem rótulo o nano joga tudo no balde 'unknown' e TODA regra com escopo de
    fonte ignora o feed: entrega verde, detecção zero. O erro precisa vir no
    save, não semanas depois numa investigação vazia."""
    with pytest.raises(ValidationError) as exc:
        NanoConfig(url="http://nano:8123")
    assert "source_type_from" in str(exc.value)


def test_derivado_sozinho_basta():
    cfg = NanoConfig(url="http://nano:8123", source_type_from="organization_vendor")
    assert cfg.source_type == ""


def test_origem_fora_do_enum_e_recusada_no_save():
    """A escrita é onde a origem inválida tem que morrer — no caminho de
    entrega ela é ignorada de propósito, para não parar o destino inteiro."""
    with pytest.raises(ValidationError):
        NanoConfig(url="http://nano:8123", source_type_from="organizacao")


def test_literal_continua_valendo_sozinho():
    cfg = NanoConfig(url="http://nano:8123", source_type="centralops_sophos")
    assert cfg.source_type_from is None


def test_literal_maiusculo_ainda_e_recusado():
    """O guard antigo não pode ter sido afrouxado ao tornar o campo opcional: o
    rótulo literal também é digitado do lado do nano, nas regras."""
    with pytest.raises(ValidationError):
        NanoConfig(url="http://nano:8123", source_type="CentralOps")


# ── Fio ──────────────────────────────────────────────────────────────────────

def test_um_destino_rotula_cada_tenant_com_a_origem_dele():
    cliente = _cliente(source_type_from="organization_vendor")
    a = cliente.format(_envelope(slug="acme"))
    b = cliente.format(_envelope(slug="beta", vendor="wazuh"))
    assert a == {"event": {"class_uid": 2004, "time": 1}, "source_type": "acme_sophos"}
    assert b == {"event": {"class_uid": 2004, "time": 1}, "source_type": "beta_wazuh"}


def test_o_contrato_de_fio_do_nano_nao_mudou():
    """A linha continua sendo ``{event, source_type}`` e o corpo continua sendo
    o OCSF puro — nada do envelope canônico vaza."""
    linha = _cliente(source_type_from="organization").format(_envelope())
    assert set(linha) == {"event", "source_type"}
    assert "_centralops" not in linha["event"]


def test_literal_e_o_valor_de_reserva_do_derivado():
    cliente = _cliente(source_type="centralops_fallback", source_type_from="organization")
    sem_org = _envelope(slug=None)
    sem_org["_centralops"]["organization_id"] = None
    assert cliente.format(sem_org)["source_type"] == "centralops_fallback"


def test_sem_derivar_e_sem_reserva_o_rotulo_e_consultavel():
    cliente = _cliente(source_type_from="organization")
    sem_org = _envelope(slug=None)
    sem_org["_centralops"]["organization_id"] = None
    assert cliente.format(sem_org)["source_type"] == "unresolved"


def test_o_rotulo_derivado_respeita_o_que_o_nano_aceita():
    """O nome da org vem de campo de texto livre. Espaço ou maiúscula no
    ``source_type`` quebra o escopo das regras do lado do nano — e aqui não há
    validador, porque o valor não passa pelo formulário."""
    rotulo = _cliente(source_type_from="organization").format(
        _envelope(slug="Grupo Acme S/A")
    )["source_type"]
    assert rotulo == rotulo.lower()
    assert " " not in rotulo
    assert all(c.isalnum() or c == "_" for c in rotulo)


def test_a_coluna_derivada_aparece_na_sonda_de_forma():
    """``test()`` compara ``chaves_emitidas()`` com as colunas reais da tabela.
    Se a coluna derivada não aparecesse na sonda, o teste de conexão diria OK
    para uma config que grava numa coluna inexistente."""
    assert _cliente(source_type_from="organization").chaves_emitidas() == {
        "event",
        "source_type",
    }
