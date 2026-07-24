"""Contrato base para collectors assíncronos por vendor (RF03, RF04).

Cada ``(vendor, stream)`` é uma subclasse concreta de ``BaseCollector``
que expõe um ``async def collect()`` produzindo eventos crus do vendor.

Após a Sprint 2 do plano de evolução, o collector NÃO mais transforma
o evento — ele só produz raw events. A transformação para o envelope
canônico ``{_centralops, normalized, raw}`` é feita pelo pipeline
chamando ``normalize.engine.MappingEngine.apply`` com o mapping
versionado correspondente a ``event_type`` e em seguida
``normalize.envelope.build_envelope``.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, Optional

import aiohttp

if TYPE_CHECKING:
    import redis.asyncio as redis_async

    from .domain_limiter import DomainLimiter
    from .rate_limit_redis import RedisRateLimiter

logger = logging.getLogger(__name__)


@dataclass
class CollectorContext:
    """Estado in-flight de uma coleta. Vive apenas durante ``run_collection_once``."""

    integration_id: int
    organization_id: int
    platform: str
    headers: Dict[str, str]
    session: aiohttp.ClientSession
    cursor: Optional[Dict[str, Any]]
    domain_limiter: "DomainLimiter"
    rate_limiter: "RedisRateLimiter"
    redis: "redis_async.Redis"
    #: True (default, caminho de POLLING agendado): o coletor limita o trabalho por
    #: ciclo (``_MAX_PAGES_PER_CYCLE``) e o beat retoma no próximo ciclo — evita o
    #: poison-loop de soft-timeout. False (caminho de BACKFILL one-shot): o coletor
    #: DRENA a janela inteira num run (o orquestrador de backfill não tem loop de
    #: retomada; capar aqui truncaria o job silenciosamente). Ver os guards de teto
    #: nos coletores (ex.: wazuh_detections.py).
    bounded_per_cycle: bool = True
    #: Filtros de coleta configurados para ESTE (integração, stream), já validados
    #: contra os ``CollectionFilterField`` que o plugin declara. Dict VAZIO = não
    #: filtra nada (o default de toda instalação). O coletor empurra isto para a
    #: consulta do fornecedor — ver ``BaseCollector.filter_value``.
    filters: Dict[str, Any] = field(default_factory=dict)
    #: Preenchido pelo coletor quando ele encerra o run por ter batido o teto de
    #: páginas do ciclo. É o sinal de que SOBROU trabalho — o que distingue
    #: "watermark parado porque não há eventos" de "watermark parado porque há
    #: backlog". Sem isto, atraso de watermark sozinho dá falso positivo em stream
    #: de baixo volume; a invariante "os dois sinais juntos, nunca um só" é a que
    #: ``test_pipeline_health_router.py::test_determine_status_backlog_requires_both_signals``
    #: cobre, e o lado do coletor (ciclo vazio NÃO liga a flag) está em
    #: ``test_collector_cycle_cap_contract.py::test_empty_cycle_does_not_claim_backlog``.
    hit_cycle_cap: bool = False


class BaseCollector(abc.ABC):
    """Um collector por (vendor, stream). Stateless entre chamadas."""

    platform: str  # "sophos" | "microsoft_defender" | "ninjaone" …
    stream: str  # "alerts" | "detections" | "incidents" | "activities" …
    # event_type é a chave de roteamento de mapping (RF3.3) — combinada
    # com vendor resolve ``MappingDefinition``. Convenção:
    # ``"<vendor_slug>.<event_kind>"`` (ex: ``"sophos.alert"``).
    event_type: str

    def __init__(self, ctx: CollectorContext) -> None:
        self.ctx = ctx

    # ── API pública ────────────────────────────────────────────────────

    @property
    @abc.abstractmethod
    def domain(self) -> str:
        """Host usado para o semáforo por domínio (RNF08)."""

    @abc.abstractmethod
    def collect(self) -> AsyncIterator[Dict[str, Any]]:
        """Yield eventos crus do vendor.

        Implementações devem:

        1. Respeitar paginação até exaurir (RF03).
        2. Atualizar ``self.ctx.cursor`` no fim da iteração (RF02).
        3. Fazer ``await self.ctx.rate_limiter.acquire(...)`` antes de cada
           requisição e envolver a requisição em
           ``async with self.ctx.domain_limiter.slot(self.domain)`` (RNF08).

        Eventos saem **crus** — sem transformação. O pipeline aplica o
        mapping versionado depois.
        """

    @abc.abstractmethod
    def extract_message_id(self, event: Dict[str, Any]) -> str:
        """ID usado para dedupe (RNF07). Prefere id nativo do vendor."""

    # ── Filtro de coleta (declarado no registry, aplicado aqui) ─────────

    def filter_value(self, key: str) -> Any:
        """Valor configurado para o filtro ``key``, ou ``None`` se não filtra.

        ``None`` é o caminho quente: a esmagadora maioria das instalações não
        configura filtro nenhum, e o coletor deve montar a mesma consulta de
        sempre. Use assim::

            min_level = self.filter_value("min_rule_level")
            if min_level is not None:
                query["bool"]["filter"].append({"range": {"rule.level": {"gte": min_level}}})
        """
        return self.ctx.filters.get(key)

    def mark_cycle_capped(self) -> None:
        """Sinaliza que o run terminou no teto de páginas — sobrou backlog.

        Chame SEMPRE junto do ``return`` que encerra o ciclo pelo teto. É o que
        permite a Saúde do Pipeline distinguir backlog real de watermark
        legitimamente parado, e é a única evidência de que o filtro de coleta
        ainda não é agressivo o bastante para aquele volume.
        """
        self.ctx.hit_cycle_cap = True

    # ── Watermark (posição do cursor na linha do tempo do FORNECEDOR) ───

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """Instante do fornecedor até onde este cursor já consumiu.

        A semântica do cursor é OPACA ao core — cada coletor a interpreta. Este
        método é o único ponto onde ela é traduzida para algo comparável com o
        relógio, e é o que alimenta o ``watermark_lag_seconds`` da Saúde do
        Pipeline.

        Devolver ``None`` (o default) significa "este stream não tem cursor
        temporal" — legítimo para cursores de página opaca. A saúde então omite o
        indicador em vez de inventar um número.

        Coletores com cursor temporal DEVEM sobrescrever — na prática chamando
        ``cls.watermark_from_iso`` ou ``cls.watermark_from_epoch_ms``, que já
        implementam o contrato inteiro (naive-UTC, ``None`` em vez de exceção,
        WARNING quando o valor existe mas não é legível).

        Sem isto, um coletor pode ficar 15 horas atrasado reportando
        ``lag_seconds: 0`` e status ``healthy`` — que foi exatamente o incidente de
        jul/2026: ``lag_seconds`` mede ``agora − last_success_at``, e
        ``last_success_at`` é reescrito a cada ciclo que sucede, mesmo processando
        o dia anterior.
        """
        return None

    @classmethod
    def watermark_from_iso(
        cls, cursor: Optional[Dict[str, Any]], key: str
    ) -> Optional[datetime]:
        """``cursor[key]`` em ISO-8601 → instante naive em UTC.

        Aceita os três formatos que os fornecedores realmente devolvem no MESMO
        campo: ``Z`` (o lookback default da maioria dos módulos), ``+0000`` sem
        dois-pontos (o que o Wazuh grava) e ``+00:00``. Um valor sem offset é
        lido como UTC — todos estes vendors documentam UTC, e supor o fuso do
        worker faria o mesmo cursor render atrasos diferentes por réplica.

        Naive é obrigatório: o consumidor compara com ``datetime.utcnow()`` e as
        colunas de ``CollectionState`` são naive. Devolver aware quebraria a
        subtração com ``TypeError`` dentro do ciclo de coleta.
        """
        raw = (cursor or {}).get(key)
        if not isinstance(raw, str) or not raw.strip():
            # Chave ausente/vazia é o estado normal de um cold start — não é
            # anomalia e não merece log em todo ciclo.
            return None
        parsed = _parse_iso_utc_naive(raw)
        if parsed is None:
            logger.warning(
                "%s: cursor %s=%r não é ISO-8601 — atraso de watermark "
                "indisponível para este ciclo",
                _collector_label(cls), key, raw,
            )
        return parsed

    @classmethod
    def watermark_from_epoch_ms(
        cls, cursor: Optional[Dict[str, Any]], key: str
    ) -> Optional[datetime]:
        """``cursor[key]`` em epoch de MILISSEGUNDOS → instante naive em UTC.

        Existe separado de ``watermark_from_iso`` porque tratar epoch e ISO no
        mesmo helper obrigaria a adivinhar a unidade: um ``1753351800`` poderia
        ser segundos (2026) ou milissegundos (1970), e errar por 1000x
        transformaria um stream em dia num atraso de 55 anos na tela. Quem tem
        cursor em epoch declara isso na chamada.

        ``<= 0`` devolve ``None``: é cursor corrompido, e reportar "1970" viraria
        um backlog permanente de décadas que o operador aprende a ignorar.
        """
        raw = (cursor or {}).get(key)
        # ``bool`` é ``int`` em Python — sem este guard um ``True`` viraria 1970.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            if raw is not None:
                logger.warning(
                    "%s: cursor %s=%r não é epoch em ms — atraso de watermark "
                    "indisponível para este ciclo",
                    _collector_label(cls), key, raw,
                )
            return None
        if raw <= 0:
            return None
        try:
            return datetime.fromtimestamp(raw / 1000.0, tz=timezone.utc).replace(
                tzinfo=None
            )
        except (OverflowError, OSError, ValueError):
            logger.warning(
                "%s: cursor %s=%r fora da faixa representável — atraso de "
                "watermark indisponível para este ciclo",
                _collector_label(cls), key, raw,
            )
            return None


def _collector_label(cls: type) -> str:
    """``vendor/stream`` para o log; um coletor abstrato ainda não os tem."""
    return f"{getattr(cls, 'platform', '?')}/{getattr(cls, 'stream', '?')}"


def _parse_iso_utc_naive(raw: str) -> Optional[datetime]:
    """ISO-8601 → naive UTC, ou ``None``. NUNCA levanta.

    Roda no fim de todo ciclo de coleta, para todo vendor: uma exceção aqui
    trocaria um problema de observabilidade por parada de ingestão. O pipeline
    tem um ``try`` por cima, mas depender dele deixaria o indicador sumindo em
    silêncio — que é o modo de falha que este campo existe para evitar.
    """
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    elif len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        # "+0000" → "+00:00": o Wazuh grava o offset sem dois-pontos.
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def utcnow_iso() -> str:
    """Timestamp ISO-8601 UTC com sufixo Z (RFC 3339-friendly)."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
