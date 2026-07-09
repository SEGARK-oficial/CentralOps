"""Fontes PUSH — FortiGate (syslog) e Windows Event Log/WEC.

CentralOps nasceu pull-only (Celery beat → poll de API). Fontes que EMPURRAM
(syslog do FortiGate, Windows Event Forwarding/Collector) não têm API de poll.
Este módulo introduz o transporte **push** reaproveitando 100% do pipeline:

    edge-collector (Vector/OTel)  ──HTTP──▶  POST /api/ingest/<stream>
                                                   │  (autentica + bufferiza)
                                                   ▼
                                         Redis list  ingest:buf:<int>:<stream>
                                                   │
                              run_collection_once (beat, ~20s)
                                                   ▼
                        PushBufferCollector.collect()  → drena o buffer
                                                   ▼
                 normalize (mapping seedado) → dedupe → routing → dispatch

Não há 2º caminho de normalização: o collector virtual só DRENA; o resto do ciclo
(``_run_collection_once``) é idêntico ao das fontes pull. O ``refresh_fn`` é no-op
(não há OAuth); a credencial relevante é o **token de ingestão** (ver
``services.ingest_tokens``), verificado no endpoint — não no collector.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, AsyncIterator, Dict

from ..base import BaseCollector
from ..ingest_buffer import drain_events

logger = logging.getLogger(__name__)


class PushBufferCollector(BaseCollector):
    """Collector virtual: drena o buffer Redis de ingestão da integração.

    Subclasses concretas fixam ``platform``/``stream``/``event_type`` (lidos pelo
    pipeline ANTES da instanciação). Não faz I/O de rede de saída — só ``RPOP`` do
    Redis — então ``rate_limiter``/``domain_limiter`` ficam ociosos (sem host
    externo)."""

    @property
    def domain(self) -> str:
        # Sem host externo (não puxa de vendor); domínio sintético só para o
        # semáforo por-domínio não colidir com fontes pull reais.
        return f"push.{self.platform}"

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        events = await drain_events(self.ctx.redis, self.ctx.integration_id, self.stream)
        if events:
            logger.info(
                "push_ingest: drenando %d evento(s) integration_id=%s stream=%s",
                len(events), self.ctx.integration_id, self.stream,
            )
        for ev in events:
            yield ev

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        # O endpoint de ingestão carimba ``_ingest_id`` (id estável por evento).
        # Ausente ⇒ "" e o pipeline cai no ``compute_message_id(raw)`` (hash do
        # conteúdo) — dedupe honesto mesmo sem id nativo do vendor.
        meta = event.get("_ingest") or {}
        return str(meta.get("id") or "")


class FortiGateTrafficCollector(PushBufferCollector):
    platform = "fortinet_fortigate"
    stream = "traffic"
    event_type = "fortinet_fortigate.traffic"


class WindowsSecurityEventCollector(PushBufferCollector):
    platform = "windows_event_log"
    stream = "security"
    event_type = "windows_event_log.security"


# ── No-op refresher (push não usa OAuth; auth é o token de ingestão) ──────


async def _push_refresher(integration_id: int) -> Dict[str, object]:
    return {"access_token": "", "expires_in": 86400}


def _register() -> None:
    from ..queues import Q_BULK, T_COLLECT_BULK
    from ..registry import (
        CollectorRegistration,
        PlatformRegistration,
        register,
        register_platform,
    )

    # Cadência de DRENO: ~20s ⇒ latência de cauda baixa sem martelar o Redis.
    drain_schedule = timedelta(seconds=20)

    # ── FortiGate (syslog) ────────────────────────────────────────────────
    register_platform(
        PlatformRegistration(
            platform="fortinet_fortigate",
            display_name="Fortinet FortiGate",
            category="Rede / Firewall",
            description="Logs de tráfego/UTM do FortiGate via syslog, recebidos por um edge-collector "
            "(Vector/OTel) e encaminhados ao endpoint de ingestão.",
            icon_id="fortinet",
            docs_url="https://docs.fortinet.com/document/fortigate/latest/administration-guide/378085/configuring-syslog-settings",
            order=40,
            transport="push",
            capabilities=frozenset({"catalog", "collect:traffic"}),
            # Sem auth_fields de poll: a credencial é o token de ingestão
            # (emitido após o create). Sem test_fn pré-save (o teste é "chegou evento").
            auth_fields=(),
        )
    )
    register(
        CollectorRegistration(
            platform=FortiGateTrafficCollector.platform,
            stream=FortiGateTrafficCollector.stream,
            collector_cls=FortiGateTrafficCollector,
            refresh_fn=_push_refresher,
            schedule=drain_schedule,
            queue=Q_BULK,
            task_name=T_COLLECT_BULK,
        )
    )

    # ── Windows Event Log (WEC/WEF) ───────────────────────────────────────
    register_platform(
        PlatformRegistration(
            platform="windows_event_log",
            display_name="Windows Event Log (WEC)",
            category="Endpoint / OS",
            description="Eventos de segurança do Windows coletados nativamente via Windows Event "
            "Forwarding/Collector (WEC) e encaminhados por um edge-collector ao endpoint de ingestão.",
            icon_id="windows",
            docs_url="https://learn.microsoft.com/windows/win32/wec/windows-event-collector",
            order=41,
            transport="push",
            capabilities=frozenset({"catalog", "collect:security"}),
            auth_fields=(),
        )
    )
    register(
        CollectorRegistration(
            platform=WindowsSecurityEventCollector.platform,
            stream=WindowsSecurityEventCollector.stream,
            collector_cls=WindowsSecurityEventCollector,
            refresh_fn=_push_refresher,
            schedule=drain_schedule,
            queue=Q_BULK,
            task_name=T_COLLECT_BULK,
        )
    )


_register()
