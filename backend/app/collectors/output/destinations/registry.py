"""Registry self-registering de **destinos**.

Espelha ponto-a-ponto o ``collectors/registry.py`` (registry de vendors
de ENTRADA). O contrato é: **adicionar um tipo de destino toca em 1
lugar** — um módulo em ``destinations/<kind>.py`` chama ``register()`` no
import; o ``__init__`` só importa para disparar o side-effect. Nenhuma
edição em pipeline/beat/UI ao adicionar um ``kind``.

O ``_build()`` fechado de ``wazuh_target.py`` é substituído, no
caminho multi-destino, por ``get(kind).factory(cfg, secrets)``. O
catálogo de tipos (``all_kinds``/``describe``) alimenta o endpoint
``GET /collectors/destination-types`` que a UI lê — sem enum
hard-coded no front.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Type

from pydantic import BaseModel

from ..base import Destination

logger = logging.getLogger(__name__)


def compute_config_version(
    config: Mapping[str, Any],
    delivery: Optional[Mapping[str, Any]] = None,
) -> str:
    """sha1(config+delivery)[:12] — versão de recriação do singleton.

    Fonte única de verdade do ``config_version``: usada pelo seed da
    migração (``database._run_lightweight_migrations``) e pelo repositório
    de destinos. Espelha ``CollectorConfigSnapshot.config_version``.
    """
    payload = {"config": dict(config or {}), "delivery": dict(delivery or {})}
    raw = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha1(raw).hexdigest()[:12]


@dataclass(frozen=True)
class DestinationConfig:
    """Config resolvida de UM destino (linha ``destinations`` desserializada).

    É o que a factory de um ``kind`` recebe para construir o
    ``Destination`` concreto. ``config`` já foi validado pelo
    ``config_schema`` do kind; ``delivery`` carrega batch/retry/
    backpressure/queue; ``secret_ref`` aponta a credencial no cofre
    (nunca o segredo em claro).
    """

    destination_id: str
    kind: str
    config: Mapping[str, Any] = field(default_factory=dict)
    delivery: Mapping[str, Any] = field(default_factory=dict)
    secret_ref: Optional[str] = None
    config_version: str = ""
    name: str = ""
    organization_id: Optional[int] = None


# A factory recebe (config_resolvida, secrets_backend|None) → Destination.
# ``secrets`` é Optional porque kinds sem credencial (ex. jsonl local) o
# ignoram; tipado como Any para não acoplar o registry ao core/secrets.
DestinationFactory = Callable[[DestinationConfig, Optional[Any]], Destination]


@dataclass(frozen=True)
class DestinationRegistration:
    """Metadado completo de um ``kind`` de destino.

    Espelha ``CollectorRegistration`` (que carrega ``queue``/``task_name``) e,
    desde o refactor de catálogo, também os campos *self-describing* do
    ``PlatformRegistration`` do lado das FONTES (``category``/``description``/
    ``icon_id``/``docs_url``/``tier``). É o que torna a galeria de destinos
    100% plugin-driven e simétrica à de integrações: o vendor declara aqui
    label/ícone/categoria/descrição e o frontend lê via
    ``GET /collectors/destinations/destination-types`` SEM hardcode. Adicionar
    um destino = registrar isto no módulo dele; ZERO mudança no frontend.
    """

    kind: str
    factory: DestinationFactory
    config_schema: Type[BaseModel]
    default_queue: str
    #: {"tls", "batch", "persistent_queue", "test", "load_balance", "idempotent", ...}
    capabilities: frozenset = frozenset()
    #: nomes lógicos de segredos exigidos (token HEC, app Entra, ...).
    required_secrets: Tuple[str, ...] = ()
    #: rótulo legível para o catálogo da UI.
    label: str = ""
    #: defaults de ``DeliveryConfig`` por kind. Deep-merged
    #: SOB a delivery do usuário (usuário vence). Ex.: jsonl→concurrency=1,
    #: splunk_hec→concurrency=8. Vazio = usa os defaults do modelo.
    delivery_defaults: Mapping[str, Any] = field(default_factory=dict)
    # ── Catálogo self-describing (simetria com PlatformRegistration) ───────
    #: agrupa o destino na galeria ("SIEM", "Data Lake", "Object Storage",
    #: "Streaming", "Observability", "Network / Syslog", "File", "Webhook").
    category: str = "Outros"
    #: descrição curta exibida no card da galeria.
    description: str = ""
    #: id de ícone de marca (ver ``brand-icons`` no frontend): "splunk",
    #: "elastic", "clickhouse", "crowdstrike", "kafka", "s3", "sentinel", …
    #: Vazio ⇒ o frontend cai num glifo genérico derivado da categoria.
    icon_id: Optional[str] = None
    #: link para a doc de configuração do destino (aparece no formulário).
    docs_url: Optional[str] = None
    #: maturidade: "stable" | "beta" | "generic". A UI mostra um badge.
    tier: str = "stable"
    #: posição de exibição na galeria (menor primeiro), depois label.
    order: int = 100

    def describe(self) -> Dict[str, Any]:
        """Forma serializável para o endpoint de catálogo (UI lê isto)."""
        # Import tardio: delivery_config importa o registry (lazy) → sem ciclo.
        from ..delivery_config import DeliveryConfig

        return {
            "kind": self.kind,
            "label": self.label or self.kind,
            "default_queue": self.default_queue,
            "capabilities": sorted(self.capabilities),
            "required_secrets": list(self.required_secrets),
            "config_schema": self.config_schema.model_json_schema(),
            "delivery_schema": DeliveryConfig.model_json_schema(),
            "delivery_defaults": dict(self.delivery_defaults),
            # Catálogo self-describing (simetria com /providers/platforms).
            "category": self.category,
            "description": self.description,
            "icon_id": self.icon_id,
            "docs_url": self.docs_url,
            "tier": self.tier,
            "order": self.order,
        }


_REGISTRY: Dict[str, DestinationRegistration] = {}


def register(reg: DestinationRegistration) -> None:
    """Registra um ``kind``. Idempotente; sobrescreve com warn (igual ao
    registry de collectors)."""
    if reg.kind in _REGISTRY:
        logger.warning("destinations.registry: sobrescrevendo kind=%s", reg.kind)
    _REGISTRY[reg.kind] = reg


def get(kind: str) -> DestinationRegistration:
    try:
        return _REGISTRY[kind]
    except KeyError as exc:
        raise KeyError(
            f"nenhum destino registrado para kind={kind!r}. "
            f"Registrados: {sorted(_REGISTRY.keys())}"
        ) from exc


def has(kind: str) -> bool:
    return kind in _REGISTRY


def all_kinds() -> List[str]:
    return sorted(_REGISTRY.keys())


def all_registrations() -> List[DestinationRegistration]:
    return list(_REGISTRY.values())


def describe_all() -> List[Dict[str, Any]]:
    """Catálogo completo p/ a UI (``GET /collectors/destination-types``).

    Ordenado por ``(order, label)`` — espelha ``registry.all_platforms()`` do
    lado das fontes, para a galeria renderizar numa ordem curada (e não
    alfabética por ``kind``, que é técnica)."""
    regs = sorted(
        _REGISTRY.values(),
        key=lambda r: (r.order, (r.label or r.kind).lower()),
    )
    return [r.describe() for r in regs]


def build(config: DestinationConfig, secrets: Optional[Any] = None) -> Destination:
    """Constrói o ``Destination`` concreto via factory do ``kind``.

    Substitui o ``_build()`` fechado no caminho multi-destino.
    """
    return get(config.kind).factory(config, secrets)


def clear() -> None:
    """Apenas para testes unitários."""
    _REGISTRY.clear()


# ── Built-ins ──────────────────────────────────────────────────────────
# Cada kind se auto-registra no próprio módulo. Aqui só importamos para
# disparar o side-effect (idêntico a collectors/registry._register_builtins).


def _register_builtins() -> None:
    # Import tardio evita ciclo: os módulos de kind importam ``register``
    # e ``Destination`` daqui / de ``..base``.
    from . import syslog_rfc3164  # noqa: F401  (side-effect: register)
    from . import syslog_rfc5424  # noqa: F401  (side-effect: register)
    from . import jsonl  # noqa: F401            (side-effect: register)
    from . import splunk_hec  # noqa: F401       (side-effect: register)
    from . import elastic_bulk  # noqa: F401     (side-effect: register)
    from . import otlp  # noqa: F401             (side-effect: register)
    from . import s3  # noqa: F401               (side-effect: register)
    from . import sentinel  # noqa: F401         (side-effect: register)
    from . import kafka  # noqa: F401            (side-effect: register)
    from . import webhook  # noqa: F401          (side-effect: register)
    from . import datadog  # noqa: F401          (side-effect: register)
    from . import chronicle  # noqa: F401        (side-effect: register)
    from . import security_lake  # noqa: F401    (side-effect: register)
    from . import clickhouse  # noqa: F401       (side-effect: register — analítico colunar)
    from . import crowdstrike_logscale  # noqa: F401  (side-effect: register — Falcon LogScale)
    from . import crowdstrike_ngsiem  # noqa: F401    (side-effect: register — Falcon NG-SIEM)


_register_builtins()
