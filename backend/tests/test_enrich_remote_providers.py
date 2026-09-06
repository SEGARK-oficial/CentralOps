"""AbuseIPDB, OTX e GreyNoise (W4.4) sobre o esqueleto remoto comum.

Sessão HTTP falsa: o que se prova é o CONTRATO — cota antes da requisição,
429 trava o lote e sobe só quando nada resolveu, 401 é ``auth`` e não
``unknown``, IP privado nunca sai, MISS de cada provedor tem a semântica
documentada, e o parse devolve campos planos com nome fixo.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from backend.app.collectors.enrich import enrichers as _enrichers  # noqa: F401
from backend.app.collectors.enrich import ratelimit
from backend.app.collectors.enrich import registry as registry_mod
from backend.app.collectors.enrich import remote
from backend.app.collectors.enrich.contract import EnrichContext
from backend.app.collectors.enrich.enrichers import abuseipdb, greynoise, otx
from backend.app.collectors.enrich.runtime import _error_reason


class _Resp:
    def __init__(self, status: int, body: Any = None, headers: Optional[dict] = None):
        self.status, self._body, self.headers = status, body, headers or {}

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def json(self, content_type=None): return self._body
    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _Session:
    def __init__(self, responder):
        self.calls: List[Dict[str, Any]] = []
        self.headers: Dict[str, str] = {}
        self._responder = responder

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    def get(self, url: str, params: Optional[dict] = None):
        self.calls.append({"url": url, "params": dict(params or {})})
        return self._responder(url, dict(params or {}))


def _env(monkeypatch, responder, *, api_key: Optional[str] = "chave-de-teste"):
    ratelimit.reset_registry()
    session = _Session(responder)

    def _factory(**kw):
        session.headers = dict(kw.get("headers") or {})
        return session
    monkeypatch.setattr(remote.aiohttp, "ClientSession", _factory)

    async def _key(ctx): return api_key
    monkeypatch.setattr(remote, "resolve_api_key", _key)
    # os enrichers importaram o nome: patch neles também
    for mod in (abuseipdb, otx, greynoise):
        monkeypatch.setattr(mod, "resolve_api_key", _key)
    return session


def _run(enricher, keys):
    return asyncio.run(enricher.resolve(keys, EnrichContext(organization_id=1)))


# ── registro ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,kinds,secrets", [
    ("abuseipdb", {"ip"}, ("api_key",)),
    ("otx", {"ip", "domain", "file_hash", "url"}, ("api_key",)),
    ("greynoise", {"ip"}, ()),
])
def test_registrados_como_remotos_de_terceiro(name, kinds, secrets):
    reg = registry_mod.require(name)
    assert reg.caps.mode == "remote" and reg.caps.egress == "third_party"
    assert reg.caps.key_kinds == frozenset(kinds)
    assert reg.required_secrets == secrets
    assert reg.caps.supports_bulk and reg.caps.redistributable is False
    assert "source" in reg.output_fields


def test_excecoes_classificam_rate_limit_e_auth_na_metrica():
    assert _error_reason(remote.RemoteQuotaExceeded("x")) == "rate_limit"
    assert _error_reason(remote.ProviderUnauthorized("x")) == "auth"


@pytest.mark.parametrize("ip,public", [
    ("185.220.101.5", True), ("8.8.8.8", True), ("2606:4700:4700::1111", True),
    ("10.0.5.7", False), ("192.168.1.1", False), ("172.16.0.1", False), ("127.0.0.1", False),
    ("169.254.1.1", False), ("::1", False), ("0.0.0.0", False), ("224.0.0.1", False), ("banana", False),
])
def test_is_public_ip(ip, public):
    assert remote.is_public_ip(ip) is public


# ── AbuseIPDB ──────────────────────────────────────────────────────────────

def _abuse(score, reports, **extra):
    d = {"ipAddress": "185.220.101.5", "abuseConfidenceScore": score, "totalReports": reports,
         "numDistinctUsers": 3, "lastReportedAt": "2026-09-05T10:00:00+00:00", "countryCode": "NL",
         "usageType": "Data Center/Web Hosting/Transit", "isp": "Example Hosting", "domain": "example.net",
         "hostnames": ["a.example.net"], "isTor": False, "isWhitelisted": False, "isPublic": True}
    d.update(extra)
    return {"data": d}


def test_abuseipdb_hit_parse_headers_e_params(monkeypatch):
    s = _env(monkeypatch, lambda url, p: _Resp(200, _abuse(87, 42)))
    out = _run(abuseipdb.AbuseIPDBEnricher({"max_age_days": 30}), ["185.220.101.5"])
    row = out["185.220.101.5"]
    assert row["abuse_confidence_score"] == 87 and row["total_reports"] == 42 and row["isp"] == "Example Hosting"
    assert row["source"] == "abuseipdb" and row["is_tor"] is False
    assert s.headers["Key"] == "chave-de-teste"
    assert s.calls[0]["url"].endswith("/check") and s.calls[0]["params"]["maxAgeInDays"] == "30"


def test_abuseipdb_sem_relato_e_abaixo_do_piso_sao_miss(monkeypatch):
    _env(monkeypatch, lambda url, p: _Resp(200, _abuse(0, 0)))
    assert _run(abuseipdb.AbuseIPDBEnricher({}), ["185.220.101.5"]) == {"185.220.101.5": None}
    _env(monkeypatch, lambda url, p: _Resp(200, _abuse(40, 5)))
    assert _run(abuseipdb.AbuseIPDBEnricher({"min_confidence_score": 75}), ["185.220.101.5"]) == {"185.220.101.5": None}
    assert _run(abuseipdb.AbuseIPDBEnricher({"min_confidence_score": 25}), ["185.220.101.5"])["185.220.101.5"]["abuse_confidence_score"] == 40


def test_abuseipdb_422_e_miss_e_ip_privado_nem_sai(monkeypatch):
    s = _env(monkeypatch, lambda url, p: _Resp(422, {"errors": [{"detail": "private"}]}))
    out = _run(abuseipdb.AbuseIPDBEnricher({}), ["10.0.5.7", "127.0.0.1", "185.220.101.5"])
    assert out == {"185.220.101.5": None}           # privados ausentes (não consultados), 422 = None
    assert [c["params"]["ipAddress"] for c in s.calls] == ["185.220.101.5"]


def test_abuseipdb_sem_chave_e_auth(monkeypatch):
    _env(monkeypatch, lambda url, p: _Resp(200, _abuse(1, 1)), api_key=None)
    with pytest.raises(remote.ProviderUnauthorized):
        _run(abuseipdb.AbuseIPDBEnricher({}), ["185.220.101.5"])


# ── OTX ────────────────────────────────────────────────────────────────────

def _otx(count, pulses=None, validation=None):
    return {"pulse_info": {"count": count, "pulses": pulses or []}, "reputation": 0,
            "validation": validation or [], "type_title": "IPv4"}


def _pulse(name, **kw):
    p = {"name": name, "tags": ["scanner", "ssh"], "adversary": "", "malware_families": [], "industries": [],
         "created": "2026-09-01T00:00:00", "modified": "2026-09-05T00:00:00"}
    p.update(kw); return p


def test_otx_hit_agrega_pulses_tags_e_adversarios(monkeypatch):
    pulses = [_pulse("Brute force SSH"), _pulse("Botnet C2", tags=["c2", "ssh"], adversary="APT-X",
                                              malware_families=[{"display_name": "Mirai"}], created="2026-08-01T00:00:00")]
    s = _env(monkeypatch, lambda url, p: _Resp(200, _otx(2, pulses, validation=[{"source": "whitelist", "message": "x"}])))
    row = _run(otx.OTXEnricher({"key_kind": "ip"}), ["185.220.101.5"])["185.220.101.5"]
    assert row["pulse_count"] == 2 and row["pulses"] == ["Brute force SSH", "Botnet C2"]
    assert row["tags"] == ["scanner", "ssh", "c2"] and row["adversaries"] == ["APT-X"] and row["malware_families"] == ["Mirai"]
    assert row["first_seen"] == "2026-08-01T00:00:00" and row["last_seen"] == "2026-09-05T00:00:00"
    assert row["whitelisted"] is True and row["validation"] == ["whitelist"]
    assert s.headers["X-OTX-API-KEY"] == "chave-de-teste"
    assert s.calls[0]["url"].endswith("/IPv4/185.220.101.5/general")


def test_otx_sem_pulse_e_miss_mesmo_com_validation(monkeypatch):
    _env(monkeypatch, lambda url, p: _Resp(200, _otx(0, validation=[{"source": "whitelist"}])))
    assert _run(otx.OTXEnricher({}), ["185.220.101.5"]) == {"185.220.101.5": None}


@pytest.mark.parametrize("kind,key,path", [
    ("ip", "2606:4700:4700::1111", "/IPv6/2606%3A4700%3A4700%3A%3A1111/general"),
    ("domain", "evil.example", "/domain/evil.example/general"),
    ("file_hash", "44d88612fea8a8f36de82e1278abb02f", "/file/44d88612fea8a8f36de82e1278abb02f/general"),
    ("url", "http://evil.example/a?b=1", "/url/http%3A%2F%2Fevil.example%2Fa%3Fb%3D1/general"),
])
def test_otx_monta_o_path_por_tipo_e_escapa_a_url(monkeypatch, kind, key, path):
    s = _env(monkeypatch, lambda url, p: _Resp(404))
    assert _run(otx.OTXEnricher({"key_kind": kind}), [key]) == {key: None}
    assert s.calls[0]["url"].endswith(path)


def test_otx_key_kind_invalido():
    with pytest.raises(ValueError):
        otx.OTXConfig(key_kind="cve")


# ── GreyNoise ──────────────────────────────────────────────────────────────

def test_greynoise_community_noise_riot_e_404(monkeypatch):
    bodies = {
        "1.1.1.1": _Resp(200, {"ip": "1.1.1.1", "noise": False, "riot": True, "classification": "benign", "name": "Cloudflare Public DNS", "link": "https://viz.greynoise.io/riot/1.1.1.1", "last_seen": "2026-09-05"}),
        "185.220.101.5": _Resp(200, {"ip": "185.220.101.5", "noise": True, "riot": False, "classification": "malicious", "name": "unknown", "last_seen": "2026-09-04"}),
        "45.33.32.156": _Resp(404, {"message": "IP not observed scanning the internet or contained in RIOT data set."}),
    }
    s = _env(monkeypatch, lambda url, p: bodies[url.rsplit("/", 1)[-1]], api_key=None)
    out = _run(greynoise.GreyNoiseEnricher({}), ["1.1.1.1", "185.220.101.5", "45.33.32.156"])
    assert out["1.1.1.1"]["riot"] is True and out["1.1.1.1"]["name"] == "Cloudflare Public DNS" and out["1.1.1.1"]["tier"] == "community"
    assert out["185.220.101.5"]["noise"] is True and out["185.220.101.5"]["classification"] == "malicious"
    assert out["45.33.32.156"] is None
    assert "key" not in s.headers  # sem chave: community anônimo
    assert all("/v3/community/" in c["url"] for c in s.calls)


def test_greynoise_enterprise_context_e_seen_false(monkeypatch):
    bodies = {
        "185.220.101.5": _Resp(200, {"ip": "185.220.101.5", "seen": True, "classification": "malicious", "actor": "unknown",
                                   "tags": ["SSH Bruteforcer"], "cve": ["CVE-2024-0001"], "vpn": False, "bot": True, "spoofable": False,
                                   "first_seen": "2026-01-01", "last_seen": "2026-09-05",
                                   "metadata": {"country_code": "CN", "asn": "AS4134", "organization": "Example Telecom"}}),
        "45.33.32.156": _Resp(200, {"ip": "45.33.32.156", "seen": False}),
    }
    s = _env(monkeypatch, lambda url, p: bodies[url.rsplit("/", 1)[-1]])
    out = _run(greynoise.GreyNoiseEnricher({"tier": "enterprise"}), ["185.220.101.5", "45.33.32.156"])
    row = out["185.220.101.5"]
    assert row["noise"] and row["bot"] and row["tags"] == ["SSH Bruteforcer"] and row["asn"] == "AS4134" and row["tier"] == "enterprise"
    assert out["45.33.32.156"] is None
    assert s.headers["key"] == "chave-de-teste" and all("/v2/noise/context/" in c["url"] for c in s.calls)


def test_greynoise_enterprise_sem_chave_e_auth(monkeypatch):
    _env(monkeypatch, lambda url, p: _Resp(200, {}), api_key=None)
    with pytest.raises(remote.ProviderUnauthorized):
        _run(greynoise.GreyNoiseEnricher({"tier": "enterprise"}), ["185.220.101.5"])


# ── esqueleto remoto: cota, 429, 401, teto ─────────────────────────────────

def test_429_com_nada_resolvido_sobe_com_retry_after_e_trava_o_lote(monkeypatch):
    s = _env(monkeypatch, lambda url, p: _Resp(429, {}, {"Retry-After": "120"}))
    e = abuseipdb.AbuseIPDBEnricher({"concurrency": 1})
    with pytest.raises(remote.RemoteQuotaExceeded) as exc:
        _run(e, ["185.220.101.5", "185.220.101.6", "185.220.101.7"])
    assert exc.value.retry_after_s == 120.0
    assert len(s.calls) == 1, "após o 1º 429 as demais chaves nem saem"
    # Retry-After vigente: o próximo lote nem tenta.
    with pytest.raises(remote.RemoteQuotaExceeded):
        _run(e, ["185.220.101.8"])
    assert len(s.calls) == 1


def test_429_com_resultado_parcial_devolve_o_que_veio(monkeypatch):
    seen = {"n": 0}

    def responder(url, p):
        seen["n"] += 1
        return _Resp(200, _abuse(90, 9)) if seen["n"] == 1 else _Resp(429, {}, {"Retry-After": "30"})
    _env(monkeypatch, responder)
    out = _run(abuseipdb.AbuseIPDBEnricher({"concurrency": 1}), ["185.220.101.5", "185.220.101.6"])
    assert list(out) == ["185.220.101.5"] and out["185.220.101.5"]["abuse_confidence_score"] == 90


def test_401_sobe_como_auth_do_lote_inteiro(monkeypatch):
    _env(monkeypatch, lambda url, p: _Resp(401, {}))
    with pytest.raises(remote.ProviderUnauthorized):
        _run(otx.OTXEnricher({}), ["185.220.101.5", "185.220.101.6"])


def test_cota_local_adia_o_excedente_sem_consultar(monkeypatch):
    s = _env(monkeypatch, lambda url, p: _Resp(200, _abuse(50, 2)))
    out = _run(abuseipdb.AbuseIPDBEnricher({"requests_per_minute": 2, "requests_per_day": 100}), ["185.220.101.1", "185.220.101.2", "185.220.101.3"])
    assert len(out) == 2 and len(s.calls) == 2   # a 3ª fica UNKNOWN (ausente), sem consulta


def test_teto_por_lote_e_erro_generico_vira_unknown_da_chave(monkeypatch):
    def responder(url, p):
        if p["ipAddress"] == "185.220.101.2":
            raise RuntimeError("timeout de rede")
        return _Resp(200, _abuse(50, 2))
    s = _env(monkeypatch, responder)
    out = _run(abuseipdb.AbuseIPDBEnricher({"max_keys_per_batch": 2}), ["185.220.101.1", "185.220.101.2", "185.220.101.3"])
    assert list(out) == ["185.220.101.1"] and len(s.calls) == 2  # 2 consultadas (teto), 1 falhou = ausente
