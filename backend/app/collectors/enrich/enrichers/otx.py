"""Enricher **AlienVault OTX** — pulses da comunidade para IP, domínio, hash e
URL (W4.4).

``GET /api/v1/indicators/<tipo>/<valor>/general`` devolve ``pulse_info`` (quantos
pulses citam o indicador e quais), ``reputation`` e ``validation`` — a lista de
motivos pelos quais o OTX considera o indicador benigno (allowlist, CDN, top
sites). Chave gratuita, cota generosa mas não documentada; ``Retry-After`` é
honrado quando vier.

**MISS = nenhum pulse.** Um indicador que só aparece em ``validation`` (é
benigno segundo o OTX) também sai como HIT com ``whitelisted=true`` e
``pulse_count=0``? Não: seria um HIT que toda regra "casa pulse" leria como
ameaça. Sai como MISS, e a informação de allowlist só acompanha um HIT real.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote

import aiohttp
from pydantic import Field, field_validator

from ..contract import EnrichContext, EnricherCapabilities, EnricherRegistration
from ..registry import register
from ..remote import ProviderUnauthorized, RemoteBatchConfig, is_public_ip, read_json, resolve_api_key, resolve_bulk

_API_BASE = "https://otx.alienvault.com/api/v1/indicators"

#: ``key_kind`` → segmento de tipo da API (IP decide IPv4/IPv6 pelo valor).
_KIND_TO_TYPE: Mapping[str, str] = {
    "ip": "IPv4",
    "domain": "domain",
    "file_hash": "file",
    "url": "url",
}


class OTXConfig(RemoteBatchConfig):
    key_kind: str = Field("ip", description="ip | domain | file_hash | url")
    #: Quantos nomes de pulse e tags carregar (o resto é contado, não listado).
    max_pulses: int = Field(5, ge=1, le=50)
    requests_per_minute: int = Field(60, ge=1, le=100_000)

    @field_validator("key_kind")
    @classmethod
    def _kind(cls, value: str) -> str:  # validator, não model_post_init: Cython (ver virustotal.py)
        if value not in _KIND_TO_TYPE:
            raise ValueError(f"key_kind inválido: {value!r}. Válidos: {sorted(_KIND_TO_TYPE)}")
        return value


_CAPS = EnricherCapabilities(
    key_kinds=frozenset(_KIND_TO_TYPE),
    mode="remote",
    supports_bulk=True,
    p99_budget_ms=2_000.0,
    suggested_ttl_s=21_600,
    suggested_negative_ttl_s=1_800,
    emits_pii=False,
    license="AlienVault OTX Terms",
    redistributable=False,
    egress="third_party",
)


def _type_for(kind: str, key: str) -> str:
    if kind == "ip":
        try:
            return "IPv6" if ipaddress.ip_address(key).version == 6 else "IPv4"
        except ValueError:
            return "IPv4"
    return _KIND_TO_TYPE[kind]


def flatten(body: Mapping[str, Any], *, max_pulses: int) -> Optional[Mapping[str, Any]]:
    info = body.get("pulse_info") or {}
    count = int(info.get("count") or 0)
    pulses = [p for p in (info.get("pulses") or []) if isinstance(p, Mapping)]
    if count == 0 and not pulses:
        return None
    tags: List[str] = []
    adversaries: List[str] = []
    families: List[str] = []
    industries: List[str] = []
    created: List[str] = []
    modified: List[str] = []
    for p in pulses:
        for t in p.get("tags") or []:
            if isinstance(t, str) and t not in tags:
                tags.append(t)
        adv = p.get("adversary")
        if isinstance(adv, str) and adv and adv not in adversaries:
            adversaries.append(adv)
        for f in p.get("malware_families") or []:
            fam = f.get("display_name") if isinstance(f, Mapping) else f
            if isinstance(fam, str) and fam and fam not in families:
                families.append(fam)
        for i in p.get("industries") or []:
            if isinstance(i, str) and i not in industries:
                industries.append(i)
        if isinstance(p.get("created"), str):
            created.append(p["created"])
        if isinstance(p.get("modified"), str):
            modified.append(p["modified"])
    validation = [v.get("source") for v in (body.get("validation") or []) if isinstance(v, Mapping) and v.get("source")]
    return {
        "pulse_count": max(count, len(pulses)),
        "pulses": [str(p.get("name")) for p in pulses[:max_pulses] if p.get("name")],
        "tags": tags[: max_pulses * 2],
        "adversaries": adversaries[:max_pulses],
        "malware_families": families[:max_pulses],
        "industries": industries[:max_pulses],
        "first_seen": min(created) if created else None,
        "last_seen": max(modified) if modified else None,
        "reputation": body.get("reputation"),
        "whitelisted": bool(validation),
        "validation": validation[:5],
        "source": "otx",
    }


class OTXEnricher:
    caps = _CAPS

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._cfg = OTXConfig(**dict(config or {}))

    async def resolve(self, keys: Sequence[str], ctx: EnrichContext) -> Mapping[str, Optional[Mapping[str, Any]]]:
        api_key = await resolve_api_key(ctx)
        if not api_key:
            raise ProviderUnauthorized("OTX sem API key: cadastre a credencial na fonte configurada")
        cfg = self._cfg

        async def fetch(session: aiohttp.ClientSession, key: str) -> Optional[Mapping[str, Any]]:
            url = f"{_API_BASE}/{_type_for(cfg.key_kind, key)}/{quote(key, safe='')}/general"
            async with session.get(url) as resp:
                body = await read_json(resp, name="otx", key=key)
            if not isinstance(body, Mapping):
                return None
            return flatten(body, max_pulses=cfg.max_pulses)

        return await resolve_bulk(
            name="otx", keys=keys, cfg=cfg, quota_identity=api_key,
            headers={"X-OTX-API-KEY": api_key, "Accept": "application/json"},
            fetch=fetch, only_public_ips=(cfg.key_kind == "ip"),
        )


register(
    EnricherRegistration(
        name="otx",
        factory=lambda cfg: OTXEnricher(cfg),
        caps=_CAPS,
        config_schema=OTXConfig,
        required_secrets=("api_key",),
        label="AlienVault OTX",
        category="Threat Intel",
        description=(
            "Pulses da comunidade OTX para IP, domínio, hash ou URL: quantos e quais "
            "pulses citam o indicador, adversário, famílias de malware, tags e se o OTX o "
            "considera benigno. Resolvido em lote. Indicadores do seu ambiente são "
            "enviados a um terceiro; IPs privados nunca saem."
        ),
        icon_id="alienvault",
        docs_url="https://otx.alienvault.com/api",
        tier="beta",
        order=22,
        output_fields={
            "pulse_count": "Quantos pulses citam o indicador",
            "pulses": "Nomes dos pulses mais recentes (até max_pulses)",
            "tags": "Tags dos pulses (união)",
            "adversaries": "Adversários atribuídos nos pulses",
            "malware_families": "Famílias de malware citadas",
            "industries": "Setores citados",
            "first_seen": "Criação do pulse mais antigo (ISO)",
            "last_seen": "Modificação do pulse mais recente (ISO)",
            "reputation": "Reputação OTX do indicador",
            "whitelisted": "O OTX considera o indicador benigno (validation)",
            "validation": "Fontes da allowlist (até 5)",
            "source": "Constante 'otx', proveniência",
        },
    )
)
