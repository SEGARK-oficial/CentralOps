"""GeoIP / ASN (W4.4): o enricher local sobre base MaxMind ``.mmdb``.

Sem base real no disco: o leitor é trocado por um fake com a MESMA forma de
saída do ``maxminddb`` (dicts aninhados e localizados), porque o que se testa
aqui é (1) o guard de caminho na config, (2) o achatamento estável dos campos,
(3) fail-loud na base ausente, (4) o custo declarado de memória — e (5) que
duas fontes do mesmo enricher na mesma org NÃO compartilham tabela.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from backend.app.collectors.enrich import enrichers as _enrichers  # noqa: F401
from backend.app.collectors.enrich import registry as registry_mod
from backend.app.collectors.enrich import runtime as runtime_mod
from backend.app.collectors.enrich.contract import EnrichContext
from backend.app.collectors.enrich.enrichers import geoip as geoip_mod
from backend.app.collectors.enrich.enrichers.geoip import (
    GeoIPConfig,
    GeoIPEnricher,
    flatten_record,
)
from backend.app.core.config import settings


CITY_RECORD = {
    "city": {"names": {"en": "São Paulo", "pt-BR": "São Paulo"}},
    "continent": {"code": "SA", "names": {"en": "South America"}},
    "country": {"iso_code": "BR", "names": {"en": "Brazil", "pt-BR": "Brasil"}},
    "location": {"latitude": -23.55, "longitude": -46.63, "accuracy_radius": 20, "time_zone": "America/Sao_Paulo"},
    "subdivisions": [{"iso_code": "SP", "names": {"en": "Sao Paulo"}}],
}
ASN_RECORD = {"autonomous_system_number": 64500, "autonomous_system_organization": "EXAMPLE-NET"}


class _FakeReader:
    def __init__(self, db_type: str, records: dict) -> None:
        self._db_type = db_type
        self._records = records
        self.closed = False

    def get(self, ip: str):
        # O maxminddb real levanta ValueError para IP inválido.
        if ip.count(".") != 3 and ":" not in ip:
            raise ValueError(f"{ip!r} does not appear to be an IPv4 or IPv6 address")
        return self._records.get(ip)

    def metadata(self):
        return SimpleNamespace(database_type=self._db_type, node_count=len(self._records))


@pytest.fixture
def geoip_dir(tmp_path, monkeypatch):
    (tmp_path / "GeoLite2-City.mmdb").write_bytes(b"\x00")
    (tmp_path / "GeoLite2-ASN.mmdb").write_bytes(b"\x00")
    monkeypatch.setattr(settings, "ENRICH_GEOIP_DIR", str(tmp_path))
    readers = {
        "GeoLite2-City.mmdb": _FakeReader("GeoLite2-City", {"203.0.113.7": CITY_RECORD}),
        "GeoLite2-ASN.mmdb": _FakeReader("GeoLite2-ASN", {"203.0.113.7": ASN_RECORD}),
    }
    opened: list = []

    def _open(path: str):
        opened.append(path)
        return readers[path.rsplit("/", 1)[-1]]

    monkeypatch.setattr(geoip_mod, "_open_reader", _open)
    return SimpleNamespace(path=tmp_path, opened=opened)


# ── registro / capacidades ──────────────────────────────────────────────────

def test_registrado_como_local_sem_egresso_e_nao_redistribuivel():
    reg = registry_mod.get("geoip")
    assert reg is not None
    assert reg.caps.mode == "local"
    assert reg.caps.egress == "none"
    assert reg.caps.key_kinds == frozenset({"ip"})
    # A base MaxMind não pode ir na imagem: é o caso que motivou o campo.
    assert reg.caps.redistributable is False
    assert reg.required_secrets == ()
    assert {"country_iso", "city", "latitude", "asn", "asn_org"} <= set(reg.output_fields)


# ── config: só o NOME do arquivo ─────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["/etc/passwd", "../x.mmdb", "sub/x.mmdb", "..\\x.mmdb", "x.dat", "", "   "])
def test_config_recusa_caminho_e_extensao_errada(bad):
    with pytest.raises(ValueError):
        GeoIPConfig(file=bad)


def test_config_recusa_kind_desconhecido_e_campo_extra():
    with pytest.raises(ValueError):
        GeoIPConfig(file="GeoLite2-City.mmdb", kind="continent")
    with pytest.raises(ValueError):
        GeoIPConfig(file="GeoLite2-City.mmdb", path="/x")


def test_config_aceita_nome_simples_e_normaliza_espacos():
    cfg = GeoIPConfig(file="  GeoLite2-City.mmdb ")
    assert cfg.file == "GeoLite2-City.mmdb"
    assert cfg.kind == "city"


# ── achatamento ─────────────────────────────────────────────────────────────

def test_flatten_city_devolve_campos_planos_em_ingles():
    out = flatten_record(CITY_RECORD, "city")
    assert out["country_iso"] == "BR"
    assert out["country_name"] == "Brazil"
    assert out["city"] == "São Paulo"
    assert out["subdivision_iso"] == "SP"
    assert out["latitude"] == -23.55 and out["longitude"] == -46.63
    assert out["timezone"] == "America/Sao_Paulo"
    assert out["continent_code"] == "SA"
    assert out["source"] == "geoip"


def test_flatten_country_nao_vaza_campos_de_cidade():
    out = flatten_record(CITY_RECORD, "country")
    assert out["country_iso"] == "BR"
    assert "city" not in out and "latitude" not in out


def test_flatten_asn():
    out = flatten_record(ASN_RECORD, "asn")
    assert out == {"source": "geoip", "asn": 64500, "asn_org": "EXAMPLE-NET"}


def test_flatten_usa_registered_country_quando_country_falta():
    out = flatten_record({"registered_country": {"iso_code": "DE", "names": {"en": "Germany"}}}, "country")
    assert out["country_iso"] == "DE" and out["country_name"] == "Germany"


@pytest.mark.parametrize("record", [None, {}, {"traits": {"is_anycast": True}}])
def test_flatten_vazio_e_miss_nao_hit_com_tudo_none(record):
    assert flatten_record(record, "city") is None
    assert flatten_record(record, "asn") is None


# ── carga ───────────────────────────────────────────────────────────────────

def test_base_ausente_falha_alto_na_carga(geoip_dir):
    e = GeoIPEnricher({"file": "GeoIP2-Enterprise.mmdb"})
    with pytest.raises(FileNotFoundError, match="ENRICH_GEOIP_DIR"):
        asyncio.run(e.load(EnrichContext(organization_id=1)))
    assert geoip_dir.opened == []


def test_lookup_hit_miss_e_ip_invalido(geoip_dir):
    tabela = asyncio.run(GeoIPEnricher({"file": "GeoLite2-City.mmdb"}).load(EnrichContext(organization_id=1)))
    hit = tabela.lookup("203.0.113.7")
    assert hit["country_iso"] == "BR" and hit["city"] == "São Paulo"
    assert tabela.lookup("198.51.100.1") is None
    # IP malformado = miss, nunca exceção no hot path.
    assert tabela.lookup("not-an-ip") is None
    assert tabela.entry_count == 1


def test_custo_declarado_e_resident_do_mmap_nao_o_tamanho_do_arquivo(geoip_dir):
    """Um GeoLite2-City tem ~70 MB; o teto por fork é 32 MiB. Se o enricher
    declarasse o tamanho do arquivo, o runtime recusaria a base mais útil do
    catálogo por um custo que mmap não tem."""
    tabela = asyncio.run(GeoIPEnricher({"file": "GeoLite2-City.mmdb"}).load(EnrichContext(organization_id=1)))
    assert 0 < tabela.approx_bytes < settings.ENRICH_MAX_TABLE_BYTES


def test_kind_incompativel_com_a_base_avisa(geoip_dir, caplog):
    with caplog.at_level(logging.WARNING):
        asyncio.run(GeoIPEnricher({"file": "GeoLite2-ASN.mmdb", "kind": "city"}).load(EnrichContext(organization_id=1)))
    assert any(getattr(r, "event", "") == "enrich.geoip.kind_mismatch" for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        asyncio.run(GeoIPEnricher({"file": "GeoLite2-ASN.mmdb", "kind": "asn"}).load(EnrichContext(organization_id=1)))
    assert not any(getattr(r, "event", "") == "enrich.geoip.kind_mismatch" for r in caplog.records)


# ── runtime: duas fontes, duas tabelas ──────────────────────────────────────

def _rule(rule_id: str, source: str):
    return SimpleNamespace(rule_id=rule_id, enricher="geoip", table=None, source=source)


def test_runtime_cacheia_por_fonte_nao_so_por_enricher(geoip_dir, monkeypatch):
    """City e ASN na mesma org são readers diferentes. Antes a chave do cache
    era ``(enricher, org, table)`` e a 2ª regra lia a tabela da 1ª: miss de
    100% no ASN, sem erro nenhum."""
    rt = runtime_mod.EnrichRuntime(max_table_bytes=settings.ENRICH_MAX_TABLE_BYTES, lru_bytes=64 << 20)
    configs = {
        "geo-city": {"file": "GeoLite2-City.mmdb", "kind": "city"},
        "geo-asn": {"file": "GeoLite2-ASN.mmdb", "kind": "asn"},
    }
    monkeypatch.setattr(rt, "_resolve_source", lambda org, name: (configs[name], None))
    reg = registry_mod.require("geoip")
    ctx = EnrichContext(organization_id=7)

    city = asyncio.run(rt._load_one(reg, _rule("r-city", "geo-city"), ctx))
    asn = asyncio.run(rt._load_one(reg, _rule("r-asn", "geo-asn"), ctx))
    assert city.lookup("203.0.113.7")["country_iso"] == "BR"
    assert asn.lookup("203.0.113.7")["asn"] == 64500
    assert len(geoip_dir.opened) == 2

    # Mesma fonte de novo = cache, não reabre o arquivo.
    asyncio.run(rt._load_one(reg, _rule("r-city-2", "geo-city"), ctx))
    assert len(geoip_dir.opened) == 2
