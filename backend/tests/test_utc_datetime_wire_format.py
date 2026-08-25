"""Instante que sai na API tem de carregar o fuso — senão o cliente adivinha.

O banco guarda naive-UTC (convenção do projeto, ver ``core/datetime_utils``). Sem
serializador, o Pydantic emite ``"2026-08-25T06:27:57.457811"`` — sem fuso nenhum
— e a especificação do JavaScript manda o ``new Date()`` interpretar isso como
**hora local**.

Consequência medida (ago/2026, browser em UTC−3): a tela de Coletores mostrava
TODO atraso 180 min menor que o real, e qualquer coleta das últimas 3h caía no
``Math.max(0, …)`` do badge e aparecia como "Ativo" verde — timestamp "no futuro"
virando zero. A mesma instância dizia 156 min numa tela e 358 na outra, porque a
segunda calcula no servidor (naive − naive, correto).
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from datetime import datetime, timedelta, timezone

from backend.app.api.schemas import CollectionStateRead
from backend.app.core.datetime_utils import to_utc_iso
from backend.app.routers.pipeline_health import IntegrationPipelineHealth
from backend.app.schemas.health import HealthResponse

_NAIVE = datetime(2026, 8, 25, 6, 27, 57, 457811)


def test_naive_do_banco_sai_rotulado_como_utc() -> None:
    assert to_utc_iso(_NAIVE) == "2026-08-25T06:27:57.457811Z"


def test_aware_em_outro_fuso_e_convertido_para_utc() -> None:
    """Entrada aware não pode sair com o offset do produtor."""
    aware = datetime(2026, 8, 25, 3, 27, 57, 457811, tzinfo=timezone(timedelta(hours=-3)))
    assert to_utc_iso(aware) == "2026-08-25T06:27:57.457811Z"


def test_collection_state_nao_emite_mais_timestamp_sem_fuso() -> None:
    """A tela de Coletores — a superfície onde o erro de 3h foi medido."""
    dumped = CollectionStateRead(
        integration_id=7,
        stream="siem_events",
        last_success_at=_NAIVE,
        last_attempt_at=_NAIVE,
        watermark_at=_NAIVE,
        updated_at=_NAIVE,
    ).model_dump(mode="json")

    for campo in ("last_success_at", "last_attempt_at", "watermark_at", "updated_at"):
        assert dumped[campo].endswith("Z"), (
            f"{campo} saiu sem fuso ({dumped[campo]!r}) — o browser vai ler como "
            "hora local e subestimar o atraso pelo offset do operador"
        )


def test_pipeline_health_e_health_v2_tambem() -> None:
    ph = IntegrationPipelineHealth(
        integration_id=7,
        status="healthy",
        events_per_minute=None,
        lag_seconds=18,
        watermark_lag_seconds=None,
        backlog_detected=False,
        last_error=None,
        last_success_at=_NAIVE,
        stale_stream=None,
        stale_stream_lag_seconds=None,
        mapped_field_ratio=None,
        drift_count_24h=0,
        quarantine_count_24h=0,
        cached_at=_NAIVE,
    ).model_dump(mode="json")
    assert ph["last_success_at"].endswith("Z")
    assert ph["cached_at"].endswith("Z")

    hv2 = HealthResponse(
        platform="sophos", last_collection_at=_NAIVE, last_success_at=_NAIVE
    ).model_dump(mode="json")
    assert hv2["last_collection_at"].endswith("Z")
    assert hv2["last_success_at"].endswith("Z")


def test_none_continua_none() -> None:
    dumped = CollectionStateRead(integration_id=1, stream="alerts").model_dump(mode="json")
    assert dumped["last_success_at"] is None
    assert dumped["watermark_at"] is None


def test_modo_python_continua_devolvendo_datetime() -> None:
    """``when_used="json"``: só o que vai para a rede é rotulado.

    Código interno que faz aritmética com o resultado de ``model_dump()`` (sem
    ``mode="json"``) continua recebendo ``datetime``, não string.
    """
    dumped = CollectionStateRead(
        integration_id=1, stream="alerts", last_success_at=_NAIVE
    ).model_dump()
    assert isinstance(dumped["last_success_at"], datetime)


def test_o_instante_sobrevive_ao_round_trip() -> None:
    """Meta-teste: o rótulo tem de ser CORRETO, não só estar presente.

    Um serializador que grudasse "Z" num horário local passaria em todos os
    asserts de sufixo acima e continuaria mentindo por 3h.
    """
    texto = to_utc_iso(_NAIVE)
    reparsed = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    assert reparsed == _NAIVE.replace(tzinfo=timezone.utc)
