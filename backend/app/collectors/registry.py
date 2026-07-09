"""Registry unificado de collectors (vendor × stream).

**Por que existe.** Antes, adicionar uma nova integração exigia editar
três lugares (``pipeline._COLLECTOR_MAP``, ``auth.refreshers._REGISTRY``,
``beat_schedule.build_schedule``). Isso é frágil e convida divergência.

**Contrato.** Um único ponto centraliza o mapeamento

    (platform, stream) → CollectorRegistration

com **todos** os metadados que os 3 consumidores precisam:

- ``collector_cls``  — classe ``BaseCollector`` concreta (``pipeline`` usa).
- ``refresh_fn``     — adapter de refresh OAuth (``oauth_cache`` usa).
- ``schedule``       — ``timedelta`` de cadência padrão (``beat_schedule`` usa).
- ``queue``          — fila Celery (``collect.priority`` | ``collect.bulk``).
- ``task_name``      — nome da task que o Beat dispara.

**Como adicionar um vendor novo.** Um módulo em ``vendors/foo.py`` chama
``register()`` no final (self-registering). O ``_register_builtins()``
abaixo só importa os módulos para disparar esse side-effect. Você NÃO
mexe em pipeline/beat/refreshers ao adicionar um vendor.

Este é o registry de fonte ÚNICO: além de collectors+catálogo,
resolve o ``BaseProvider`` rico via ``PlatformRegistration.provider_factory``
(ver ``get_provider`` abaixo) — substituindo o antigo ``providers/registry.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional, Tuple, Type

from .base import BaseCollector
from .capabilities import QueryCapability

logger = logging.getLogger(__name__)

RefreshFn = Callable[[int], Awaitable[Dict[str, object]]]


@dataclass(frozen=True)
class CollectorRegistration:
    """Metadado completo para um (vendor, stream)."""

    platform: str
    stream: str
    collector_cls: Type[BaseCollector]
    refresh_fn: RefreshFn
    schedule: timedelta
    queue: str  # "collect.priority" | "collect.bulk"
    task_name: str  # nome Celery, ex: "collectors.collect_vendor_logs_priority"

    @property
    def beat_key(self) -> str:
        """Chave estável usada em ``celery_app.conf.beat_schedule``."""
        return f"{self.platform}-{self.stream}"


@dataclass(frozen=True)
class AuthField:
    """Um campo de credencial/configuração que a UI renderiza no formulário de
    Nova Integração. É METADADA (nunca carrega valor de secret)."""

    key: str
    label: str
    # "string" | "secret" | "url" | "bool" | "select"
    type: str
    required: bool = False
    help_text: Optional[str] = None
    options: Optional[Tuple[str, ...]] = None


@dataclass(frozen=True)
class PlatformRegistration:
    """Metadado de CATÁLOGO de uma plataforma (vendor), self-describing.

    É o que torna a tela de Nova Integração 100% plugin-driven: o vendor declara
    aqui display_name/category/description/icon/docs/auth_fields, e o endpoint
    ``GET /providers/platforms`` + o frontend leem isto SEM hardcode. Adicionar
    um vendor novo = registrar isto no módulo dele; ZERO mudança em providers.py
    ou no frontend.

    ``category`` agrupa o vendor na galeria (ex.: "EDR / XDR", "Identity",
    "Cloud-audit", "Network / Firewall", "Email", "RMM", "SIEM"). ``order`` define
    a posição de exibição (menor primeiro)."""

    platform: str
    display_name: str
    category: str
    description: str = ""
    icon_id: Optional[str] = None
    docs_url: Optional[str] = None
    auth_fields: Tuple[AuthField, ...] = ()
    order: int = 100
    # Probe STATELESS de teste de conexão (creds cruas, pré-save). Recebe o dict de
    # config (auth fields digitados) e devolve um ``TestResult``. ``None`` ⇒ a
    # plataforma não suporta teste pré-save (a UI esconde o botão). Tipado solto p/
    # evitar import de TestResult aqui (vive em output/base.py).
    test_fn: Optional[Callable[[Dict[str, Any]], Awaitable[Any]]] = None
    # ── Capability model ──────────────────────────────────────
    # Declara o que o plugin suporta (string-keys: "collect:<stream>",
    # "query:<dialect>", "discover:children", "licensing:list", …). É metadado
    # de CATÁLOGO (UI/descoberta); o gate em runtime usa
    # ``get_provider(integration).capabilities()`` — que pode variar por kind.
    capabilities: frozenset = frozenset()
    # ── Query capability model ────────────────────────────
    # Metadado ESTRUTURADO por trás das capability-keys ``query:<dialect>`` que o
    # vendor declara em ``capabilities`` (dialect/modes/max_window/rate_limit/…).
    # Espelha o ``DestinationRegistration`` do lado dos destinos. Lido pelo catálogo
    # (``GET /providers/query-capabilities``) e pela resolução de runtime
    # (``BaseProvider.query_capability`` → ``integration_query_capability``). Vazio ⇒
    # a plataforma não oferece query. O teste-âncora trava o alinhamento entre as
    # keys ``query:<dialect>`` daqui e os ``query_capabilities[].dialect``.
    query_capabilities: Tuple[QueryCapability, ...] = ()
    # Segredos lógicos que o vendor exige — consumidos pelo storage
    # ``integration_credentials``.
    required_secrets: Tuple[str, ...] = ()
    version: str = "1"
    # Factory do BaseProvider rico (alerts/health/query/ações). ``None`` ⇒ a
    # plataforma é só catálogo+coleta (sem provider rico). Tipado solto para NÃO
    # acoplar o registry de collectors ao pacote ``providers``; os vendors passam
    # uma factory tardia (lazy-import) p/ evitar ciclo no boot.
    provider_factory: Optional[Callable[..., Any]] = None
    # Sub-tipo p/ multi-kind (ex.: "partner", "organization"). Vazio = variante única.
    # Quando preenchido, o create deriva ``Integration.kind`` daqui.
    variant: str = ""
    # Para variantes que são "cards" de uma plataforma-base (sophos →
    # sophos/sophos_partner/sophos_organization). Vazio ⇒ a própria ``platform`` é a
    # base. Quando preenchido, o create persiste ``Integration.platform=base_platform``
    # (ex.: "sophos") — collectors/providers continuam vendo a plataforma-base, zero
    # ripple. O ``platform`` (chave da registration) existe só no catálogo/galeria.
    base_platform: str = ""
    # ── Modelo de transporte (push-ingestion) ────────────────────────────
    # "pull"  → o framework agenda ``run_collection_once`` e o collector PUXA do
    #           vendor via API (Sophos/Wazuh/Okta/…). É o default histórico.
    # "push"  → a fonte EMPURRA eventos (syslog/WEC/agente) para o endpoint
    #           ``POST /api/ingest/...``, que os bufferiza no Redis; um collector
    #           virtual (``PushBufferCollector``) drena o buffer no mesmo
    #           ``run_collection_once`` (reaproveita normalize→dedupe→dispatch). A
    #           UI renderiza token de ingestão + endpoint + snippet de edge-collector
    #           em vez de credenciais de poll.
    transport: str = "pull"


_REGISTRY: Dict[Tuple[str, str], CollectorRegistration] = {}
_PLATFORM_REGISTRY: Dict[str, PlatformRegistration] = {}


def register(reg: CollectorRegistration) -> None:
    """Registra uma (platform, stream). Idempotente; sobrescreve warns."""
    key = (reg.platform, reg.stream)
    if key in _REGISTRY:
        logger.warning(
            "registry: sobrescrevendo registro existente para %s/%s",
            reg.platform, reg.stream,
        )
    _REGISTRY[key] = reg


def get(platform: str, stream: str) -> CollectorRegistration:
    try:
        return _REGISTRY[(platform, stream)]
    except KeyError as exc:
        raise KeyError(
            f"nenhum collector registrado para platform={platform!r} stream={stream!r}. "
            f"Registrados: {sorted(_REGISTRY.keys())}"
        ) from exc


def has(platform: str, stream: str) -> bool:
    return (platform, stream) in _REGISTRY


def iter_for_platform(platform: str) -> Iterator[CollectorRegistration]:
    for reg in _REGISTRY.values():
        if reg.platform == platform:
            yield reg


def all_registrations() -> List[CollectorRegistration]:
    return list(_REGISTRY.values())


def supported_platforms() -> List[str]:
    return sorted({reg.platform for reg in _REGISTRY.values()})


def supported_streams(platform: str) -> List[str]:
    return sorted(reg.stream for reg in iter_for_platform(platform))


# ── Platform catalog (metadata da UI, self-describing) ────────────────────


def register_platform(reg: PlatformRegistration) -> None:
    """Registra a metadata de catálogo de uma plataforma. Idempotente: o PRIMEIRO
    registro vence (vendors com múltiplos streams chamam isto 1× no módulo dono)."""
    if reg.platform in _PLATFORM_REGISTRY:
        logger.debug("registry: platform %r já registrada — mantendo a primeira", reg.platform)
        return
    # O catálogo deve falar o vocabulário canônico de capability.
    # Soft (warn, não crash no boot) — o teste-âncora trava no PR.
    from .capabilities import invalid_capabilities
    _bad = invalid_capabilities(reg.capabilities)
    if _bad:
        logger.warning(
            "registry: platform %r declara capabilities fora do vocabulário canônico: %s",
            reg.platform, _bad,
        )
    # As keys ``query:<dialect>`` do catálogo e os
    # ``query_capabilities[].dialect`` precisam ser o MESMO conjunto (sem drift).
    _cat_dialects = {c.split(":", 1)[1] for c in reg.capabilities if c.startswith("query:")}
    _struct_dialects = {qc.dialect for qc in reg.query_capabilities}
    if _cat_dialects != _struct_dialects:
        logger.warning(
            "registry: platform %r: dialetos de query divergem — capabilities=%s vs "
            "query_capabilities=%s",
            reg.platform, sorted(_cat_dialects), sorted(_struct_dialects),
        )
    _PLATFORM_REGISTRY[reg.platform] = reg


def get_platform(platform: str) -> Optional[PlatformRegistration]:
    return _PLATFORM_REGISTRY.get(platform)


def all_platforms() -> List[PlatformRegistration]:
    """Catálogo ordenado (order, depois display_name) para a galeria da UI."""
    return sorted(
        _PLATFORM_REGISTRY.values(), key=lambda r: (r.order, r.display_name.lower())
    )


def clear() -> None:
    """Apenas para testes unitários."""
    _REGISTRY.clear()
    _PLATFORM_REGISTRY.clear()


# ── Resolução de provider (registry de fonte ÚNICO) ─────────────
# Substitui o antigo ``app/providers/registry.py`` (dict ``_PROVIDERS`` +
# ``get_provider``). A indireção vive aqui: a ``PlatformRegistration`` aponta o
# ``provider_factory`` (lazy) registrado pelo módulo do vendor — fim do registry
# paralelo e do carve-out de plataforma.


def get_provider(integration: Any) -> Any:
    """Instancia o BaseProvider rico da integração (alerts/health/query/ações).

    Resolve via ``PlatformRegistration.provider_factory`` registrado pelo módulo
    do vendor. Plataformas só-catálogo/coleta (sem factory) levantam ``ValueError``
    — preservando o erro do registry legado para call-sites que não esperam um
    provider rico."""
    reg = _PLATFORM_REGISTRY.get(integration.platform)
    if reg is not None and reg.provider_factory is not None:
        return reg.provider_factory(integration)
    raise ValueError(f"No provider registered for platform '{integration.platform}'")


def provider_supported_platforms() -> List[str]:
    """Plataformas que expõem um BaseProvider rico (factory registrada).

    Espelha o antigo ``providers.registry.supported_platforms()`` ({sophos, wazuh})
    — usado pela validação de criação e pelo endpoint legado de plataformas."""
    return sorted(p for p, r in _PLATFORM_REGISTRY.items() if r.provider_factory is not None)


def integration_capabilities(integration: Any) -> frozenset:
    """Capabilities de RUNTIME da integração — fonte única de gating no core.

    O router NUNCA ramifica por ``integration.platform ==`` /
    ``integration.kind in (...)``. Toda decisão "este integration é um parent
    MSSP / suporta alertas / tem licenças?" passa por ESTA capability set, que o
    provider deriva (a lógica por kind/vendor vive no provider, não no core).

    Plataformas só-catálogo/coleta (ninjaone/defender — sem provider rico) ⇒
    conjunto VAZIO: nunca são parent, nunca expõem licença/alertas ricos. Resolve
    de forma tolerante (``get_provider`` levanta ``ValueError`` sem factory)."""
    try:
        return frozenset(get_provider(integration).capabilities())
    except Exception:  # noqa: BLE001 — sem provider rico ⇒ sem capabilities de runtime
        return frozenset()


def integration_has_capability(integration: Any, capability: str) -> bool:
    """Gate de runtime VALIDADO.

    Valida ``capability`` contra o vocabulário canônico ANTES de checar a
    pertinência — um typo (``discover:childrenn``) levanta ``ValueError`` em vez
    de virar um ``in`` que nunca casa (fail-OPEN silencioso, ex.: bulk-delete sem
    guard). Prefira esta função + as constantes ``CAP_*`` aos literais crus."""
    from .capabilities import validate_capability

    validate_capability(capability)
    return capability in integration_capabilities(integration)


def integration_query_capability(integration: Any) -> Optional[QueryCapability]:
    """``QueryCapability`` aplicável a ESTA integração (``None`` ⇒ não suporta query).

    Acessor de runtime instance-aware: delega ao provider, que aplica
    regras por kind (ex.: Sophos partner/org não roda query → ``None``). Tolerante a
    plataforma só-catálogo/coleta (sem provider rico) ⇒ ``None``. É o ponto único que
    o ``QueryService`` (QF1) consulta para gatear/limitar uma query."""
    try:
        return get_provider(integration).query_capability()
    except Exception:  # noqa: BLE001 — sem provider rico ⇒ sem query capability
        return None


# ── Built-ins ─────────────────────────────────────────────────────────
# Cada vendor é responsável pelo próprio ``register(...)`` dentro do seu
# módulo. Aqui apenas importamos para disparar o side-effect.


def _register_builtins() -> None:
    # Import tardio evita ciclos: vendors/* importam BaseCollector.
    from . import vendors  # noqa: F401  (side-effect: registra todos os vendors)


_register_builtins()
