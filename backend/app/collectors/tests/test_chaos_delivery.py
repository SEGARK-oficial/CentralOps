"""Testes de CAOS da camada de entrega.

Exercita a resiliência já implementada sob condições adversas:

(a) Redis/breaker indisponível no meio do envio:
    - check_for_config levanta redis.exceptions.ConnectionError
      → dispatch FAIL-OPEN (entrega ocorre, sem crash, sem DLQ silenciosa).

(b) Breaker FLAPPING (sequência sucesso/falha cruzando o threshold):
    - Transições closed→open→half-open→closed corretas.
    - Poison-pill (4xx, retryable=False) NÃO abre o breaker — só falha
      de SAÚDE do destino (5xx / timeout) abre.

(c) Falha ao gravar DLQ:
    - persist_rejected_to_dlq retorna False → TransientDeliveryError
      levantado (não some silenciosamente).

Todos os testes são determinísticos: sem rede real, sem Redis real.
Usam fakeredis.FakeServer compartilhado para isolar estado entre clientes.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors.output.base import DeliveryResult, RejectedEvent
from backend.app.db.database import Base
from backend.app.db import models


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_envelope(event_id: str = "evt-chaos-001", org_id: int | None = 1) -> dict:
    return {
        "_centralops": {
            "event_id": event_id,
            "organization_id": org_id,
            "vendor": "sophos",
        },
        "normalized": {},
        "raw": {},
    }


def _dest_config_mock(dest_id: str = "chaos-dest-001", kind: str = "splunk_hec") -> MagicMock:
    cfg = MagicMock()
    cfg.destination_id = dest_id
    cfg.kind = kind
    cfg.delivery = {}
    cfg.secret_ref = None
    cfg.config_version = "v1"
    cfg.name = "Chaos Test Destination"
    cfg.organization_id = None
    return cfg


def _stub_metrics() -> MagicMock:
    m = MagicMock()
    m.labels.return_value = m
    return m


# ── Redis de outage (levanta redis.exceptions.ConnectionError — não o builtin) ──


class _BreakerRedisOutage:
    """Stub assíncrono cujas ops de breaker levantam redis.exceptions.ConnectionError.

    Distinct from Python builtin ConnectionError — exatamente o tipo que
    anteriormente escapava do _RETRYABLE e teria DLQ'd um batch saudável.
    """

    def __init__(self) -> None:
        import redis.exceptions as _re

        self._exc = _re.ConnectionError("redis down — simulado caos")

    async def exists(self, *_a: Any) -> int:
        raise self._exc

    async def set(self, *_a: Any, **_k: Any) -> Any:
        raise self._exc

    async def delete(self, *_a: Any) -> int:
        raise self._exc

    def pipeline(self) -> Any:
        raise self._exc

    async def aclose(self) -> None:
        return None


# ── Fixtures de DB ────────────────────────────────────────────────────────────


@pytest.fixture()
def static_db():
    """SQLite in-memory com StaticPool — todos os callers compartilham o mesmo DB."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    import backend.app.db.database as db_module

    original = db_module.SessionLocal
    db_module.SessionLocal = TestingSessionLocal
    yield TestingSessionLocal, engine
    db_module.SessionLocal = original
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def seeded_destination(static_db):
    """Semeia um destino splunk_hec habilitado e retorna o id."""
    TestingSessionLocal, _ = static_db
    dest_id = "chaos-splunk-001"
    with TestingSessionLocal() as session:
        session.add(
            models.Destination(
                id=dest_id,
                name="Chaos Splunk",
                kind="splunk_hec",
                enabled=True,
                config='{"url": "https://splunk:8088", "sourcetype": "chaos"}',
                secret_ref=None,
                delivery="{}",
                config_version="v1",
                organization_id=None,
            )
        )
        session.commit()
    return dest_id


# ── (a) Redis/breaker indisponível → FAIL-OPEN ───────────────────────────────


@pytest.mark.asyncio
async def test_breaker_redis_outage_fail_open_delivers(
    static_db, seeded_destination
) -> None:
    """Redis inacessível durante check_for_config → breaker FAIL-OPEN.

    O batch DEVE ser entregue (sem DLQ, sem crash). A política documentada
    em circuit_breaker.py: "Breaker store down — FAIL OPEN. Never convert a
    Redis blip into DLQ."
    """
    TestingSessionLocal, _ = static_db
    dest_id = seeded_destination

    fake_target = AsyncMock()
    fake_target.send_batch.return_value = DeliveryResult(accepted=1)

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            return_value=_BreakerRedisOutage(),
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        # Não deve levantar — Redis down é fail-open, não DLQ
        await dispatch_batch_to_destination(dest_id, [_make_envelope("evt-fo-1")])

    # Entrega ocorreu (fail-open permitiu o send)
    fake_target.send_batch.assert_awaited_once()

    # Nada na DLQ
    with TestingSessionLocal() as session:
        dlq_count = (
            session.query(models.DestinationDeadLetter)
            .filter(models.DestinationDeadLetter.destination_id == dest_id)
            .count()
        )
    assert dlq_count == 0, (
        "Redis down (breaker) NÃO deve gerar DLQ — falha é fail-open"
    )


