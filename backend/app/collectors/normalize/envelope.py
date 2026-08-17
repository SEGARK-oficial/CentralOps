"""Construção do envelope canônico (RF3.1).

Substitui o ``BaseCollector.enrich`` legado. O envelope produzido
respeita o formato:

    {
      "_centralops": {
        "schema_version": "...", "ocsf_version": "...",
        "vendor": "...", "integration_id": ...,
        "customer_id": <int — Organization.id INTERNO>,
        "customer_name": "<nome humano da Organization>",
        "organization_id": <int — id INTERNO do tenant (Organization.id)>,
        "severity_id": <int — espelha normalized.severity_id (label de rota)>,
        "stream": "...", "event_type": "...",
        "mapping_version_id": "...", "collected_at": "...",
        "collector_host": "...", "event_id": "..."
      },
      "normalized": { ...output do MappingEngine.apply... },
      "raw": { ...payload do vendor preservado intacto... }
    }

``customer_id`` no envelope é o ``Organization.id`` INTERNO do
CentralOps — a entrega de eventos NÃO depende mais da identidade do IRIS. O
mapeamento ``Organization.id → customer id externo`` (IRIS/TheHive/SOAR) vive em
``destination_customer_mappings`` e é resolvido APENAS na borda do connector
daquele destino. (Hoje ``customer_id == organization_id``; ``customer_id`` é
mantido para consumidores downstream que ainda o leem.)

``raw`` nunca é mutado — sempre uma cópia rasa do dict original. O
bloco ``normalized`` é o output direto do engine; quem chama é
responsável por validar campos OCSF críticos (class_uid etc.) antes
de enviar o envelope adiante.
"""

from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from . import ENVELOPE_SCHEMA_VERSION, OCSF_VERSION


# Resolvido uma vez por processo — usado em ``collector_host`` no
# envelope para rastrear qual réplica produziu o evento.
_COLLECTOR_HOST = socket.gethostname() or "unknown"


@dataclass(frozen=True)
class EnvelopeContext:
    """Tudo que o envelope precisa do contexto da coleta.

    Separado de :class:`CollectorContext` para que ``build_envelope``
    seja testável sem mockar aiohttp/redis. O pipeline preenche um
    :class:`EnvelopeContext` a partir de ``CollectorContext`` +
    ``MappingDefinition`` em runtime.

    **Semântica de ``customer_id``:** é o ``Organization.id`` INTERNO
    do CentralOps — **não** mais o id do IRIS. A entrega não depende do IRIS; o
    customer id externo (IRIS/SOAR) vive em ``destination_customer_mappings`` e é
    resolvido só na borda do connector.

    ``None`` aqui sinaliza evento SEM Organization (erro legítimo de tenant) e
    cai em quarentena via :func:`has_customer_id` — não mais "IRIS não mapeado".
    """

    vendor: str
    integration_id: int
    # Organization.id interno (None = evento sem tenant → quarentena).
    customer_id: Optional[int]
    stream: str
    event_type: str
    mapping_version_id: Optional[str]
    customer_name: Optional[str] = None
    collector_host: str = _COLLECTOR_HOST
    # ``platform`` é a plataforma da integração de origem
    # (``Integration.platform``: "sophos", "microsoft_defender", "wazuh", ...).
    # É um dos 6 labels de roteamento de 1ª classe. Hoje os call-sites do
    # pipeline derivam ``vendor`` do MESMO ``integration.platform``, então
    # ``platform`` defaulta para ``vendor`` quando não é passado explicitamente
    # (ver ``__post_init__``) — adiciona o label sem quebrar nenhum call-site.
    platform: Optional[str] = None
    # id INTERNO do tenant (``Organization.id``) — distinto de
    # ``customer_id`` (id do IRIS). Label de roteamento por tenant e
    # chave de isolamento event-level (auditoria/lineage por org). Optional
    # porque fluxos de teste/legados podem não tê-lo.
    organization_id: Optional[int] = None
    # Slug da organização (``Organization.slug``, único e indexado). É o que
    # serve de RÓTULO estável quando um destino deriva o nome do tenant por
    # evento (ver ``row_fields_from`` em ``output/payload_shape.py``).
    #
    # Existe separado de ``customer_name`` de propósito: ``name`` é editável na
    # tela e um rename mudaria o rótulo de todo evento novo, partindo o
    # histórico do lado do consumidor sem erro nenhum. Slug não muda por
    # renomeação de fachada.
    organization_slug: Optional[str] = None
    # data_geography herdada da Integration (Sophos dataRegion /
    # campo manual). Propagada no envelope para que o engine de roteamento
    # possa aplicar enforcement de residência por destino sem lookup em DB por evento.
    # None = desconhecida (enforcement desligado — conservador).
    data_geography: Optional[str] = None

    def __post_init__(self) -> None:
        # ``platform`` defaulta para ``vendor`` (mesma fonte:
        # ``Integration.platform`` no pipeline). frozen=True → object.__setattr__.
        if self.platform is None:
            object.__setattr__(self, "platform", self.vendor)


