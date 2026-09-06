"""Enricher **AbuseIPDB** — reputação de IP por relatos da comunidade (W4.4).

``GET /api/v2/check`` devolve, para um IP público, o ``abuseConfidenceScore``
(0–100), quantos relatos e de quantos usuários distintos, a última data, e o
contexto de rede (ISP, tipo de uso, país, Tor). Limites públicos: **1.000
consultas/dia** no plano gratuito (mais nos pagos); não há endpoint de lote.

**MISS vs HIT.** A API responde 200 para QUALQUER IP público, inclusive um sem
nenhum relato (score 0, ``totalReports`` 0). Devolver isso como HIT faria toda
consulta "casar" e mataria o negative caching. Aqui: abaixo de
``min_confidence_score`` (default 0 ⇒ só "nenhum relato") o IP é MISS — a
regra que quer "qualquer relato" deixa 0, a que quer "provável abuso" põe 75.

IP privado/loopback não sai (o provedor responde 422 e a cota vai embora).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

import aiohttp
from pydantic import Field

from ..contract import EnrichContext, EnricherCapabilities, EnricherRegistration
from ..registry import register
from ..remote import ProviderUnauthorized, RemoteBatchConfig, read_json, resolve_api_key, resolve_bulk

_API_BASE = "https://api.abuseipdb.com/api/v2"


class AbuseIPDBConfig(RemoteBatchConfig):
    #: Janela dos relatos considerados (1–365 dias). 90 é o default do provedor.
    max_age_days: int = Field(90, ge=1, le=365)
    #: Abaixo disto é MISS. 0 = qualquer relato é HIT.
    min_confidence_score: int = Field(0, ge=0, le=100)
    requests_per_minute: int = Field(60, ge=1, le=100_000)
    requests_per_day: Optional[int] = Field(1000, ge=1, le=10_000_000)


_CAPS = EnricherCapabilities(
    key_kinds=frozenset({"ip"}),
    mode="remote",
    supports_bulk=True,
    p99_budget_ms=2_000.0,
    suggested_ttl_s=21_600,
    suggested_negative_ttl_s=1_800,
    emits_pii=False,
    license="AbuseIPDB Terms of Use",
    redistributable=False,
    egress="third_party",
)


def flatten(data: Mapping[str, Any], *, min_score: int) -> Optional[Mapping[str, Any]]:
    score = int(data.get("abuseConfidenceScore") or 0)
    reports = int(data.get("totalReports") or 0)
    if reports == 0 and score == 0:
        return None
    if score < min_score:
        return None
    return {
        "abuse_confidence_score": score,
        "total_reports": reports,
        "num_distinct_users": int(data.get("numDistinctUsers") or 0),
        "last_reported_at": data.get("lastReportedAt"),
        "country_code": data.get("countryCode"),
        "usage_type": data.get("usageType"),
        "isp": data.get("isp"),
        "domain": data.get("domain"),
        "hostnames": [h for h in (data.get("hostnames") or []) if isinstance(h, str)][:10],
        "is_tor": bool(data.get("isTor")),
        "is_whitelisted": bool(data.get("isWhitelisted")),
        "source": "abuseipdb",
    }


class AbuseIPDBEnricher:
    caps = _CAPS

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._cfg = AbuseIPDBConfig(**dict(config or {}))

    async def resolve(self, keys: Sequence[str], ctx: EnrichContext) -> Mapping[str, Optional[Mapping[str, Any]]]:
        api_key = await resolve_api_key(ctx)
        if not api_key:
            raise ProviderUnauthorized("AbuseIPDB sem API key: cadastre a credencial na fonte configurada")
        cfg = self._cfg

        async def fetch(session: aiohttp.ClientSession, key: str) -> Optional[Mapping[str, Any]]:
            params = {"ipAddress": key, "maxAgeInDays": str(cfg.max_age_days), "verbose": ""}
            async with session.get(f"{_API_BASE}/check", params=params) as resp:
                if resp.status == 422:
                    return None  # IP inválido/privado para o provedor
                body = await read_json(resp, name="abuseipdb", key=key)
            if body is None:
                return None
            return flatten((body or {}).get("data") or {}, min_score=cfg.min_confidence_score)

        return await resolve_bulk(
            name="abuseipdb", keys=keys, cfg=cfg, quota_identity=api_key,
            headers={"Key": api_key, "Accept": "application/json"},
            fetch=fetch, only_public_ips=True,
        )


register(
    EnricherRegistration(
        name="abuseipdb",
        factory=lambda cfg: AbuseIPDBEnricher(cfg),
        caps=_CAPS,
        config_schema=AbuseIPDBConfig,
        required_secrets=("api_key",),
        label="AbuseIPDB",
        category="Threat Intel",
        description=(
            "Reputação de IP por relatos da comunidade (score 0–100, relatos, ISP, Tor). "
            "Resolvido em lote no fim do ciclo. Plano gratuito: 1.000 consultas/dia — use "
            "um `when` restritivo. IPs do seu ambiente são enviados a um terceiro; IPs "
            "privados nunca saem."
        ),
        icon_id="abuseipdb",
        docs_url="https://docs.abuseipdb.com/",
        tier="beta",
        order=21,
        output_fields={
            "abuse_confidence_score": "Confiança de abuso 0–100 (AbuseIPDB)",
            "total_reports": "Relatos na janela max_age_days",
            "num_distinct_users": "Usuários distintos que relataram",
            "last_reported_at": "Data do último relato (ISO)",
            "country_code": "País do IP",
            "usage_type": "Tipo de uso (Data Center, ISP, …)",
            "isp": "Provedor",
            "domain": "Domínio do provedor",
            "hostnames": "Hostnames associados (até 10)",
            "is_tor": "É nó de saída Tor",
            "is_whitelisted": "Está na allowlist do provedor",
            "source": "Constante 'abuseipdb', proveniência",
        },
    )
)