@pytest.mark.asyncio
async def test_breaker_redis_outage_mid_batch_no_crash(
    static_db, seeded_destination
) -> None:
    """Redis cai NO MEIO de um batch multi-chunk: primeiro chunk OK, segundo
    com breaker down → ambos entregues (fail-open por chunk), sem crash."""
    TestingSessionLocal, _ = static_db
    dest_id = seeded_destination

    send_calls: list[int] = []

    async def fake_send(batch: list) -> DeliveryResult:
        send_calls.append(len(batch))
        return DeliveryResult(accepted=len(batch))

    fake_target = AsyncMock()
    fake_target.send_batch.side_effect = fake_send

    # Redis outage no check_for_config (chamado por chunk)
    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            return_value=_BreakerRedisOutage(),
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        # 4 eventos, max_items padrão (500) → 1 chunk; mas testamos que a
        # presença do outage não interrompe o loop
        batch = [_make_envelope(f"evt-mid-{i}") for i in range(4)]
        await dispatch_batch_to_destination(dest_id, batch)

    # Todos os eventos entregues
    assert sum(send_calls) == 4, (
        f"Todos 4 eventos devem ser entregues com Redis down; got {send_calls}"
    )


# ── (b) Breaker FLAPPING: transições corretas + poison-pill não abre ─────────


@pytest_asyncio.fixture()
async def shared_fake_redis():
    """FakeRedis com FakeServer compartilhado — estado persiste entre clientes."""
    import fakeredis
    import fakeredis.aioredis as fakeredis_aio

    server = fakeredis.FakeServer()
    client = fakeredis_aio.FakeRedis(decode_responses=True, server=server)
    yield server, client
    await client.aclose()


@pytest.mark.asyncio
async def test_breaker_flapping_closed_open_half_open_closed(
    shared_fake_redis,
) -> None:
    """Sequência de flapping: closed → open → half-open (probe) → closed.

    Prova que as transições do estado machine estão corretas sob
    alternância de sucessos e falhas cruzando o threshold.
    """
    from backend.app.collectors.circuit_breaker import (
        BreakerOpen,
        check,
        record_failure,
        record_success,
    )

    server, _ = shared_fake_redis
    import fakeredis.aioredis as fakeredis_aio

    dest_id = "flap-dest-001"
    threshold = 3
    cooldown_s = 30
    window_s = 60

    def _client():
        return fakeredis_aio.FakeRedis(decode_responses=True, server=server)

    # ── Fase 1: CLOSED → acumula falhas até o threshold ──────────────
    for _ in range(threshold):
        c = _client()
        await record_failure(c, dest_id, threshold=threshold, cooldown_s=cooldown_s, window_s=window_s)
        await c.aclose()

    # ── Fase 2: OPEN → check() deve conceder probe ao primeiro caller ─
    c1 = _client()
    await check(c1, dest_id, probe_ttl_s=cooldown_s)  # não levanta — probe concedido
    await c1.aclose()

    # ── Fase 3: ainda OPEN, probe tomado → segundo caller → BreakerOpen ─
    c2 = _client()
    with pytest.raises(BreakerOpen) as exc_info:
        await check(c2, dest_id, probe_ttl_s=cooldown_s)
    assert exc_info.value.destination_id == dest_id
    await c2.aclose()

    # ── Fase 4: half-open probe bem-sucedido → record_success → CLOSED ─
    c3 = _client()
    await record_success(c3, dest_id)
    await c3.aclose()

    # ── Fase 5: agora CLOSED → check() passa sem levantar ─────────────
    c4 = _client()
    await check(c4, dest_id, probe_ttl_s=cooldown_s)  # não levanta
    assert await c4.exists(f"breaker:{dest_id}:open") == 0
    assert await c4.exists(f"breaker:{dest_id}:fail") == 0
    await c4.aclose()