def _utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def compute_event_id(raw: Mapping[str, Any], vendor_msg_id: Optional[str]) -> str:
    """ID estável do evento.

    Prefere o ID nativo do vendor (vindo de
    ``BaseCollector.extract_message_id``) — colocá-lo direto no
    envelope evita um SHA-256 desnecessário no caminho quente.

    Fallback: ``sha256:<hash>`` do payload canonicalmente serializado.
    O prefixo deixa claro que é um hash derivado, não um ID emitido
    pelo vendor.
    """
    if vendor_msg_id:
        return vendor_msg_id
    serialized = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def build_envelope(
    raw: Mapping[str, Any],
    normalized: Mapping[str, Any],
    ctx: EnvelopeContext,
    *,
    vendor_msg_id: Optional[str] = None,
    collected_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Monta o envelope canônico.

    ``normalized`` deve já vir aninhado sob a chave ``normalized`` se o
    engine escreveu nela — o engine convencionalmente produz
    ``{"normalized": {...}}``. Aceitamos ambas as formas para
    flexibilidade nos testes:

    - Se ``normalized`` tem chave ``"normalized"``, extraímos.
    - Caso contrário, usamos como o conteúdo direto do bloco.
    """
    source_block: Mapping[str, Any]
    if "normalized" in normalized and isinstance(normalized["normalized"], dict):
        source_block = normalized["normalized"]
    else:
        source_block = normalized
    # Cópia rasa isola o envelope no nível das chaves top-level: dois
    # tenants que compartilhem o mesmo ``ApplyResult`` (teste/dry-run)
    # recebem dicts independentes — adição/remoção de chave em um não
    # vaza pro outro (RNF4.6). Valores aninhados são compartilhados,
    # mas o pipeline nunca muta o interior de ``normalized`` após montar
    # o envelope, então aliasing profundo é inerte em produção.
    # Custo: O(n) sobre as chaves de 1º nível, vs O(total_nodes) do deepcopy.
    normalized_block: Dict[str, Any] = dict(source_block) if source_block else {}

    centralops: Dict[str, Any] = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "ocsf_version": OCSF_VERSION,
        "vendor": ctx.vendor,
        # ``platform`` (Integration.platform) como label de
        # roteamento de 1ª classe. Defaulta para ``vendor`` (mesma origem no
        # pipeline) via EnvelopeContext.__post_init__, mas é um campo distinto
        # para permitir divergência futura (ex.: vendor agregador vs platform).
        "platform": ctx.platform,
        "integration_id": ctx.integration_id,
        "customer_id": ctx.customer_id,
        "customer_name": ctx.customer_name,
        # ``organization_id`` (id interno do tenant) e
        # ``severity_id`` são labels de roteamento de 1ª classe e
        # base do isolamento event-level. ``severity_id`` espelha
        # ``normalized.severity_id`` — mesma fonte de verdade do ``pri_for_event``
        # (severity_map), aqui só exposto para o roteador sem reler ``normalized``.
        "organization_id": ctx.organization_id,
        # Rótulo estável do tenant para destinos que derivam o nome da origem
        # por evento. Ver ``EnvelopeContext.organization_slug``.
        "organization_slug": ctx.organization_slug,
        "severity_id": normalized_block.get("severity_id"),
        "stream": ctx.stream,
        "event_type": ctx.event_type,
        "mapping_version_id": ctx.mapping_version_id,
        "collected_at": collected_at or _utcnow_iso(),
        "collector_host": ctx.collector_host,
        "event_id": compute_event_id(raw, vendor_msg_id),
        # data_geography da integração de origem (Sophos dataRegion
        # ou campo manual). Usado pelo engine de roteamento para enforcement de
        # residência por destino. Omitido (None) = geografia desconhecida →
        # enforcement desativado para este evento (conservador, nunca bloqueia
        # sem informação suficiente).
        "data_geography": ctx.data_geography,
    }

    return {
        "_centralops": centralops,
        "normalized": normalized_block,
        # Cópia rasa: o docstring diz "cópia rasa do dict original". Isola
        # top-level keys (o caller não vê mudanças no raw original). Aliasing
        # profundo é aceitável — o pipeline nunca muta raw após entregar o envelope.
        "raw": dict(raw) if raw else {},
    }


def has_customer_id(envelope: Mapping[str, Any]) -> bool:
    """RF4.2 — ``customer_id`` é obrigatório.

    Eventos que falharem essa checagem vão para quarentena com
    ``error_kind="missing_customer_id"``. A função existe para que o
    pipeline não duplique a lógica de inspeção do envelope.
    """
    meta = envelope.get("_centralops") or {}
    cid = meta.get("customer_id")
    return cid is not None and cid != ""
