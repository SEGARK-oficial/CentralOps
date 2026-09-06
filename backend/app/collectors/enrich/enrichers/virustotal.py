"""Enricher **VirusTotal** — lookup remoto, resolvido em bulk (ADR-LOCAL-0002).

Ao contrário do OpenCTI, aqui não há como materializar tabela local: a base do
VirusTotal é o mundo inteiro. Então este é o caso REMOTO, e ele carrega todas as
restrições que vêm com isso.

**Os números que definem o desenho** (limites públicos da API v3):

- chave pública: **4 requisições/minuto**, 500/dia, ~15,5 mil/mês;
- chave premium: negociada, tipicamente ordens de grandeza acima;
- **não existe endpoint de lote** para IP/domínio/hash — cada chave é uma requisição.

A 4 req/min, uma chave pública resolve **240 indicadores por hora**. Um pipeline de
SOC vê isso em segundos. Portanto:

1. o enricher é ``mode="remote"`` e resolve só no flush do lote, nunca por evento;
2. as chaves do lote são deduplicadas antes (200 eventos do mesmo host = 1 chamada);
3. a política **precisa** de um ``when`` restritivo — o exemplo canônico é excluir
   RFC1918 e ativos já conhecidos. Sem isso o free tier queima em segundos, e essa
   é a razão de o campo existir na DSL;
4. ``egress="third_party"``: um IP do cliente indo para o VirusTotal é um evento de
   privacidade, não um detalhe de rede. A UI exibe isso com destaque e exige opt-in.

**Sobre a política de retentativa: não há.** Um 429 aqui significa que a cota
acabou; repetir dentro do mesmo lote só consome o que sobrou e atrasa o ciclo. O
enricher devolve o que conseguiu, marca erro, e o applier degrada por ``on_error``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Dict, Mapping, Optional, Sequence

import aiohttp
from pydantic import BaseModel, Field, field_validator

from ..contract import EnrichContext, EnricherCapabilities, EnricherRegistration
from ..ratelimit import buckets_for, parse_retry_after
from ..registry import register

logger = logging.getLogger(__name__)

_API_BASE = "https://www.virustotal.com/api/v3"

#: ``key_kind`` → segmento de path da API v3. ``url`` fica de fora de propósito: o
#: endpoint de URL exige o id em base64-urlsafe SEM padding do próprio URL, uma
#: regra de codificação que erra silencioso (devolve 404 em vez de erro de formato)
#: e merece um enricher próprio, não um ramo escondido aqui.
_KIND_TO_PATH: Mapping[str, str] = {
    "ip": "ip_addresses",
    "domain": "domains",
    "file_hash": "files",
}


class VirusTotalConfig(BaseModel):
    #: Tipo de chave que ESTA instância resolve. Uma instância por tipo mantém o
    #: contrato bulk simples (uma lista homogênea de chaves) e evita adivinhar o
    #: endpoint pelo formato da string — heurística que erra em hash vs domínio.
    key_kind: str = Field("ip", description="ip | domain | file_hash")
    #: Teto de chaves por LOTE. É o controle de custo mais importante: sem ele um
    #: lote de 200 eventos distintos consome metade da cota diária de um free tier.
    max_keys_per_batch: int = Field(25, ge=1, le=500)
    #: Requisições simultâneas. Baixo por default — a API pública tolera 4/min, e
    #: paralelismo alto só antecipa o 429.
    concurrency: int = Field(4, ge=1, le=32)
    timeout_s: float = Field(10.0, gt=0, le=60)
    #: Cota da chave, RESPEITADA antes da requisição (token-bucket local, ver
    #: ``enrich/ratelimit.py``). Defaults = chave pública (4/min, 500/dia).
    #: Chave premium: suba os dois. N workers com a mesma chave: divida por N —
    #: o bucket é por processo; quem para a tempestade entre processos é o
    #: breaker por fonte, compartilhado no Redis.
    requests_per_minute: int = Field(4, ge=1, le=100_000)
    requests_per_day: Optional[int] = Field(500, ge=1, le=10_000_000)

    @field_validator("key_kind")
    @classmethod
    def _validate_key_kind(cls, value: str) -> str:
        """Valida ``key_kind`` por VALIDATOR, não por ``model_post_init``.

        Não é estilo: sob Cython (a imagem de produção compila
        ``app/collectors`` inteiro em ``.so``) um ``model_post_init`` definido no
        corpo da classe vira ``cyfunction``, e o ``inspect_namespace`` do pydantic
        não o reconhece como método — trata como **campo sem anotação** e levanta
        ``PydanticUserError`` na criação da classe. O import do módulo falha, o
        ``try/except`` do pacote de enrichers o engole, e o VirusTotal
        simplesmente NÃO EXISTE na imagem, sem erro visível.

        Passou em toda a suíte local (Python puro) e só apareceu no gate compilado
        do build. O decorador sobrevive porque o pydantic detecta o
        ``PydanticDescriptorProxy`` pelo tipo, não pela natureza da função.
        """
        if value not in _KIND_TO_PATH:
            raise ValueError(
                f"key_kind inválido: {value!r}. Válidos: {sorted(_KIND_TO_PATH)}"
            )
        return value


class VirusTotalQuotaExceeded(RuntimeError):
    """429 da API. Classificado como ``rate_limit`` na métrica de erro.

    ``retry_after_s`` carrega o ``Retry-After`` do provedor (ou 60 s): o bucket
    local o respeita, e o breaker por fonte o usa como cooldown mínimo.
    """

    def __init__(self, message: str, *, retry_after_s: float = 60.0) -> None:
        super().__init__(message)
        self.retry_after_s = float(retry_after_s)


_CAPS = EnricherCapabilities(
    key_kinds=frozenset({"ip", "domain", "file_hash"}),
    mode="remote",
    # Não há endpoint de lote — o "bulk" é concorrência limitada sobre chaves
    # distintas. Declarar False seria mentir para o runtime, que usa isso para
    # decidir se vale deduplicar.
    supports_bulk=True,
    p99_budget_ms=2_000.0,
    # TTL longo de propósito: o veredito do VT sobre um IP muda em dias, não em
    # minutos, e a cota é o recurso escasso.
    suggested_ttl_s=21_600,
    # Negative caching curto: um indicador desconhecido HOJE pode ser catalogado
    # amanhã, e cachear "não sei" por 6h desperdiçaria a janela de detecção.
    suggested_negative_ttl_s=900,
    emits_pii=False,
    license="VirusTotal Terms of Service",
    redistributable=False,
    egress="third_party",
)


class VirusTotalEnricher:
    caps = _CAPS

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._cfg = VirusTotalConfig(**dict(config or {}))

    async def resolve(
        self, keys: Sequence[str], ctx: EnrichContext
    ) -> Mapping[str, Optional[Mapping[str, Any]]]:
        api_key = await _resolve_api_key(ctx)
        if not api_key:
            raise PermissionError(
                "VirusTotal sem API key: cadastre a credencial na fonte configurada "
                "para o cofre de segredos"
            )

        selected = list(keys)[: self._cfg.max_keys_per_batch]
        if len(keys) > len(selected):
            # Truncar em silêncio faria a cobertura parecer total. O log é o que
            # permite ao operador ver que precisa de `when` mais restritivo.
            logger.info(
                "VirusTotal: lote com %d chaves distintas, resolvendo %d "
                "(max_keys_per_batch). As demais seguem sem enriquecimento.",
                len(keys), len(selected),
                extra={"event": "enrich.virustotal.capped", "dropped": len(keys) - len(selected)},
            )

        path = _KIND_TO_PATH[self._cfg.key_kind]
        headers = {"x-apikey": api_key, "Accept": "application/json"}
        timeout = aiohttp.ClientTimeout(total=self._cfg.timeout_s)
        sem = asyncio.Semaphore(self._cfg.concurrency)
        out: Dict[str, Optional[Mapping[str, Any]]] = {}

        # Cota ANTES da requisição. A identidade é um digest da chave (nunca a
        # chave): duas fontes com a mesma credencial partilham o bucket, que é
        # o que o provedor faz. Chave além da cota NÃO é consultada e fica
        # ausente do mapa — UNKNOWN para o runtime, que a repergunta no próximo
        # ciclo em vez de gravar "limpo" no cache.
        quota = buckets_for(
            hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16],
            requests_per_minute=self._cfg.requests_per_minute,
            requests_per_day=self._cfg.requests_per_day,
        )
        blocked = quota.minute.blocked_remaining()
        if blocked > 0:
            raise VirusTotalQuotaExceeded(
                f"Retry-After do provedor ainda vigente por {blocked:.0f}s",
                retry_after_s=blocked,
            )
        within_quota = [k for k in selected if quota.try_acquire()]
        deferred = len(selected) - len(within_quota)
        if deferred:
            logger.info(
                "VirusTotal: %d de %d chaves além da cota local (%d/min, %s/dia) — "
                "ficam para o próximo ciclo, sem consulta.",
                deferred, len(selected), self._cfg.requests_per_minute,
                self._cfg.requests_per_day,
                extra={"event": "enrich.virustotal.quota_deferred", "deferred": deferred},
            )
        if not within_quota:
            return out

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:

            async def one(key: str) -> None:
                async with sem:
                    # Um 429 de uma chave anterior deste MESMO lote já travou o
                    # bucket: as seguintes nem saem — insistir só antecipa o
                    # próximo 429 e queima o que sobrou da cota.
                    remaining = quota.minute.blocked_remaining()
                    if remaining > 0:
                        raise VirusTotalQuotaExceeded(
                            f"Retry-After vigente por {remaining:.0f}s", retry_after_s=remaining
                        )
                    try:
                        out[key] = await _fetch(session, path, key)
                    except VirusTotalQuotaExceeded as exc:
                        # Não escreve a chave: ausência do mapa = "não resolvida",
                        # que o applier trata como skipped, não como miss. Contar
                        # cota estourada como "indicador limpo" seria um falso
                        # negativo de segurança. E o Retry-After trava o bucket:
                        # as demais chaves deste lote nem tentam.
                        quota.block_for(exc.retry_after_s)
                        raise
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "VirusTotal: falha em %s/%s: %s", path, key, exc, exc_info=True
                        )

            results = await asyncio.gather(
                *(one(k) for k in within_quota), return_exceptions=True
            )

        quota_hits = [r for r in results if isinstance(r, VirusTotalQuotaExceeded)]
        if quota_hits:
            logger.warning(
                "VirusTotal: cota esgotada (429) durante o lote — %d de %d chaves "
                "resolvidas. Restrinja o `when` da regra ou use uma chave premium.",
                len(out), len(within_quota),
                extra={"event": "enrich.virustotal.quota"},
            )
            if not out:
                # Nada resolvido e o provedor mandou esperar: o runtime precisa
                # saber (breaker + activity), não só o log. Com resultado
                # parcial o lote segue — o que veio, veio.
                raise quota_hits[0]
        return out


async def _fetch(
    session: aiohttp.ClientSession, path: str, key: str
) -> Optional[Mapping[str, Any]]:
    """Uma consulta. ``None`` = indicador desconhecido (404), que é MISS legítimo."""
    url = f"{_API_BASE}/{path}/{key}"
    async with session.get(url) as resp:
        if resp.status == 404:
            return None
        if resp.status == 429:
            raise VirusTotalQuotaExceeded(
                f"429 em {path}/{key}",
                retry_after_s=parse_retry_after(resp.headers.get("Retry-After")),
            )
        if resp.status in (401, 403):
            raise PermissionError(f"VirusTotal recusou a API key (HTTP {resp.status})")
        resp.raise_for_status()
        body = await resp.json()

    attrs = ((body or {}).get("data") or {}).get("attributes") or {}
    stats = attrs.get("last_analysis_stats") or {}
    malicious = int(stats.get("malicious") or 0)
    suspicious = int(stats.get("suspicious") or 0)
    harmless = int(stats.get("harmless") or 0)
    undetected = int(stats.get("undetected") or 0)
    total = malicious + suspicious + harmless + undetected

    return {
        "malicious": malicious,
        "suspicious": suspicious,
        "harmless": harmless,
        "undetected": undetected,
        "total_engines": total,
        # Fração, não contagem: "5 engines" não é comparável entre indicadores
        # (o número de engines varia), enquanto a fração é.
        "malicious_ratio": round(malicious / total, 4) if total else 0.0,
        "reputation": attrs.get("reputation"),
        "last_analysis_date": attrs.get("last_analysis_date"),
        "tags": attrs.get("tags") or [],
        "source": "virustotal",
    }


async def _resolve_api_key(ctx: EnrichContext) -> Optional[str]:
    """Resolve a credencial no cofre. 1×/lote, JAMAIS por evento.

    **A referência vem SÓ do servidor** (``ctx.secret_ref``, preenchido a partir da
    ``EnrichmentSource`` escopada à org). Antes havia um campo ``*_secret_ref`` na
    config, e a config é escrita por um admin de organização via API — como
    ``core.secrets`` decifra qualquer ciphertext sem noção de org
    (``backend.decrypt(ciphertext)``), aceitar a referência pela config deixaria
    colar o blob da Org B e USAR a credencial dela. O campo foi removido do schema
    e ``_validate_source_config`` recusa qualquer chave com "secret".
    """
    ref = ctx.secret_ref
    if not ref:
        return None
    from ....core import secrets as secrets_mod

    backend = secrets_mod.get_default_backend()
    return await asyncio.to_thread(backend.decrypt, ref)


register(
    EnricherRegistration(
        name="virustotal",
        factory=lambda cfg: VirusTotalEnricher(cfg),
        caps=_CAPS,
        config_schema=VirusTotalConfig,
        required_secrets=("api_key",),
        label="VirusTotal",
        category="Threat Intel",
        description=(
            "Reputação de IP, domínio ou hash pela API v3 do VirusTotal. Resolvido "
            "em lote no fim do ciclo, com deduplicação de chaves e teto por lote. "
            "ATENÇÃO: a chave pública permite 4 consultas por minuto, use um `when` "
            "restritivo. Indicadores do seu ambiente são enviados a um terceiro."
        ),
        icon_id="virustotal",
        docs_url="https://docs.virustotal.com/reference/overview",
        tier="beta",
        order=20,
        output_fields={
            "malicious": "Engines que classificaram como malicioso",
            "suspicious": "Engines que classificaram como suspeito",
            "harmless": "Engines que classificaram como inofensivo",
            "undetected": "Engines sem detecção",
            "total_engines": "Total de engines que responderam",
            "malicious_ratio": "malicious / total_engines (0.0-1.0)",
            "reputation": "Score de reputação da comunidade VirusTotal",
            "last_analysis_date": "Epoch da última análise",
            "tags": "Tags atribuídas pelo VirusTotal",
            "source": "Constante 'virustotal', proveniência",
        },
    )
)
