"""O card da integração não pode dizer ``healthy`` com um stream morto.

``lag_seconds`` agrega por ``max(last_success_at)`` — o stream que coletou MAIS
recentemente. É deliberado (responde "alguma coleta terminou agora?"), mas deixa
um ponto cego inteiro de fora: quando UM stream para e os irmãos seguem
coletando, os vivos reescrevem o máximo a cada ciclo e o morto some.

Medido em produção (ago/2026): integração com 4 streams, um deles parado há
**364 min**, e a API devolvendo ``status: healthy, lag_seconds: 18``. O número
real só aparecia em ``stale_minutes_max`` do resumo global — que não diz QUAL
stream.

``watermark_lag_seconds`` também não resolve: stream parado mantém o watermark
parado, e isso é indistinguível de fonte sem eventos no período. Só comparar cada
stream com a PRÓPRIA cadência separa "não veio nada" de "não rodou".
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base
from backend.app.routers.pipeline_health import (
    _determine_status,
    _worst_stale_stream,
    compute_pipeline_health,
)

# Cadências reais do registry, para o teste falhar se elas mudarem sem revisão:
#   sophos/alerts 1min · siem_events 1min · cases 3min · detections 5min
# Limiar = max(300s, cadência × 3).


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def _org_and_integration(db, platform: str = "sophos") -> int:
    org = models.Organization(name="Org de teste", slug="org-de-teste")
    db.add(org)
    db.commit()
    db.refresh(org)
    integration = models.Integration(
        organization_id=org.id,
        name="Integração de teste",
        platform=platform,
        is_active=True,
        auth_status="unknown",
    )
    db.add(integration)
    db.commit()
    db.refresh(integration)
    return integration.id


def _state(db, integration_id: int, stream: str, last_success_at: datetime) -> None:
    db.add(
        models.CollectionState(
            integration_id=integration_id,
            stream=stream,
            last_success_at=last_success_at,
            last_attempt_at=last_success_at,
            consecutive_failures=0,
            events_collected_total=1,
        )
    )
    db.commit()


# ── A forma exata do incidente ───────────────────────────────────────────


def test_um_stream_parado_entre_irmaos_vivos_derruba_o_status(db) -> None:
    """3 streams coletando agora + 1 parado há 6h ⇒ ``unhealthy``, com o nome.

    Antes da correção este mesmo cenário devolvia ``healthy``.
    """
    integration_id = _org_and_integration(db)
    agora = datetime.utcnow()
    for stream in ("alerts", "cases", "detections"):
        _state(db, integration_id, stream, agora - timedelta(seconds=20))
    _state(db, integration_id, "siem_events", agora - timedelta(minutes=364))

    health = compute_pipeline_health(db, integration_id)

    assert health.stale_stream == "siem_events"
    assert health.stale_stream_lag_seconds is not None
    assert health.stale_stream_lag_seconds > 6 * 60 * 60 - 120
    assert health.status == "unhealthy"

    # A prova de que o campo ANTIGO continua cego — e por que o novo precisa
    # existir. Se este assert quebrar, ``lag_seconds`` mudou de semântica e a
    # justificativa do campo novo precisa ser reescrita, não o teste.
    assert health.lag_seconds is not None and health.lag_seconds < 300, (
        "lag_seconds deveria seguir otimista (max entre streams) — é justamente "
        "por isso que ele não serve para detectar stream parado"
    )


def test_todos_os_streams_em_dia_seguem_healthy(db) -> None:
    """Controle POSITIVO: sem ele o teste acima passaria com um alarme preso."""
    integration_id = _org_and_integration(db)
    agora = datetime.utcnow()
    for stream in ("alerts", "cases", "detections", "siem_events"):
        _state(db, integration_id, stream, agora - timedelta(seconds=20))

    health = compute_pipeline_health(db, integration_id)

    assert health.stale_stream is None
    assert health.stale_stream_lag_seconds is None
    assert health.status == "healthy"


# ── O limiar é por cadência, não único ───────────────────────────────────


def test_stream_de_cadencia_longa_nao_alarma_dentro_da_propria_janela(db) -> None:
    """``detections`` roda a cada 5 min: 12 min de atraso é 2,4 ciclos, não falha.

    Um limiar único de 300s pintaria de vermelho metade da frota e o indicador
    queimaria na primeira semana — que é o modo de falha que este projeto já
    documentou em outros alarmes.
    """
    integration_id = _org_and_integration(db)
    agora = datetime.utcnow()
    _state(db, integration_id, "alerts", agora - timedelta(seconds=20))
    _state(db, integration_id, "detections", agora - timedelta(minutes=12))

    stream, _lag = _worst_stale_stream(db, integration_id, "sophos", agora)
    assert stream is None


def test_stream_de_cadencia_curta_alarma_no_mesmo_atraso(db) -> None:
    """Par do teste acima: 12 min num stream de 1 min são 12 ciclos perdidos.

    O MESMO número de minutos, veredito oposto — é isso que só a comparação com
    a cadência própria consegue expressar.
    """
    integration_id = _org_and_integration(db)
    agora = datetime.utcnow()
    _state(db, integration_id, "detections", agora - timedelta(seconds=20))
    _state(db, integration_id, "alerts", agora - timedelta(minutes=12))

    stream, lag = _worst_stale_stream(db, integration_id, "sophos", agora)
    assert stream == "alerts"
    assert lag is not None and lag >= 12 * 60 - 5


def test_ordena_pelo_mais_anomalo_e_nao_pelo_maior_numero(db) -> None:
    """Ranking por RAZÃO (atraso ÷ limiar), não por segundos absolutos.

    ``alerts`` parado há 30 min (limiar 300s ⇒ 6×) é um coletor quebrado;
    ``detections`` parado há 35 min (limiar 900s ⇒ 2,3×) está atrás no relógio
    mas menos anômalo. Ordenar por segundos apontaria o dedo para o errado.
    """
    integration_id = _org_and_integration(db)
    agora = datetime.utcnow()
    _state(db, integration_id, "alerts", agora - timedelta(minutes=30))
    _state(db, integration_id, "detections", agora - timedelta(minutes=35))

    stream, _lag = _worst_stale_stream(db, integration_id, "sophos", agora)
    assert stream == "alerts"


def test_stream_fora_do_registry_e_pulado_em_vez_de_alarmar(db) -> None:
    """Sem cadência declarada não há limiar — inventar um alarmaria por ruído."""
    integration_id = _org_and_integration(db)
    agora = datetime.utcnow()
    _state(db, integration_id, "stream-que-nao-existe", agora - timedelta(days=3))

    stream, lag = _worst_stale_stream(db, integration_id, "sophos", agora)
    assert (stream, lag) == (None, None)


# ── A regra de status ────────────────────────────────────────────────────


def test_stale_stream_escala_para_unhealthy_e_nao_para_degraded() -> None:
    """A régua de ``unhealthy`` é "parou de coletar" — e foi o que aconteceu.

    Rebaixar para ``degraded`` porque os irmãos ainda coletam seria a mesma
    anistia que ``max(last_success_at)`` já dava, com outro nome.
    """
    assert (
        _determine_status(
            last_success_at=datetime.utcnow(),
            lag_seconds=18,
            consecutive_failures_max=0,
            last_error=None,
            stale_stream="siem_events",
        )
        == "unhealthy"
    )


def test_sem_stale_stream_a_regra_nao_muda_nada() -> None:
    assert (
        _determine_status(
            last_success_at=datetime.utcnow(),
            lag_seconds=18,
            consecutive_failures_max=0,
            last_error=None,
            stale_stream=None,
        )
        == "healthy"
    )
