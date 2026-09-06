"""Esqueleto comum dos enrichers REMOTOS de reputação (W4.4).

VirusTotal, AbuseIPDB, OTX e GreyNoise têm o mesmo formato: uma API HTTP por
indicador, chave no header, cota por chave, 429 com ``Retry-After``, 404 (ou
"zero relatos") = indicador desconhecido. O que muda é o parse da resposta.
Este módulo carrega o que é igual — para que um bug de cota ou de privacidade
seja corrigido UMA vez, não quatro:

* **cota ANTES da requisição** (token-bucket por processo, identidade = digest
  da chave + nome do provedor): chave além da cota fica AUSENTE do mapa, que o
  runtime trata como UNKNOWN e repergunta no próximo ciclo — nunca como MISS;
* **429 trava o bucket** pelo ``Retry-After`` e as demais chaves do lote nem
  saem; se NADA foi resolvido, a exceção sobe (breaker + log de atividade); com
  resultado parcial o lote segue;
* **401/403 é ``ProviderUnauthorized``** (classificado ``auth`` na métrica, não
  ``unknown`` — o operador precisa saber que é a credencial);
* **IP privado/loopback/link-local NÃO sai** para o provedor: é cota queimada e
  dado interno vazando para um terceiro em troca de um 422 ou de lixo. Fica
  ausente do mapa, sem contar como consulta;
* **teto por lote** com log do que ficou de fora — truncar em silêncio faria a
  cobertura parecer total.

O VirusTotal antecede este módulo e mantém o próprio código (é o modelo de onde
isto saiu); os três novos o usam por inteiro.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Sequence

import aiohttp
from pydantic import BaseModel, Field

from .contract import EnrichContext
from .ratelimit import buckets_for

logger = logging.getLogger(__name__)


class RemoteQuotaExceeded(RuntimeError):
    """429 do provedor (ou Retry-After ainda vigente). ``rate_limit`` na métrica;
    ``retry_after_s`` vira cooldown mínimo do breaker."""

    def __init__(self, message: str, *, retry_after_s: float = 60.0) -> None:
        super().__init__(message)
        self.retry_after_s = float(retry_after_s)


class ProviderUnauthorized(PermissionError):
    """401/403: a credencial. O nome carrega ``Unauthorized`` de propósito —
    ``_error_reason`` classifica pelo nome da classe e ``PermissionError`` puro
    cairia em ``unknown``."""


class RemoteBatchConfig(BaseModel):
    """Campos que TODO enricher remoto de reputação tem. Subclasses acrescentam
    os seus e sobrescrevem os defaults de cota com os limites públicos do
    provedor."""

    #: Teto de chaves por LOTE — o controle de custo mais importante.
    max_keys_per_batch: int = Field(25, ge=1, le=500)
    concurrency: int = Field(4, ge=1, le=32)
    timeout_s: float = Field(10.0, gt=0, le=60)
    #: Cota local, respeitada ANTES da requisição. N workers com a mesma chave:
    #: divida por N (o bucket é por processo); entre processos quem segura é o
    #: breaker por fonte, no Redis.
    requests_per_minute: int = Field(60, ge=1, le=100_000)
    requests_per_day: Optional[int] = Field(None, ge=1, le=10_000_000)


def is_public_ip(value: str) -> bool:
    """Só IP público sai para um provedor. Inválido também não sai."""
    try:
        ip = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return False
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


async def resolve_api_key(ctx: EnrichContext) -> Optional[str]:
    """Credencial do cofre, 1×/lote. A referência vem SÓ do servidor
    (``ctx.secret_ref``, da ``EnrichmentSource`` escopada à org) — nunca da
    config, que um admin de org escreve."""
    ref = ctx.secret_ref
    if not ref:
        return None
    from ...core import secrets as secrets_mod

    backend = secrets_mod.get_default_backend()
    return await asyncio.to_thread(backend.decrypt, ref)


Fetch = Callable[[aiohttp.ClientSession, str], Awaitable[Optional[Mapping[str, Any]]]]


async def resolve_bulk(
    *,
    name: str,
    keys: Sequence[str],
    cfg: RemoteBatchConfig,
    quota_identity: str,
    headers: Mapping[str, str],
    fetch: Fetch,
    only_public_ips: bool = False,
) -> Dict[str, Optional[Mapping[str, Any]]]:
    """Resolve ``keys`` com cota, teto por lote, concorrência limitada e a
    semântica de 429 descrita no módulo. ``fetch(session, key)`` devolve a linha
    (HIT), ``None`` (MISS) ou levanta ``RemoteQuotaExceeded`` /
    ``ProviderUnauthorized``; qualquer outra exceção vira UNKNOWN da chave."""
    out: Dict[str, Optional[Mapping[str, Any]]] = {}
    wanted = list(keys)
    if only_public_ips:
        skipped_private = [k for k in wanted if not is_public_ip(k)]
        if skipped_private:
            logger.debug(
                "%s: %d chave(s) não-públicas não consultadas (privado/loopback/inválido)",
                name, len(skipped_private),
            )
            wanted = [k for k in wanted if is_public_ip(k)]
    selected = wanted[: cfg.max_keys_per_batch]
    if len(wanted) > len(selected):
        logger.info(
            "%s: lote com %d chaves distintas, resolvendo %d (max_keys_per_batch). "
            "As demais seguem sem enriquecimento.",
            name, len(wanted), len(selected),
            extra={"event": f"enrich.{name}.capped", "dropped": len(wanted) - len(selected)},
        )
    if not selected:
        return out

    quota = buckets_for(
        hashlib.sha256(f"{name}:{quota_identity}".encode("utf-8")).hexdigest()[:16],
        requests_per_minute=cfg.requests_per_minute,
        requests_per_day=cfg.requests_per_day,
    )
    blocked = quota.minute.blocked_remaining()
    if blocked > 0:
        raise RemoteQuotaExceeded(
            f"{name}: Retry-After do provedor ainda vigente por {blocked:.0f}s",
            retry_after_s=blocked,
        )
    within_quota = [k for k in selected if quota.try_acquire()]
    deferred = len(selected) - len(within_quota)
    if deferred:
        logger.info(
            "%s: %d de %d chaves além da cota local (%d/min, %s/dia) — ficam para o "
            "próximo ciclo, sem consulta.",
            name, deferred, len(selected), cfg.requests_per_minute, cfg.requests_per_day,
            extra={"event": f"enrich.{name}.quota_deferred", "deferred": deferred},
        )
    if not within_quota:
        return out

    sem = asyncio.Semaphore(cfg.concurrency)
    timeout = aiohttp.ClientTimeout(total=cfg.timeout_s)

    async with aiohttp.ClientSession(timeout=timeout, headers=dict(headers)) as session:

        async def one(key: str) -> None:
            async with sem:
                remaining = quota.minute.blocked_remaining()
                if remaining > 0:
                    raise RemoteQuotaExceeded(
                        f"{name}: Retry-After vigente por {remaining:.0f}s", retry_after_s=remaining
                    )
                try:
                    out[key] = await fetch(session, key)
                except RemoteQuotaExceeded as exc:
                    quota.block_for(exc.retry_after_s)
                    raise
                except ProviderUnauthorized:
                    raise
                except Exception as exc:  # noqa: BLE001 — UNKNOWN desta chave
                    logger.debug("%s: falha em %s: %s", name, key, exc, exc_info=True)

        results = await asyncio.gather(*(one(k) for k in within_quota), return_exceptions=True)

    unauthorized = [r for r in results if isinstance(r, ProviderUnauthorized)]
    if unauthorized:
        # Credencial errada é erro do lote inteiro, não de uma chave: subir é o
        # que leva o 401 ao log de atividade em vez de "miss de 100%".
        raise unauthorized[0]
    quota_hits = [r for r in results if isinstance(r, RemoteQuotaExceeded)]
    if quota_hits:
        logger.warning(
            "%s: cota esgotada (429) durante o lote — %d de %d chaves resolvidas. "
            "Restrinja o `when` da regra ou suba a cota da chave.",
            name, len(out), len(within_quota), extra={"event": f"enrich.{name}.quota"},
        )
        if not out:
            raise quota_hits[0]
    return out


async def read_json(resp: aiohttp.ClientResponse, *, name: str, key: str) -> Any:
    """Status → semântica comum. 404 = ``None`` (desconhecido). Devolve o JSON."""
    from .ratelimit import parse_retry_after

    if resp.status == 404:
        return None
    if resp.status == 429:
        raise RemoteQuotaExceeded(
            f"{name}: 429 em {key}", retry_after_s=parse_retry_after(resp.headers.get("Retry-After")),
        )
    if resp.status in (401, 403):
        raise ProviderUnauthorized(f"{name} recusou a credencial (HTTP {resp.status})")
    resp.raise_for_status()
    return await resp.json(content_type=None)
