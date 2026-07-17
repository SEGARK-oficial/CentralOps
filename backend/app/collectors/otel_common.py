"""Base compartilhada do export OTel (vendor-neutro).

Helpers usados pelos três sinais OTel — traces (``tracing.py``), métricas
(``otel_metrics.py``) e logs (``otel_logs.py``): a leitura da flag de runtime e
a construção do ``Resource`` **idêntico** entre os sinais, para que o backend de
ops (Tempo/Mimir/Loki/Datadog/Zabbix) correlacione tudo pelo mesmo
``service.name`` / ``service.instance.id`` / ``deployment.environment``.

Mantido fino e SEM import de ``opentelemetry`` no topo (os pacotes são extras
opcionais — ver ``requirements-otel.txt``); o import vive dentro de
``build_resource`` para degradar com graça quando ausentes.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Any

logger = logging.getLogger(__name__)


def otel_flag() -> bool:
    """``True`` se ``OTEL_ENABLED`` está ligado (import tardio evita ciclo)."""
    try:
        from ..core.config import settings

        return bool(getattr(settings, "OTEL_ENABLED", False))
    except Exception:  # pragma: no cover — config sempre disponível em runtime
        return False


def otel_logs_flag() -> bool:
    """``True`` se OTel **e** o sinal de LOGS estão ligados (toggle separado —
    volume de logs pode ser alto, então é opt-in independente das métricas)."""
    try:
        from ..core.config import settings

        return bool(getattr(settings, "OTEL_ENABLED", False)) and bool(
            getattr(settings, "OTEL_LOGS_ENABLED", False)
        )
    except Exception:  # pragma: no cover
        return False


def otlp_endpoint() -> str:
    """Endpoint OTLP explícito da config (vazio ⇒ o SDK usa os envs padrão
    OTEL_EXPORTER_OTLP_ENDPOINT/_TRACES/_METRICS/_LOGS_ENDPOINT)."""
    try:
        from ..core.config import settings

        return (getattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "") or "").strip()
    except Exception:  # pragma: no cover
        return ""


def otlp_endpoint_for(signal: str) -> str:
    """Endpoint OTLP/HTTP POR SINAL (``signal`` ∈ {traces, metrics, logs}).

    Crítico p/ "um endpoint base serve os 3 sinais": o exporter OTLP/HTTP usa
    ``endpoint=`` EXATAMENTE como recebido — NÃO anexa ``/v1/<sinal>`` (só anexa
    quando o endpoint vem da env padrão do SDK, não do kwarg). Como a config
    expõe UM ``OTEL_EXPORTER_OTLP_ENDPOINT`` base compartilhado, anexamos aqui o
    path do sinal a uma BASE (sem ``/v1/``), de modo que
    ``OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`` funcione para
    traces, metrics E logs (paridade com o comportamento de env do SDK). Se o
    operador já informou um path ``/v1/<sinal>`` explícito, respeitamos como está.
    Vazio ⇒ vazio (o SDK cai nos envs padrão)."""
    base = otlp_endpoint()
    if not base:
        return ""
    # Se o operador já informou um path /v1/<sinal> explícito (de QUALQUER sinal),
    # respeitamos a URL como veio para TODOS os sinais — não reescrevemos nem
    # anexamos. Sem isso, "…/v1/traces" para o sinal "logs" virava
    # "…/v1/traces/v1/logs" (path duplicado). Ver test_explicit_v1_path_is_respected.
    stripped = base.rstrip("/")
    if any(stripped.endswith(f"/v1/{s}") for s in ("traces", "metrics", "logs")):
        return base
    return stripped + "/v1/" + signal


def sdk_env_endpoint_valid() -> bool:
    """``True`` se ALGUMA env padrão do SDK aponta para um endpoint OTLP ABSOLUTO
    (com scheme ``http://``/``https://``).

    Guarda contra a armadilha de produção: quando ``OTEL_ENABLED=true`` mas o
    endpoint é IRRESOLVÍVEL, delegar ao SDK constrói uma URL RELATIVA
    ``/v1/<sinal>`` → o exporter HTTP falha a CADA ciclo com "No scheme supplied".
    Isso acontece porque o compose/Helm SETA ``OTEL_EXPORTER_OTLP_ENDPOINT`` como
    string VAZIA (presente-porém-vazia) — a var existe, então o
    ``os.environ.get(default)`` do SDK NUNCA cai no default ``localhost:4318``.

    Retorna ``True`` só se o operador REALMENTE informou um endpoint com scheme
    em alguma das envs padrão do SDK; caso contrário ``False`` (o chamador então
    desliga o sinal com 1 warning em vez de spammar export falho)."""
    for key in (
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ):
        val = (os.environ.get(key) or "").strip()
        if val.startswith("http://") or val.startswith("https://"):
            return True
    return False


def service_role() -> str:
    """Papel do processo (``worker`` / ``beat`` / ``dispatcher``) lido da env
    ``SERVICE_ROLE`` — a MESMA que o ``celery_app`` usa para decidir signals.
    Usado como label de séries por-papel (ex.: ``collector_up``), permitindo SLO
    de disponibilidade segmentado por papel. ``unknown`` quando ausente."""
    return os.environ.get("SERVICE_ROLE", "").strip() or "unknown"


def build_resource() -> Any:
    """``Resource`` OTel com os atributos semânticos padrão (mesma identidade
    nos 3 sinais). Requer os pacotes ``opentelemetry-sdk`` — chamado só de dentro
    dos ``init_*`` já protegidos por try/except."""
    from opentelemetry.sdk.resources import Resource

    from ..core.config import settings

    service = str(getattr(settings, "OTEL_SERVICE_NAME", "centralops-collector"))
    version = str(getattr(settings, "APP_VERSION", "") or "unknown")
    env = str(getattr(settings, "APP_ENV", "") or "unknown")
    try:
        host = socket.gethostname()
    except Exception:  # pragma: no cover — hostname sempre resolve
        host = "unknown"
    # service.instance.id distingue cada filho prefork (host:pid) — sem isso o
    # backend não separa as séries por processo (gauges multiproc, p.ex.).
    instance = f"{host}:{os.getpid()}"
    return Resource.create(
        {
            "service.name": service,
            "service.version": version,
            "service.instance.id": instance,
            "deployment.environment": env,
        }
    )
