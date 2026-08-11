"""TAXII 2.1: parsing STIX, contrato HTTP e os guards de segurança.

O enricher fala com QUALQUER plataforma que implemente o padrão OASIS (MISP,
OpenCTI, Anomali, ThreatConnect, EclecticIQ), então os testes miram o CONTRATO
do padrão, não o comportamento de um fornecedor.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.collectors.enrich import enrichers as _enrichers  # noqa: F401
from backend.app.collectors.enrich import registry as registry_mod
from backend.app.collectors.enrich.contract import EnrichContext
from backend.app.collectors.enrich.enrichers.taxii import TaxiiConfig, TaxiiEnricher
from backend.app.collectors.enrich.stix import (
    extract_observable_from_pattern,
    is_expired,
    parse_indicator,
)


# ── parsing STIX (compartilhado com o OpenCTI) ──────────────────────────────

@pytest.mark.parametrize(
    "pattern,esperado",
    [
        ("[ipv4-addr:value = '185.220.101.5']", ("ip", "185.220.101.5")),
        ("[ipv6-addr:value = '2001:db8::1']", ("ip", "2001:db8::1")),
        ("[domain-name:value = 'evil.example']", ("domain", "evil.example")),
        ("[url:value = 'http://evil.example/x']", ("url", "http://evil.example/x")),
        ("[file:hashes.'SHA-256' = 'abc123']", ("file_hash", "abc123")),
        ("[mac-addr:value = '00:11:22:33:44:55']", ("mac", "00:11:22:33:44:55")),
    ],
)
def test_extrai_padrao_stix_de_um_termo(pattern, esperado):
    assert extract_observable_from_pattern(pattern) == esperado


def test_padrao_composto_e_descartado():
    """Avaliar só o primeiro termo daria hit ERRADO em silêncio.

    Casar um evento contra `A AND B` exige avaliar a expressão inteira. Meia
    avaliação é pior que nenhuma: produz alerta que ninguém consegue justificar.
    """
    assert extract_observable_from_pattern(
        "[ipv4-addr:value='1.1.1.1'] AND [domain-name:value='x.com']"
    ) is None


def test_tipo_stix_desconhecido_e_ignorado_nao_adivinhado():
    """`email-addr` indexado como `domain` produziria hit errado silencioso."""
    assert extract_observable_from_pattern("[email-addr:value = 'a@b.com']") is None


def test_indicador_revogado_ou_expirado_nao_entra_na_tabela():
    """Intel vencida é a maior fonte de falso positivo num feed de TI."""
    base = {
        "type": "indicator",
        "pattern": "[ipv4-addr:value = '1.2.3.4']",
        "confidence": 90,
    }
    assert parse_indicator({**base, "revoked": True}) is None
    assert parse_indicator({**base, "valid_until": "2020-01-01T00:00:00Z"}) is None
    # Válido no futuro entra.
    ok = parse_indicator({**base, "valid_until": "2099-01-01T00:00:00Z"})
    assert ok is not None and ok[0] == "1.2.3.4"


def test_data_de_validade_ilegivel_nao_descarta():
    """Trocar falso positivo por falso NEGATIVO seria o erro caro."""
    assert is_expired("nao-e-data") is False
    ok = parse_indicator({
        "type": "indicator", "pattern": "[ipv4-addr:value = '9.9.9.9']",
        "valid_until": "nao-e-data",
    })
    assert ok is not None


def test_piso_de_confianca_filtra_na_carga():
    obj = {"type": "indicator", "pattern": "[ipv4-addr:value = '5.5.5.5']", "confidence": 30}
    assert parse_indicator(obj, min_confidence=50) is None
    assert parse_indicator(obj, min_confidence=10) is not None


def test_chave_sai_minuscula_para_casar_com_normalize_lower():
    """As duas pontas TÊM que concordar: divergir dá miss de 100% sem erro."""
    key, _ = parse_indicator({
        "type": "indicator", "pattern": "[domain-name:value = 'EVIL.Example']",
    })
    assert key == "evil.example"


def test_linha_carrega_o_contexto_que_torna_o_hit_acionavel():
    _, row = parse_indicator({
        "type": "indicator",
        "id": "indicator--1",
        "name": "C2 conhecido",
        "pattern": "[ipv4-addr:value = '1.2.3.4']",
        "confidence": 80,
        "labels": ["malicious-activity"],
        "kill_chain_phases": [{"phase_name": "command-and-control"}],
    })
    assert row["confidence"] == 80
    assert row["kill_chain_phases"] == ["command-and-control"]
    assert row["labels"] == ["malicious-activity"]
    assert row["source"] == "taxii"


# ── guards de segurança ─────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "https://user:senha@tip.exemplo",
    "https://tip.exemplo/caminho?x=1",
])
def test_url_passa_pelo_guard_de_egresso(url):
    """A URL vai para o aiohttp COM a credencial no Authorization.

    Sem o guard, um endereço de metadados de nuvem ou host externo viraria SSRF
    com exfiltração de credencial.
    """
    with pytest.raises(Exception):
        TaxiiConfig(url=url, collection="c1")


def test_config_nao_aceita_campo_de_segredo():
    """A credencial entra só pelo campo `secret` da fonte, cifrado no servidor."""
    assert "token" not in TaxiiConfig.model_fields
    assert not [f for f in TaxiiConfig.model_fields if "secret" in f.lower()]


def test_registro_declara_credencial_obrigatoria_e_egresso_de_entrada():
    reg = registry_mod.require("taxii")
    assert reg.required_secrets == ("token",)
    # Buscamos a lista; nada do ambiente do cliente sai.
    assert reg.caps.egress == "internal"
    assert reg.caps.mode == "local"  # roda por evento, alimenta a detecção


def test_schema_exposto_a_ui_tem_enum_para_auth_mode():
    """`Literal` vira `enum` no schema; a UI rende dropdown em vez de texto."""
    sch = TaxiiConfig.model_json_schema()
    prop = sch["properties"]["auth_mode"]
    valores = prop.get("enum") or [
        s.get("const") for s in prop.get("anyOf", []) if "const" in s
    ]
    assert set(valores) >= {"basic", "bearer", "none"}


# ── contrato HTTP do padrão ─────────────────────────────────────────────────

def test_url_de_objetos_segue_o_padrao_oasis():
    e = TaxiiEnricher({"url": "https://tip.exemplo", "collection": "abc"})
    assert e._objects_url() == "https://tip.exemplo/taxii2/collections/abc/objects/"


def test_barra_final_na_url_nao_duplica_caminho():
    e = TaxiiEnricher({"url": "https://tip.exemplo/", "collection": "abc"})
    assert e._objects_url() == "https://tip.exemplo/taxii2/collections/abc/objects/"


def test_auth_bearer_e_basic_montam_o_header_certo():
    import base64

    bearer = TaxiiEnricher({"url": "https://t.exemplo", "collection": "c"})
    assert bearer._auth_header("tok") == {"Authorization": "Bearer tok"}

    basic = TaxiiEnricher({
        "url": "https://t.exemplo", "collection": "c",
        "auth_mode": "basic", "username": "u",
    })
    esperado = "Basic " + base64.b64encode(b"u:senha").decode()
    assert basic._auth_header("senha") == {"Authorization": esperado}

    anon = TaxiiEnricher({"url": "https://t.exemplo", "collection": "c", "auth_mode": "none"})
    assert anon._auth_header("tok") == {}


def test_carrega_paginando_por_more_e_next(monkeypatch):
    """A paginação do TAXII é `more`+`next`, não offset."""
    paginas = [
        {"more": True, "next": "cursor-1", "objects": [
            {"type": "indicator", "pattern": "[ipv4-addr:value = '1.1.1.1']", "confidence": 70},
        ]},
        {"more": False, "objects": [
            {"type": "indicator", "pattern": "[domain-name:value = 'evil.example']", "confidence": 70},
            {"type": "malware", "name": "ignorado"},  # não-indicator é descartado
        ]},
    ]
    vistos = _FakeSession.install(monkeypatch, paginas)

    e = TaxiiEnricher({"url": "https://tip.exemplo", "collection": "c1"})
    tabela = asyncio.run(e.load(EnrichContext(organization_id=1)))

    assert tabela.lookup("1.1.1.1") is not None
    assert tabela.lookup("evil.example") is not None
    # Media type do padrão e filtro de tipo NO SERVIDOR.
    assert vistos[0]["headers"]["Accept"] == "application/taxii+json;version=2.1"
    assert vistos[0]["params"]["match[type]"] == "indicator"
    # A 2ª requisição usa o cursor devolvido pela 1ª.
    assert vistos[1]["params"]["next"] == "cursor-1"


def test_more_sem_next_para_em_vez_de_repetir_a_pagina(monkeypatch):
    """Servidor fora do padrão não pode virar laço infinito."""
    paginas = [{"more": True, "objects": [
        {"type": "indicator", "pattern": "[ipv4-addr:value = '2.2.2.2']"},
    ]}]
    vistos = _FakeSession.install(monkeypatch, paginas, repetir_ultima=True)
    e = TaxiiEnricher({"url": "https://tip.exemplo", "collection": "c1"})
    tabela = asyncio.run(e.load(EnrichContext(organization_id=1)))
    assert tabela.lookup("2.2.2.2") is not None
    assert len(vistos) == 1, "deveria parar na primeira página"


def test_401_vira_erro_de_credencial_e_nao_tabela_vazia(monkeypatch):
    _FakeSession.install(monkeypatch, [], status=401)
    e = TaxiiEnricher({"url": "https://tip.exemplo", "collection": "c1"})
    with pytest.raises(PermissionError, match="credencial"):
        asyncio.run(e.load(EnrichContext(organization_id=1)))


def test_406_avisa_media_type_em_vez_de_tabela_vazia(monkeypatch):
    """Tratar 406 como vazio produziria tabela vazia SILENCIOSA."""
    _FakeSession.install(monkeypatch, [], status=406)
    e = TaxiiEnricher({"url": "https://tip.exemplo", "collection": "c1"})
    with pytest.raises(ValueError, match="media type"):
        asyncio.run(e.load(EnrichContext(organization_id=1)))


def test_teto_de_paginas_trunca_em_vez_de_drenar_o_ciclo(monkeypatch):
    """Mesmo poison-loop que já derrubou coletor neste produto."""
    pagina = {"more": True, "next": "c", "objects": [
        {"type": "indicator", "pattern": "[ipv4-addr:value = '3.3.3.3']"},
    ]}
    vistos = _FakeSession.install(monkeypatch, [pagina], repetir_ultima=True)
    e = TaxiiEnricher({
        "url": "https://tip.exemplo", "collection": "c1", "max_pages": 3,
    })
    asyncio.run(e.load(EnrichContext(organization_id=1)))
    assert len(vistos) == 3


# ── harness ─────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, body, status):
        self._body, self.status = body, status

    async def json(self, content_type=None):
        return self._body

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    """Substitui `aiohttp.ClientSession` e registra o que foi pedido."""

    def __init__(self, paginas, vistos, status, repetir_ultima):
        self._paginas, self._vistos = paginas, vistos
        self._status, self._repetir = status, repetir_ultima
        self._i = 0

    @classmethod
    def install(cls, monkeypatch, paginas, *, status=200, repetir_ultima=False):
        vistos: list = []
        import backend.app.collectors.enrich.enrichers.taxii as mod

        monkeypatch.setattr(
            mod.aiohttp, "ClientSession",
            lambda *a, **k: cls(paginas, vistos, status, repetir_ultima),
        )
        monkeypatch.setattr(mod.aiohttp, "TCPConnector", lambda *a, **k: None)
        return vistos

    def get(self, url, headers=None, params=None):
        self._vistos.append({"url": url, "headers": headers, "params": dict(params or {})})
        if self._i < len(self._paginas):
            body = self._paginas[self._i]
        elif self._repetir and self._paginas:
            body = self._paginas[-1]
        else:
            body = {"more": False, "objects": []}
        self._i += 1
        return _FakeResp(body, self._status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False
