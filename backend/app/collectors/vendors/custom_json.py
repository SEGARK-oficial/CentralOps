"""Fonte genérica PUSH — ``custom_json`` (W3.1).

FortiGate e WEC provaram o transporte push (``push_ingest.py``): edge-collector
faz ``POST /api/ingest/<stream>``, o buffer Redis segura, ``PushBufferCollector``
drena no mesmo ciclo das fontes pull. Mas as duas têm stream FIXO em código, e
o pedido de campo é sempre o mesmo: "tenho um JSON de um produto que vocês não
conhecem — como entra?".

Esta plataforma responde isso sem código novo por fonte:

* os **streams são dinâmicos** — cada stream é uma linha de
  ``mapping_definitions`` com ``vendor="custom_json"`` e
  ``event_type="custom_json.<stream>"``. Criar o stream (``POST
  /api/mappings/custom-streams``) cria a definição + a versão 1 com um esqueleto
  OCSF válido; o operador refina o mapping na tela que já existe;
* o registry de collectors consulta um **resolver** para esta plataforma
  (``registry.register_stream_resolver``): ``has/get/iter_for_platform`` passam
  a enxergar os streams do banco, então ingest, providers, scheduler e pipeline
  funcionam sem saber que o stream nasceu ontem;
* um ``PushBufferCollector`` é fabricado por stream, com ``event_type`` fixo, e
  o resto do pipeline (normalize → dedupe → routing) é o de sempre.

O que NÃO é: um receptor syslog. O transporte continua sendo HTTP JSON/NDJSON
de um edge-collector — é ele quem fala syslog/TCP/arquivo com a fonte.

Sincronização entre processos: quem cria o stream registra na hora no seu
próprio processo (API); workers e beat pegam pelo resolver, com cache de
``_SYNC_TTL_S``. O beat precisa de uma entry por stream, então a criação
re-registra as integrações ``custom_json`` ativas (best-effort, como o hook
de ``POST /integrations``).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional, Type

from .push_ingest import PushBufferCollector, _push_refresher

logger = logging.getLogger(__name__)

PLATFORM = "custom_json"
EVENT_TYPE_PREFIX = PLATFORM + "."

#: Slug curto: vira segmento de URL (``/api/ingest/<stream>``), chave Redis e
#: ``event_type``. Minúsculo para não existir ``Fw`` e ``fw`` como streams distintos.
STREAM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
#: ``POST /api/ingest/integrations`` casaria com ``/{stream}``; reservado.
RESERVED_STREAMS = frozenset({"integrations"})

#: Quanto tempo um worker pode ficar sem enxergar um stream recém-criado. Uma
#: consulta ao banco a cada 5 s por processo é barata; a cada evento não seria.
_SYNC_TTL_S = 5.0

# Categoria OCSF 1.8 (class_uid // 1000). Só as classes que ``normalize/ocsf``
# aceita entram aqui — ``is_valid_class_uid`` é o guard.
_CATEGORY_NAMES: Dict[int, str] = {
    0: "Uncategorized",
    1: "System Activity",
    2: "Findings",
    3: "Identity & Access Management",
    4: "Network Activity",
    6: "Application Activity",
}


class InvalidStreamName(ValueError):
    pass


class InvalidClassUid(ValueError):
    pass


class StreamExists(ValueError):
    pass


# ── nomes ────────────────────────────────────────────────────────────────

def validate_stream_name(stream: Any) -> str:
    name = str(stream or "").strip()
    if not STREAM_RE.match(name):
        raise InvalidStreamName(
            "nome de stream inválido: use 1–63 caracteres entre a-z, 0-9, '_' e '-', "
            "começando por letra ou dígito"
        )
    if name in RESERVED_STREAMS:
        raise InvalidStreamName(f"nome de stream reservado: {name!r}")
    return name


def event_type_for(stream: str) -> str:
    return EVENT_TYPE_PREFIX + stream


def stream_name(event_type: str) -> Optional[str]:
    """``custom_json.<stream>`` → ``<stream>``; ``None`` se não é desta plataforma."""
    if not isinstance(event_type, str) or not event_type.startswith(EVENT_TYPE_PREFIX):
        return None
    name = event_type[len(EVENT_TYPE_PREFIX):]
    return name if STREAM_RE.match(name) else None


# ── collector por stream ─────────────────────────────────────────────────

_COLLECTOR_CLASSES: Dict[str, Type[PushBufferCollector]] = {}
_lock = threading.RLock()


def collector_class_for(stream: str) -> Type[PushBufferCollector]:
    """Uma subclasse por stream: o pipeline lê ``platform/stream/event_type``
    da CLASSE antes de instanciar (ver ``PushBufferCollector``)."""
    cls = _COLLECTOR_CLASSES.get(stream)
    if cls is None:
        cls = type(
            f"CustomJson_{stream}_Collector",
            (PushBufferCollector,),
            {"platform": PLATFORM, "stream": stream, "event_type": event_type_for(stream)},
        )
        _COLLECTOR_CLASSES[stream] = cls
    return cls


def ensure_registered(stream: str) -> Any:
    """Registra ``(custom_json, stream)`` no registry se ainda não está.
    Idempotente e barato — é chamado pelo resolver a cada sync."""
    from ..queues import Q_BULK, T_COLLECT_BULK
    from ..registry import CollectorRegistration, is_registered, register, _REGISTRY

    # ``is_registered`` e não ``has``: ``has`` chama o resolver, que chama isto.
    if is_registered(PLATFORM, stream):
        return _REGISTRY[(PLATFORM, stream)]
    reg = CollectorRegistration(
        platform=PLATFORM,
        stream=stream,
        collector_cls=collector_class_for(stream),
        refresh_fn=_push_refresher,
        # Mesma cadência de DRENO das outras fontes push.
        schedule=timedelta(seconds=20),
        queue=Q_BULK,
        task_name=T_COLLECT_BULK,
    )
    register(reg)
    return reg


# ── resolver (banco → registry) ──────────────────────────────────────────

_sync_state: Dict[str, Any] = {"expires": 0.0, "streams": []}


def streams_from_db(db: Any = None) -> List[str]:
    """Streams desta plataforma = definições de mapping com ``vendor=custom_json``."""
    from ...db import database, models

    def _query(session: Any) -> List[str]:
        rows = (
            session.query(models.MappingDefinition.event_type)
            .filter(models.MappingDefinition.vendor == PLATFORM)
            .all()
        )
        out = sorted({s for (et,) in rows if (s := stream_name(et))})
        return out

    if db is not None:
        return _query(db)
    with database.SessionLocal() as session:
        return _query(session)


def invalidate() -> None:
    _sync_state["expires"] = 0.0


def sync_streams(*, force: bool = False) -> List[str]:
    """Garante que todo stream do banco está no registry. Cacheado por
    ``_SYNC_TTL_S``. Falha de banco NÃO propaga: mantém o que já está
    registrado (um worker sem banco continua drenando o que conhece)."""
    now = time.monotonic()
    if not force and now < _sync_state["expires"]:
        return list(_sync_state["streams"])
    with _lock:
        if not force and time.monotonic() < _sync_state["expires"]:
            return list(_sync_state["streams"])
        try:
            streams = streams_from_db()
        except Exception:  # noqa: BLE001 — banco fora = mantém o registrado
            logger.warning(
                "custom_json: falha ao listar streams no banco — mantendo os registrados",
                exc_info=True, extra={"event": "custom_json.sync_failed"},
            )
            return list(_sync_state["streams"])
        for s in streams:
            ensure_registered(s)
        _sync_state["streams"] = streams
        _sync_state["expires"] = time.monotonic() + _SYNC_TTL_S
        return list(streams)


# ── criação de stream (definição + v1) ───────────────────────────────────

def skeleton_rules(stream: str, class_uid: int) -> Dict[str, Any]:
    """Versão 1 do mapping: o mínimo que ``compile_rules`` aceita e que emite um
    OCSF válido para a classe. ``time`` vem do carimbo de recepção — o operador
    troca pelo campo do produto na tela de mapping; até lá nada vai para a
    quarentena por ``required`` ausente."""
    from ..normalize.ocsf.classes import class_name_for

    category_uid = class_uid // 1000
    return {
        "preprocess": [],
        "rules": [
            {"target": "normalized.class_uid", "const": class_uid, "required": True},
            {"target": "normalized.class_name", "const": class_name_for(class_uid)},
            {"target": "normalized.category_uid", "const": category_uid, "required": True},
            {"target": "normalized.category_name", "const": _CATEGORY_NAMES.get(category_uid, "Uncategorized")},
            {"target": "normalized.activity_id", "const": 0},
            {"target": "normalized.type_uid", "const": class_uid * 100},
            {"target": "normalized.time", "source": "_ingest.received_at", "type_cast": "iso_to_epoch", "required": True},
            {"target": "normalized.severity_id", "const": 1, "required": True},
            {"target": "normalized.severity", "const": "Informational"},
            {"target": "normalized.metadata.product.vendor_name", "const": "custom_json"},
            {"target": "normalized.metadata.product.name", "const": stream},
            {"target": "normalized.metadata.version", "const": "1.8.0"},
            {"target": "normalized.message", "source": "message || msg || event"},
        ],
    }


def create_stream(
    db: Any,
    *,
    stream: str,
    ocsf_class_uid: int,
    description: Optional[str] = None,
    author_user_id: Optional[int] = None,
) -> Any:
    """Cria ``MappingDefinition`` + ``MappingVersion`` v1 e a promove. Registra o
    stream no processo corrente. NÃO faz commit — o chamador decide."""
    from ...db import models
    from ..normalize.engine import compile_rules
    from ..normalize.ocsf.classes import is_valid_class_uid

    name = validate_stream_name(stream)
    if not isinstance(ocsf_class_uid, int) or isinstance(ocsf_class_uid, bool) or not is_valid_class_uid(ocsf_class_uid):
        raise InvalidClassUid(f"class_uid OCSF não suportado: {ocsf_class_uid!r}")
    event_type = event_type_for(name)
    existing = (
        db.query(models.MappingDefinition)
        .filter(
            models.MappingDefinition.vendor == PLATFORM,
            models.MappingDefinition.event_type == event_type,
        )
        .first()
    )
    if existing is not None:
        raise StreamExists(f"stream {name!r} já existe")

    rules = skeleton_rules(name, ocsf_class_uid)
    compile_rules(rules)  # invariante: v1 nunca nasce inválida

    defn = models.MappingDefinition(
        vendor=PLATFORM,
        event_type=event_type,
        ocsf_class_uid=ocsf_class_uid,
        description=(description or "").strip() or f"Fonte genérica JSON — stream {name}",
    )
    db.add(defn)
    db.flush()
    version = models.MappingVersion(
        definition_id=defn.id,
        version_number=1,
        rules=json.dumps(rules, separators=(",", ":")),
        author_user_id=author_user_id,
        commit_message=f"Esqueleto inicial do stream custom_json.{name} (class_uid {ocsf_class_uid})",
        dsl_version=2,
    )
    db.add(version)
    db.flush()
    defn.current_version_id = version.id
    db.flush()

    ensure_registered(name)
    invalidate()
    return defn


def reschedule_active_integrations(db: Any) -> int:
    """Um stream novo precisa de uma entry de beat por integração desta
    plataforma. Best-effort, como o hook de ``POST /integrations``: sem Redis,
    loga e segue — a reconciliação periódica do beat corrige."""
    from ...db import models

    ids = [
        row.id for row in db.query(models.Integration.id)
        .filter(models.Integration.platform == PLATFORM, models.Integration.is_active.is_(True))
        .all()
    ]
    if not ids:
        return 0
    try:
        from ..scheduler import register_integration_in_beat
    except Exception:  # noqa: BLE001
        return 0
    for integration_id in ids:
        register_integration_in_beat(integration_id)
    return len(ids)


# ── registro da plataforma ───────────────────────────────────────────────

def _register() -> None:
    from ..registry import PlatformRegistration, register_platform, register_stream_resolver

    register_platform(
        PlatformRegistration(
            platform=PLATFORM,
            display_name="Fonte genérica (JSON)",
            category="Genérico / Custom",
            description=(
                "Qualquer produto que exporte JSON: um edge-collector (Vector, Fluent Bit, "
                "script) faz POST NDJSON no endpoint de ingestão. Os streams são criados "
                "por você, cada um com o seu mapping OCSF."
            ),
            icon_id="json",
            docs_url=None,
            order=90,
            transport="push",
            capabilities=frozenset({"catalog"}),
            auth_fields=(),
        )
    )
    register_stream_resolver(PLATFORM, sync_streams)


_register()
