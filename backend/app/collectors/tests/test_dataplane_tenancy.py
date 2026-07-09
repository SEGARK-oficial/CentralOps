"""Guardas de tenancy + config do data-plane.

- Gate cross-tenant fail-closed em ``dispatch_batch_to_destination`` (um
  destino de OUTRO tenant nomeado por rota mal-escopada NÃO recebe os eventos —
  vai à DLQ). Cobre AMBAS as lanes (Kafka consumer + Celery worker chamam aqui).
- O validador fail-fast SASL_* ⇒ exige KAFKA_SASL_MECHANISM.
"""

from __future__ import annotations

import os
import types

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors import delivery as _delivery
from backend.app.collectors import pipeline


async def test_cross_tenant_destination_fail_closed(monkeypatch):
    """Destino org=2, lote org=1 → recusa fail-closed: DLQ(cross_tenant_destination),
    sem entregar (return antes do get_destination/send)."""
    dlq: list = []

    def _fake_dlq(batch, *, destination_id, error_kind, organization_id=None):
        dlq.append(
            {"error_kind": error_kind, "destination_id": destination_id, "org": organization_id}
        )

    monkeypatch.setattr(_delivery, "persist_batch_dlq", _fake_dlq)
    # Destino pertence ao tenant 2.
    monkeypatch.setattr(
        pipeline,
        "_load_destination_config",
        lambda did: types.SimpleNamespace(
            destination_id=did, organization_id=2, kind="elastic", delivery="{}", secret_ref=None
        ),
    )
    # get_destination NUNCA deve ser alcançado (o gate retorna antes).
    async def _boom(*_a, **_k):  # pragma: no cover — não deve rodar
        raise AssertionError("get_destination não deveria ser chamado num cross-tenant")

    monkeypatch.setattr(
        "backend.app.collectors.output.destination_cache.get_destination", _boom
    )

    # Lote pertence ao tenant 1.
    batch = [{"_centralops": {"organization_id": 1}, "msg": "x"}]
    await pipeline.dispatch_batch_to_destination("d1", batch)

    assert dlq and dlq[0]["error_kind"] == "cross_tenant_destination"
    assert dlq[0]["destination_id"] == "d1"
    assert dlq[0]["org"] == 1


async def test_same_tenant_destination_passes_gate(monkeypatch):
    """Mesmo tenant (org=1==1) NÃO é barrado pelo gate — segue ao get_destination
    (que aqui sinaliza que passou). Prova que o gate não é fail-closed demais."""
    reached = {"get_destination": False}

    monkeypatch.setattr(
        pipeline,
        "_load_destination_config",
        lambda did: types.SimpleNamespace(
            destination_id=did, organization_id=1, kind="elastic", delivery="{}", secret_ref=None
        ),
    )

    async def _marker(*_a, **_k):
        reached["get_destination"] = True
        raise RuntimeError("stop-after-gate")  # corta o resto do dispatch no teste

    monkeypatch.setattr(
        "backend.app.collectors.output.destination_cache.get_destination", _marker
    )

    batch = [{"_centralops": {"organization_id": 1}, "msg": "x"}]
    with pytest.raises(Exception):
        await pipeline.dispatch_batch_to_destination("d1", batch)
    assert reached["get_destination"] is True  # passou do gate (mesmo tenant)


def test_sasl_protocol_requires_mechanism():
    """SASL_SSL sem KAFKA_SASL_MECHANISM falha-rápido no boot."""
    from backend.app.core.config import Settings

    with pytest.raises(Exception):
        Settings(KAFKA_SECURITY_PROTOCOL="SASL_SSL")  # sem mecanismo → ValidationError

    # Com mecanismo: ok.
    s = Settings(KAFKA_SECURITY_PROTOCOL="SASL_SSL", KAFKA_SASL_MECHANISM="SCRAM-SHA-256")
    assert s.KAFKA_SASL_MECHANISM == "SCRAM-SHA-256"

    # PLAINTEXT (default) não exige mecanismo.
    assert Settings().KAFKA_SECURITY_PROTOCOL == "PLAINTEXT"
