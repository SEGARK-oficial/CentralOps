"""Kind ``splunk_hec`` — destino Splunk HTTP Event Collector.

Primeiro destino externo do CentralOps: envia eventos ao Splunk via HEC
(HTTP Event Collector). Requer um token HEC configurado como credencial cifrada
(``secret_ref`` → campo ``hec_token`` no cofre de secrets).

**Ativo quando há um destino deste kind configurado** (multi-destino é GA).
Sem token (secret_ref ausente), os métodos ``send_batch`` e ``test`` falham
de forma clara — comportamento intencional para destino dormant sem credencial.

O ``SplunkHecClient`` implementa o protocolo ``Destination`` diretamente
(define ``kind``, ``format``, ``send_batch``, ``test``, ``close``) — sem
embrulho via ``LegacyTargetDestination``, pois o HEC tem resultado nativo
por item (DeliveryResult) e não se encaixa no modelo all-or-nothing do legado.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..splunk_hec_sender import SplunkHecClient
from ..payload_shape import DESCRICAO as PAYLOAD_DESCRICAO, PayloadShape
from .registry import DestinationConfig, DestinationRegistration, register

logger = logging.getLogger(__name__)

KIND = "splunk_hec"

#: Namespace fixo para derivar o GUID do canal a partir do id do destino.
#: Qualquer UUID constante serve; o que importa é NÃO mudar, senão o canal de
#: todo destino existente muda junto no próximo deploy.
_NS_CANAL = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def _canal_estavel(destination_id: str) -> str:
    """GUID de canal derivado do id do destino.

    Determinístico de propósito. O canal identifica o PRODUTOR e o servidor
    correlaciona acknowledgements por ele; um GUID novo a cada reinício do
    collector faria o servidor tratar cada boot como um cliente diferente e
    descartar os acks pendentes do anterior.

    Derivar do ``destination_id`` (que já é único e estável) evita mais um
    campo obrigatório no formulário para uma informação que o sistema já tem.
    """
    return str(uuid.uuid5(_NS_CANAL, destination_id or "sem-destination-id"))


class SplunkHecConfig(BaseModel):
    """Schema de config do destino Splunk HEC.

    Campos expostos no catálogo da UI (``GET /collectors/destination-types``).
    O token HEC **não** está aqui: fica em ``secret_ref`` (cofre de secrets).
    ``ca_bundle`` é path no filesystem do collector, não secret.
    """

    url: str = Field(description="URL base do Splunk HEC (ex: https://splunk:8088)")
    index: Optional[str] = Field(default=None, description="Índice Splunk de destino")
    sourcetype: str = Field(default="centralops", description="Sourcetype HEC")
    source: Optional[str] = Field(default=None, description="Campo source do HEC")
    host: Optional[str] = Field(default=None, description="Campo host do HEC")
    payload: PayloadShape = Field(default="envelope", description=PAYLOAD_DESCRICAO)
    channel: Optional[str] = Field(
        default=None,
        description="GUID do canal HEC (X-Splunk-Request-Channel). Deixe vazio: é "
        "derivado do id do destino, estável entre reinícios. Preencha só se o lado "
        "do coletor exigir um GUID específico. Servidores HEC que fazem indexer "
        "acknowledgement recusam o envio sem ele (400, code 10).",
    )
    verify_tls: bool = Field(default=True, description="Verificar certificado TLS")
    ca_bundle: Optional[str] = Field(
        default=None,
        description="Path do CA bundle PEM customizado (apenas com verify_tls=True)",
    )


def _factory(config: DestinationConfig, secrets: Optional[Any] = None) -> SplunkHecClient:
    """Constrói um ``SplunkHecClient`` a partir da config resolvida.

    O token HEC é decifrado via ``secrets.decrypt(config.secret_ref)`` quando
    ambos estiverem presentes. Quando ausentes (destino dormant sem credencial),
    ``token=None`` — send_batch e test falharão de forma descritiva sem levantar
    exceção aqui (fail-closed controlado).
    """
    cfg = SplunkHecConfig(**dict(config.config or {}))

    token: Optional[str] = None
    if secrets is not None and config.secret_ref:
        try:
            token = secrets.decrypt(config.secret_ref)
        except Exception as exc:
            # Não logar secret_ref nem o objeto exc (path da master key/cofre).
            logger.warning(
                "splunk_hec: falha ao decifrar credencial (%s) — token=None (dormant)",
                type(exc).__name__,
            )

    return SplunkHecClient(
        url=cfg.url,
        token=token,
        index=cfg.index,
        sourcetype=cfg.sourcetype,
        source=cfg.source,
        host=cfg.host,
        payload=cfg.payload,
        verify_tls=cfg.verify_tls,
        ca_bundle=cfg.ca_bundle,
        channel=cfg.channel or _canal_estavel(config.destination_id),
    )


register(
    DestinationRegistration(
        kind=KIND,
        factory=_factory,
        config_schema=SplunkHecConfig,
        default_queue="dispatch.splunk_hec",
        # "at_least_once": o HEC não possui dedup nativo no sender — uma
        # reentrega de lote (429/5xx) PODE duplicar eventos no índice.
        # Dedup real é responsabilidade do indexer via event_id carregado em
        # _centralops.event_id (campo exposto no payload para lookup no Splunk).
        # "idempotent" foi removido: enganoso sem ação create+_id.
        capabilities=frozenset({"tls", "batch", "test", "at_least_once"}),
        required_secrets=("hec_token",),
        label="Splunk HEC",
        # HTTP/NDJSON é paralelizável — concorrência maior por destino.
        delivery_defaults={"concurrency": 8},
        # Campos de catálogo self-describing (galeria de destinos).
        category="SIEM",
        icon_id="splunk",
        tier="stable",
        order=10,
        description="Splunk via HTTP Event Collector (HEC) — ingestão JSON/NDJSON em alta performance.",
    )
)