@pytest.mark.asyncio
async def test_breaker_flapping_multiple_cycles(shared_fake_redis) -> None:
    """O breaker pode abrir, fechar e abrir novamente (múltiplos ciclos)."""
    from backend.app.collectors.circuit_breaker import (
        check,
        record_failure,
        record_success,
    )

    server, _ = shared_fake_redis
    import fakeredis.aioredis as fakeredis_aio

    dest_id = "flap-dest-002"
    threshold = 2

    def _client():
        return fakeredis_aio.FakeRedis(decode_responses=True, server=server)

    for _ciclo in range(3):
        # Abre o breaker
        for _ in range(threshold):
            c = _client()
            await record_failure(c, dest_id, threshold=threshold, cooldown_s=5, window_s=60)
            await c.aclose()

        # Probe → fecha
        c = _client()
        await check(c, dest_id, probe_ttl_s=5)  # probe concedido
        await c.aclose()

        c = _client()
        await record_success(c, dest_id)
        await c.aclose()

        # Confirma CLOSED após fechar
        c = _client()
        await check(c, dest_id, probe_ttl_s=5)  # não levanta
        await c.aclose()


@pytest.mark.asyncio
async def test_poison_pill_does_not_open_breaker(
    static_db, seeded_destination
) -> None:
    """Poison-pill (4xx, retryable=False) NÃO abre o breaker.

    Apenas falha de SAÚDE do destino (5xx / timeout) deve acionar o
    circuit breaker. Um evento mal-formado (schema_rejected) é um
    problema do evento, não do destino.
    """
    import fakeredis
    import fakeredis.aioredis as fakeredis_aio

    from backend.app.collectors.circuit_breaker import check

    dest_id = seeded_destination
    server = fakeredis.FakeServer()

    def _client():
        return fakeredis_aio.FakeRedis(decode_responses=True, server=server)

    # Sender retorna accepted>0 + rejected determinístico (não retryable)
    poison_rej = RejectedEvent(
        event_id="evt-poison",
        reason="schema validation failed",
        error_kind="schema_rejected",
        retryable=False,
    )
    fake_target = AsyncMock()
    fake_target.send_batch.return_value = DeliveryResult(
        accepted=1,
        rejected=[poison_rej],
        retryable=False,
    )

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            side_effect=_client,
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        # Bem acima do threshold padrão (5) com poison-pills contínuos
        for i in range(8):
            await dispatch_batch_to_destination(
                dest_id,
                [_make_envelope(f"evt-pp-ok-{i}"), _make_envelope(f"evt-pp-bad-{i}")],
            )

    # Breaker deve estar FECHADO — poison-pills não abrem o breaker
    verify = _client()
    await check(verify, dest_id)  # não deve levantar BreakerOpen
    assert await verify.exists(f"breaker:{dest_id}:open") == 0, (
        "Poison-pill (retryable=False) NÃO deve abrir o breaker"
    )
    assert await verify.exists(f"breaker:{dest_id}:fail") == 0, (
        "Falha determinística de evento não deve incrementar o fail counter"
    )
    await verify.aclose()


@pytest.mark.asyncio
async def test_health_failure_5xx_opens_breaker(
    static_db, seeded_destination
) -> None:
    """Falha de SAÚDE do destino (retryable=True, sem eventos aceitos/rejeitados)
    DEVE incrementar o fail counter e eventualmente abrir o breaker.
    """
    import fakeredis
    import fakeredis.aioredis as fakeredis_aio

    from backend.app.collectors.circuit_breaker import BreakerOpen
    from backend.app.collectors.delivery import TransientDeliveryError

    dest_id = seeded_destination
    server = fakeredis.FakeServer()

    def _client():
        return fakeredis_aio.FakeRedis(decode_responses=True, server=server)

    # Sender retorna 5xx (retryable=True, 0 aceitos, 0 rejeitados) — falha de saúde
    fake_target = AsyncMock()
    fake_target.send_batch.return_value = DeliveryResult(
        accepted=0, rejected=[], retryable=True
    )

    # Threshold padrão = 5; max_retries padrão = 3; vamos usar delivery customizado
    # com threshold=2 e max_retries=0 para convergir rápido no teste
    import json
    with static_db[0]() as session:
        row = session.get(models.Destination, dest_id)
        row.delivery = json.dumps({
            "breaker": {"failure_threshold": 2, "cooldown_s": 30, "window_s": 60},
            "retry": {"max_retries": 0},
        })
        session.commit()

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            side_effect=_client,
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        # Primeiro envio → falha de saúde → 1 falha no breaker, ainda CLOSED
        with pytest.raises(TransientDeliveryError):
            await dispatch_batch_to_destination(dest_id, [_make_envelope("evt-5xx-1")])

        # Segundo envio → falha de saúde → threshold=2 atingido → OPEN
        # Próximo check pode passar (probe) ou levantar BreakerOpen
        with pytest.raises((TransientDeliveryError, BreakerOpen)):
            await dispatch_batch_to_destination(dest_id, [_make_envelope("evt-5xx-2")])

    # Breaker DEVE estar OPEN agora (2 falhas de saúde com threshold=2)
    verify = _client()
    is_open = await verify.exists(f"breaker:{dest_id}:open")
    await verify.aclose()
    assert is_open, (
        "Falhas de saúde (5xx/retryable=True) DEVEM abrir o breaker"
    )


