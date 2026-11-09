"""Fontes PUSH — FortiGate (syslog) e Windows Event Log/WEC.

Fontes que EMPURRAM (syslog do FortiGate, Windows Event Forwarding/Collector) não
têm API de poll. O transporte push reaproveita 100% do pipeline:

    edge-collector (Vector/OTel)  ──HTTP──▶  POST /api/ingest/<stream>
    receptor syslog               ──────▶        │ (autentica, carimba)
                                                 ▼
                                   tópico raw (acks=all, antes do 200)
                                                 ▼
                         processor: dedupe → normalize → enrich → route

Não há ciclo de coleta para push (ADR-0018 Onda 5): a classe registrada aqui só
diz ao ``processor`` o ``event_type`` e o id de dedupe do evento. O
``refresh_fn`` é no-op (não há OAuth); a credencial é o **token de ingestão**
(ver ``services.ingest_tokens``), verificado no endpoint.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, AsyncIterator, Dict

from ..base import BaseCollector

logger = logging.getLogger(__name__)


class PushSourceCollector(BaseCollector):
    """Coletor de uma fonte push: não busca nada.

    Subclasses concretas fixam ``platform``/``stream``/``event_type`` (lidos pelo
    ``processor`` sem instanciar). O processor só usa ``extract_message_id``."""

    @property
    def domain(self) -> str:
        return f"push.{self.platform}"

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        # Fonte push não tem o que buscar: o evento já chegou ao raw pelo ingest.
        return
        yield  # pragma: no cover — torna isto um gerador assíncrono

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        # O endpoint de ingestão carimba ``_ingest_id`` (id estável por evento).
        # Ausente ⇒ "" e o pipeline cai no ``compute_message_id(raw)`` (hash do
        # conteúdo) — dedupe honesto mesmo sem id nativo do vendor.
        meta = event.get("_ingest") or {}
        return str(meta.get("id") or "")


class FortiGateTrafficCollector(PushSourceCollector):
    platform = "fortinet_fortigate"
    stream = "traffic"
    event_type = "fortinet_fortigate.traffic"


class WindowsSecurityEventCollector(PushSourceCollector):
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

    # Push não tem ciclo agendado (``registry.is_push_platform``); a cadência
    # só alimenta o que o registro exige (TTL da trava, agenda nunca criada).
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
