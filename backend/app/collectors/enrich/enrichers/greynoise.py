"""Enricher **GreyNoise** — "esse IP está escaneando a internet inteira ou é um
serviço conhecido?" (W4.4).

É o enricher de **redução de ruído**: um IP marcado ``noise`` está batendo em
todo mundo (scanner, botnet), não em você; um IP ``riot`` é um serviço
legítimo conhecido (CDN, DNS público, atualização de SO). Os dois são motivo
para NÃO acordar o analista — e é por isso que GreyNoise costuma vir antes de
qualquer enricher de reputação na política.

Dois planos, dois endpoints:

* ``community`` (default): ``GET /v3/community/<ip>`` — noise/riot/classification/
  name/last_seen. Chave opcional; sem chave o provedor limita a ~50/dia, com
  chave gratuita ~100/dia (defaults da cota local seguem isso — suba se o seu
  plano for outro);
* ``enterprise``: ``GET /v2/noise/context/<ip>`` — o mesmo mais actor, tags,
  CVEs, VPN/bot/spoofable, ASN/país/organização.

404 (community) ou ``seen=false`` (enterprise) = o GreyNoise nunca viu o IP
= MISS, que aqui é informação ("tráfego dirigido a você, não ruído de
internet") e por isso o negative TTL é curto.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

import aiohttp
from pydantic import Field, field_validator

from ..contract import EnrichContext, EnricherCapabilities, EnricherRegistration
from ..registry import register
from ..remote import RemoteBatchConfig, read_json, resolve_api_key, resolve_bulk

_API_BASE = "https://api.greynoise.io"
_TIERS = ("community", "enterprise")


class GreyNoiseConfig(RemoteBatchConfig):
    tier: str = Field("community", description="community | enterprise")
    requests_per_minute: int = Field(10, ge=1, le=100_000)
    requests_per_day: Optional[int] = Field(100, ge=1, le=10_000_000)

    @field_validator("tier")
    @classmethod
    def _tier(cls, value: str) -> str:  # validator, não model_post_init: Cython (ver virustotal.py)
        if value not in _TIERS:
            raise ValueError(f"tier inválido: {value!r}. Válidos: {list(_TIERS)}")
        return value


_CAPS = EnricherCapabilities(
    key_kinds=frozenset({"ip"}),
    mode="remote",
    supports_bulk=True,
    p99_budget_ms=2_000.0,
    suggested_ttl_s=21_600,
    # MISS curto: "não é ruído" é a informação que muda a triagem, e um IP pode
    # começar a escanear a qualquer hora.
    suggested_negative_ttl_s=3_600,
    emits_pii=False,
    license="GreyNoise Terms of Service",
    redistributable=False,
    egress="third_party",
)


def flatten_community(body: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    noise, riot = bool(body.get("noise")), bool(body.get("riot"))
    if not noise and not riot:
        return None
    return {
        "noise": noise,
        "riot": riot,
        "classification": body.get("classification") or "unknown",
        "name": body.get("name"),
        "link": body.get("link"),
        "first_seen": None,
        "last_seen": body.get("last_seen"),
        "tags": [],
        "cve": [],
        "vpn": None, "bot": None, "spoofable": None,
        "country_code": None, "asn": None, "organization": None,
        "tier": "community",
        "source": "greynoise",
    }


def flatten_enterprise(body: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    if not body.get("seen"):
        return None
    meta = body.get("metadata") or {}
    return {
        "noise": True,
        "riot": bool(body.get("riot")),
        "classification": body.get("classification") or "unknown",
        "name": body.get("actor"),
        "link": f"https://viz.greynoise.io/ip/{body.get('ip')}" if body.get("ip") else None,
        "first_seen": body.get("first_seen"),
        "last_seen": body.get("last_seen"),
        "tags": [t for t in (body.get("tags") or []) if isinstance(t, str)][:20],
        "cve": [c for c in (body.get("cve") or []) if isinstance(c, str)][:20],
        "vpn": bool(body.get("vpn")),
        "bot": bool(body.get("bot")),
        "spoofable": bool(body.get("spoofable")),
        "country_code": meta.get("country_code"),
        "asn": meta.get("asn"),
        "organization": meta.get("organization"),
        "tier": "enterprise",
        "source": "greynoise",
    }


class GreyNoiseEnricher:
    caps = _CAPS

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._cfg = GreyNoiseConfig(**dict(config or {}))

    async def resolve(self, keys: Sequence[str], ctx: EnrichContext) -> Mapping[str, Optional[Mapping[str, Any]]]:
        api_key = await resolve_api_key(ctx)
        cfg = self._cfg
        if cfg.tier == "enterprise" and not api_key:
            from ..remote import ProviderUnauthorized

            raise ProviderUnauthorized("GreyNoise enterprise exige API key na fonte configurada")
        headers = {"Accept": "application/json"}
        if api_key:
            headers["key"] = api_key

        async def fetch(session: aiohttp.ClientSession, key: str) -> Optional[Mapping[str, Any]]:
            if cfg.tier == "enterprise":
                url = f"{_API_BASE}/v2/noise/context/{key}"
            else:
                url = f"{_API_BASE}/v3/community/{key}"
            async with session.get(url) as resp:
                body = await read_json(resp, name="greynoise", key=key)
            if not isinstance(body, Mapping):
                return None
            return flatten_enterprise(body) if cfg.tier == "enterprise" else flatten_community(body)

        return await resolve_bulk(
            name="greynoise", keys=keys, cfg=cfg,
            # Sem chave a cota é por IP de origem no provedor; identidade local
            # estável para o bucket ser compartilhado entre fontes sem chave.
            quota_identity=api_key or "anonymous",
            headers=headers, fetch=fetch, only_public_ips=True,
        )


register(
    EnricherRegistration(
        name="greynoise",
        factory=lambda cfg: GreyNoiseEnricher(cfg),
        caps=_CAPS,
        config_schema=GreyNoiseConfig,
        # Community funciona sem chave (cota menor); enterprise exige. Declarar
        # a chave como opcional deixa a fonte ser criada sem credencial.
        required_secrets=(),
        label="GreyNoise",
        category="Threat Intel",
        description=(
            "Redução de ruído: o IP está escaneando a internet inteira (noise) ou é um "
            "serviço legítimo conhecido (riot)? Community (chave opcional, ~100/dia) ou "
            "enterprise (actor, tags, CVEs, VPN/bot, ASN). Resolvido em lote; IPs "
            "privados nunca saem."
        ),
        icon_id="greynoise",
        docs_url="https://docs.greynoise.io/reference",
        tier="beta",
        order=23,
        output_fields={
            "noise": "O IP escaneia/ataca a internet em massa",
            "riot": "O IP é de um serviço legítimo conhecido (Rule It OuT)",
            "classification": "benign | malicious | unknown",
            "name": "Nome do serviço (riot) ou actor (enterprise)",
            "link": "Página do IP no GreyNoise",
            "first_seen": "Primeira observação (enterprise)",
            "last_seen": "Última observação",
            "tags": "Tags de comportamento (enterprise)",
            "cve": "CVEs exploradas (enterprise)",
            "vpn": "Sai por VPN (enterprise)",
            "bot": "Comportamento de bot (enterprise)",
            "spoofable": "IP spoofável (enterprise)",
            "country_code": "País (enterprise)",
            "asn": "ASN (enterprise)",
            "organization": "Organização do ASN (enterprise)",
            "tier": "community | enterprise (de onde veio a resposta)",
            "source": "Constante 'greynoise', proveniência",
        },
    )
)