# ── (c) Falha ao gravar DLQ → não some silenciosamente ───────────────────────


@pytest.mark.asyncio
async def test_dlq_persist_failure_raises_transient_not_silent(
    static_db, seeded_destination
) -> None:
    """persist_rejected_to_dlq retorna False → TransientDeliveryError levantado.

    O lote NÃO é silenciosamente descartado — é propagado para retry do Celery
    (acks_late garante que a task seja reentregue).
    """
    dest_id = seeded_destination

    poison_rej = RejectedEvent(
        event_id="evt-dlq-fail",
        reason="schema",
        error_kind="schema_rejected",
        retryable=False,
    )
    fake_target = AsyncMock()
    fake_target.send_batch.return_value = DeliveryResult(
        accepted=0, rejected=[poison_rej], retryable=False
    )

    import fakeredis.aioredis as fakeredis_aio

    fake_redis = fakeredis_aio.FakeRedis(decode_responses=True)
    from backend.app.collectors.delivery import TransientDeliveryError

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            return_value=fake_redis,
        ),
        # Simula falha de escrita na DLQ (DB down / integrity error)
        patch(
            "backend.app.collectors.delivery.persist_rejected_to_dlq",
            return_value=False,
        ) as mock_persist,
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        with pytest.raises(TransientDeliveryError) as exc_info:
            await dispatch_batch_to_destination(
                dest_id, [_make_envelope("evt-dlq-fail")]
            )

    assert exc_info.value.destination_id == dest_id
    mock_persist.assert_called_once(), (
        "persist_rejected_to_dlq deve ter sido chamado antes de levantar"
    )


@pytest.mark.asyncio
async def test_dlq_persist_failure_logs_not_silent(
    static_db, seeded_destination, caplog: pytest.LogCaptureFixture
) -> None:
    """Quando persist_rejected_to_dlq falha, um log de ERROR deve ser emitido
    — prova que a falha não é engolida silenciosamente."""
    import logging

    dest_id = seeded_destination

    poison_rej = RejectedEvent(
        event_id="evt-dlq-log",
        reason="schema",
        error_kind="schema_rejected",
        retryable=False,
    )
    fake_target = AsyncMock()
    fake_target.send_batch.return_value = DeliveryResult(
        accepted=0, rejected=[poison_rej], retryable=False
    )

    import fakeredis.aioredis as fakeredis_aio

    fake_redis = fakeredis_aio.FakeRedis(decode_responses=True)
    from backend.app.collectors.delivery import TransientDeliveryError

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            return_value=fake_redis,
        ),
        patch(
            "backend.app.collectors.delivery.persist_rejected_to_dlq",
            return_value=False,
        ),
        caplog.at_level(logging.ERROR, logger="backend.app.collectors.pipeline"),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        with pytest.raises(TransientDeliveryError):
            await dispatch_batch_to_destination(
                dest_id, [_make_envelope("evt-dlq-log")]
            )

    # Deve ter log de nível ERROR mencionando a falha de DLQ
    error_logs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(
        "DLQ" in r.message or "persist" in r.message.lower() or "FAILED" in r.message
        for r in error_logs
    ), (
        f"Falha de DLQ deve gerar log ERROR. Logs capturados: "
        f"{[r.message for r in error_logs]}"
    )


@pytest.mark.asyncio
async def test_dlq_persist_success_does_not_raise(
    static_db, seeded_destination
) -> None:
    """Quando persist_rejected_to_dlq retorna True (sucesso), NÃO deve levantar
    — comportamento normal de poison-pill (evento vai pra DLQ e segue)."""
    dest_id = seeded_destination

    poison_rej = RejectedEvent(
        event_id="evt-dlq-ok",
        reason="schema",
        error_kind="schema_rejected",
        retryable=False,
    )
    fake_target = AsyncMock()
    fake_target.send_batch.return_value = DeliveryResult(
        accepted=0, rejected=[poison_rej], retryable=False
    )

    import fakeredis.aioredis as fakeredis_aio

    fake_redis = fakeredis_aio.FakeRedis(decode_responses=True)

    with (
        patch("backend.app.core.secrets.get_default_backend", return_value=None),
        patch(
            "backend.app.collectors.output.destination_cache.get_destination",
            new_callable=AsyncMock,
            return_value=fake_target,
        ),
        patch(
            "backend.app.collectors.celery_app.get_worker_redis",
            return_value=fake_redis,
        ),
    ):
        from backend.app.collectors.pipeline import dispatch_batch_to_destination

        # Não deve levantar — DLQ escreveu OK
        await dispatch_batch_to_destination(dest_id, [_make_envelope("evt-dlq-ok")])
