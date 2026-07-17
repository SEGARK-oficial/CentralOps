"""Testes para DashboardSummaryV2 — o payload ÚNICO do dashboard.

Cobertura:
- KpiCard / BucketSection / DashboardSummaryV2: validação Pydantic.
- build_dashboard_summary_v2: KPIs do funil vendor-neutro.
- Buckets vendor-neutros presentes; buckets/KPIs de ALERTS AUSENTES (superfície
  Wazuh-only removida — sem shape v1, sem Accept vendorizado).
- Envelope consolidado: organizations/integrations (degraded_items/by_platform/
  contagens) presentes no v2.
- Degradação graciosa quando subsistemas (funnel_data) estão vazios.
- GET /dashboard/summary: retorna SEMPRE v2; Accept v1 não muda o shape.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app
from backend.app.routers.dashboard import (
    _collect_integration_health,
    _days_to_window,
    build_dashboard_summary_v2,
)
from backend.app.schemas.dashboard import (
    BucketItem,
    BucketSection,
    DashboardSummaryV2,
    KpiCard,
)


# ── Schema unit tests ─────────────────────────────────────────────────────────


class TestKpiCardSchema:
    def test_minimal(self):
        k = KpiCard(id="ingest_eps", label="Ingestão", value=42)
        assert k.value == 42
        assert k.trend is None

    def test_trend_valid(self):
        k = KpiCard(id="x", label="X", value=0, trend="up", trend_value="+5")
        assert k.trend == "up"

    def test_invalid_trend_rejected(self):
        with pytest.raises(Exception):
            KpiCard(id="x", label="X", value=0, trend="sideways")


class TestBucketSectionSchema:
    def test_empty_items(self):
        s = BucketSection(id="top_sources_volume", label="Top fontes", items=[])
        assert s.items == []

    def test_with_items(self):
        item = BucketItem(id="src1", label="Source 1", value=10)
        s = BucketSection(id="top_sources_volume", label="Top fontes", items=[item])
        assert len(s.items) == 1

    def test_bucket_item_href_optional(self):
        item = BucketItem(id="x", label="X", value=1, href="/pipeline?id=x")
        assert item.href == "/pipeline?id=x"


class TestDashboardSummaryV2Schema:
    def test_schema_version_fixed(self):
        d = DashboardSummaryV2(
            window="7d",
            generated_at=datetime.now(timezone.utc),
            kpis=[],
            top_buckets=[],
        )
        assert d.schema_version == 2

    def test_invalid_window_rejected(self):
        with pytest.raises(Exception):
            DashboardSummaryV2(
                window="1w",
                generated_at=datetime.now(timezone.utc),
                kpis=[],
                top_buckets=[],
            )

    def test_scope_sections_default_to_zeros(self):
        """organizations/integrations são aditivos com defaults seguros."""
        d = DashboardSummaryV2(
            window="7d",
            generated_at=datetime.now(timezone.utc),
            kpis=[],
            top_buckets=[],
        )
        assert d.organizations.total == 0
        assert d.integrations.total == 0
        assert d.integrations.degraded_items == []
        assert d.integrations.by_platform == {}


# ── _days_to_window ───────────────────────────────────────────────────────────


class TestDaysToWindow:
    def test_1_day(self):
        assert _days_to_window(1) == "24h"

    def test_7_days(self):
        assert _days_to_window(7) == "7d"

    def test_30_days(self):
        assert _days_to_window(30) == "30d"

    def test_90_days(self):
        assert _days_to_window(90) == "30d"


# ── helpers ───────────────────────────────────────────────────────────────────


def _sample_summary() -> dict:
    """Summary dict (organizations/integrations) como o handler monta."""
    return {
        "organizations": {"total": 3, "active": 3},
        "integrations": {
            "total": 4,
            "active": 4,
            "authenticated": 3,
            "by_platform": {"wazuh": 2, "sophos": 2},
            "health": {"healthy": 3, "degraded": 1, "error": 0, "unknown": 0, "inactive": 0},
            "degraded_items": [
                {
                    "integration_id": 7,
                    "integration_name": "Wazuh Prod",
                    "organization_id": 1,
                    "organization_name": "Acme Corp",
                    "status": "degraded",
                    "last_error": "indexer: Authentication failed",
                    "last_checked_at": None,
                },
            ],
            "comparison": {
                "degraded_integrations": {"current": 1, "previous": 0, "delta": 1, "trend": "up"},
            },
        },
    }


def _sample_funnel_data() -> dict:
    """Representative funnel_data from _collect_funnel_db + _collect_funnel_redis."""
    return {
        "ph_items": [
            {
                "integration_id": 1,
                "integration_name": "Wazuh Prod",
                "organization_name": "Acme Corp",
                "events_per_minute": 600.0,  # 10 EPS
                "mapped_field_ratio": 0.95,
                "quarantine_count_24h": 5,
                "drift_count_24h": 2,
            },
            {
                "integration_id": 2,
                "integration_name": "Sophos Central",
                "organization_name": "Beta Ltd",
                "events_per_minute": 120.0,  # 2 EPS
                "mapped_field_ratio": 0.70,
                "quarantine_count_24h": 0,
                "drift_count_24h": 0,
            },
        ],
        "dest_rows": [
            {"id": "dest-1", "name": "Splunk HEC", "kind": "splunk_hec", "enabled": True},
            {"id": "dest-2", "name": "Sentinel SIEM", "kind": "sentinel", "enabled": True},
            {"id": "dest-3", "name": "S3 Archive", "kind": "s3", "enabled": False},
        ],
        "dest_dlq": {
            "dest-1": {"dlq_24h": 0, "dlq_total": 0, "last_dlq_at": None},
            "dest-2": {"dlq_24h": 3, "dlq_total": 10, "last_dlq_at": None},
            "dest-3": {"dlq_24h": 0, "dlq_total": 0, "last_dlq_at": None},
        },
        "dest_eps": {
            "dest-1": 8.5,
            "dest-2": 1.2,
            "dest-3": 0.0,
        },
        "route_rows": [
            {"id": "route-a", "name": "Broadcast all", "action": "route", "destination_ids": ["dest-1"]},
            {"id": "route-b", "name": "Drop noisy", "action": "drop", "destination_ids": []},
        ],
        "route_metrics": {
            "route-a": {"matched_per_min": 12.0, "routed_per_min": 11.5, "drop_per_min": 0.5},
            "route-b": {"matched_per_min": 3.0, "routed_per_min": 0.0, "drop_per_min": 3.0},
        },
    }


# ── build_dashboard_summary_v2 — funnel KPIs ─────────────────────────────────


class TestBuildDashboardSummaryV2FunnelKpis:
    """Funnel KPIs: IDs esperados, valores calculados, severidades."""

    def _build(self, funnel: dict | None = None, summary: dict | None = None) -> DashboardSummaryV2:
        return build_dashboard_summary_v2(
            summary=summary or _sample_summary(),
            days=7,
            generated_at=datetime.now(timezone.utc),
            funnel_data=funnel,
        )

    def test_returns_correct_type(self):
        v2 = self._build()
        assert isinstance(v2, DashboardSummaryV2)
        assert v2.schema_version == 2

    def test_window_derived_from_days(self):
        v2 = self._build()
        assert v2.window == "7d"

    def test_funnel_kpi_ids_present(self):
        v2 = self._build(_sample_funnel_data())
        kpi_ids = {k.id for k in v2.kpis}
        expected = {
            "ingest_eps",
            "mapping_coverage",
            "quarantine_rate",
            "routed_events",
            "destinations_healthy",
            "active_sources",
        }
        assert expected <= kpi_ids

    def test_alert_kpi_ids_never_present(self):
        """A superfície de alerts foi removida — nenhum KPI de alerts no v2."""
        v2 = self._build(_sample_funnel_data())
        kpi_ids = {k.id for k in v2.kpis}
        assert "total_alerts" not in kpi_ids
        assert "critical_alerts" not in kpi_ids
        assert "total_orgs" not in kpi_ids
        assert "total_integrations" not in kpi_ids
        assert "degraded_integrations" not in kpi_ids
        assert "last_event" not in kpi_ids

    def test_ingest_eps_value(self):
        """600 + 120 epm → 720/60 = 12.0 EPS."""
        v2 = self._build(_sample_funnel_data())
        kpi = next(k for k in v2.kpis if k.id == "ingest_eps")
        assert kpi.value == 12.0

    def test_mapping_coverage_warn_below_80_pct(self):
        """avg(0.95, 0.70) = 0.825 ≥ 0.80 → ok."""
        v2 = self._build(_sample_funnel_data())
        kpi = next(k for k in v2.kpis if k.id == "mapping_coverage")
        # avg = (0.95+0.70)/2 = 0.825 → ok
        assert kpi.severity == "ok"
        assert "%" in str(kpi.value)

    def test_mapping_coverage_warn_when_avg_below_80(self):
        fd = _sample_funnel_data()
        fd["ph_items"][0]["mapped_field_ratio"] = 0.60
        fd["ph_items"][1]["mapped_field_ratio"] = 0.70
        v2 = self._build(fd)
        kpi = next(k for k in v2.kpis if k.id == "mapping_coverage")
        assert kpi.severity == "warn"

    def test_mapping_coverage_dash_when_no_ratio(self):
        fd = _sample_funnel_data()
        for item in fd["ph_items"]:
            item["mapped_field_ratio"] = None
        v2 = self._build(fd)
        kpi = next(k for k in v2.kpis if k.id == "mapping_coverage")
        assert kpi.value == "—"
        assert kpi.severity is None

    def test_quarantine_rate_absolute_when_no_epm(self):
        """Zero EPS → absolute count fallback."""
        fd = _sample_funnel_data()
        for item in fd["ph_items"]:
            item["events_per_minute"] = 0
            item["quarantine_count_24h"] = 10
        v2 = self._build(fd)
        kpi = next(k for k in v2.kpis if k.id == "quarantine_rate")
        assert kpi.value == 20  # 10+10
        assert kpi.severity == "warn"  # > 0 but <= 100

    def test_quarantine_rate_percentage_with_epm(self):
        """epm > 0 → rate expressed as % string."""
        v2 = self._build(_sample_funnel_data())
        kpi = next(k for k in v2.kpis if k.id == "quarantine_rate")
        assert isinstance(kpi.value, str)
        assert kpi.value.endswith("%")

    def test_routed_events_value_and_drop_rate(self):
        """matched=15/min, routed=11.5/min, drop=3.5/min, drop_rate=23.3%."""
        v2 = self._build(_sample_funnel_data())
        kpi = next(k for k in v2.kpis if k.id == "routed_events")
        # routed = 11.5 + 0 = 11.5 → round = 12
        assert kpi.value == 12
        assert "drop" in str(kpi.sub)

    def test_destinations_healthy_count(self):
        """dest-1 ok, dest-2 has dlq_24h>0 (unhealthy), dest-3 disabled (unhealthy)."""
        v2 = self._build(_sample_funnel_data())
        kpi = next(k for k in v2.kpis if k.id == "destinations_healthy")
        # dest-1: enabled + dlq_24h=0 → healthy; dest-2: dlq_24h=3 → unhealthy
        # dest-3: disabled → unhealthy
        assert kpi.value == "1/3"
        assert kpi.severity == "critical"  # 2 unhealthy

    def test_destinations_all_healthy(self):
        fd = _sample_funnel_data()
        for d_id in ["dest-1", "dest-2", "dest-3"]:
            fd["dest_dlq"][d_id]["dlq_24h"] = 0
        for d in fd["dest_rows"]:
            d["enabled"] = True
        v2 = self._build(fd)
        kpi = next(k for k in v2.kpis if k.id == "destinations_healthy")
        assert kpi.value == "3/3"
        assert kpi.severity == "ok"

    def test_active_sources_kpi(self):
        summary = _sample_summary()
        summary["integrations"]["active"] = 4
        summary["integrations"]["health"]["degraded"] = 1
        v2 = self._build(_sample_funnel_data(), summary=summary)
        kpi = next(k for k in v2.kpis if k.id == "active_sources")
        assert kpi.value == 4
        assert "1 com erro" in (kpi.sub or "")
        assert kpi.severity == "critical"  # degraded > 0


# ── build_dashboard_summary_v2 — vendor-neutral buckets ──────────────────────


class TestBuildDashboardSummaryV2Buckets:
    def _build(self, funnel: dict | None = None, summary: dict | None = None) -> DashboardSummaryV2:
        return build_dashboard_summary_v2(
            summary=summary or _sample_summary(),
            days=7,
            generated_at=datetime.now(timezone.utc),
            funnel_data=funnel,
        )

    def test_vendor_neutral_bucket_ids_present(self):
        v2 = self._build(_sample_funnel_data())
        bucket_ids = {b.id for b in v2.top_buckets}
        expected = {"top_sources_volume", "top_destinations_volume", "top_quarantine", "top_route_drops"}
        assert expected <= bucket_ids

    def test_old_vendor_bucket_ids_absent(self):
        v2 = self._build(_sample_funnel_data())
        bucket_ids = {b.id for b in v2.top_buckets}
        assert "top_hosts" not in bucket_ids
        assert "top_rules" not in bucket_ids
        assert "top_mitre" not in bucket_ids
        assert "top_agent_groups" not in bucket_ids

    def test_alerts_by_severity_never_present(self):
        v2 = self._build(_sample_funnel_data())
        bucket_ids = {b.id for b in v2.top_buckets}
        assert "alerts_by_severity" not in bucket_ids

    def test_top_sources_volume_ordered_by_epm(self):
        v2 = self._build(_sample_funnel_data())
        section = next(b for b in v2.top_buckets if b.id == "top_sources_volume")
        assert len(section.items) == 2
        # Wazuh (600 epm) > Sophos (120 epm)
        assert section.items[0].label == "Wazuh Prod"
        assert section.items[0].value == 600.0

    def test_top_destinations_volume_ordered_by_eps(self):
        v2 = self._build(_sample_funnel_data())
        section = next(b for b in v2.top_buckets if b.id == "top_destinations_volume")
        assert len(section.items) >= 1
        # dest-1 (eps=8.5) > dest-2 (eps=1.2) > dest-3 (eps=0 → excluded)
        assert section.items[0].label == "Splunk HEC"
        assert section.items[0].value == 8.5

    def test_top_quarantine_ordered_by_count(self):
        v2 = self._build(_sample_funnel_data())
        section = next(b for b in v2.top_buckets if b.id == "top_quarantine")
        assert len(section.items) == 1  # only Wazuh has quarantine_count_24h > 0
        assert section.items[0].label == "Wazuh Prod"
        assert section.items[0].value == 5

    def test_top_route_drops_ordered_by_drop_rate(self):
        v2 = self._build(_sample_funnel_data())
        section = next(b for b in v2.top_buckets if b.id == "top_route_drops")
        assert len(section.items) >= 1
        # route-b: drop_per_min=3.0 > route-a: 0.5
        assert section.items[0].label == "Drop noisy"
        assert section.items[0].value == 3.0

    def test_top_sources_empty_when_no_epm(self):
        fd = _sample_funnel_data()
        for item in fd["ph_items"]:
            item["events_per_minute"] = 0
        v2 = self._build(fd)
        section = next(b for b in v2.top_buckets if b.id == "top_sources_volume")
        assert section.items == []

    def test_top_buckets_limited_to_5(self):
        fd = _sample_funnel_data()
        for i in range(10):
            fd["ph_items"].append({
                "integration_id": 100 + i,
                "integration_name": f"Extra-{i}",
                "organization_name": "Org",
                "events_per_minute": float(i + 1),
                "mapped_field_ratio": 0.9,
                "quarantine_count_24h": i + 1,
                "drift_count_24h": 0,
            })
        v2 = self._build(fd)
        for bucket in v2.top_buckets:
            assert len(bucket.items) <= 5, f"bucket {bucket.id!r} has {len(bucket.items)} items"


# ── build_dashboard_summary_v2 — envelope consolidado (ex-v1) ────────────────


class TestBuildDashboardSummaryV2ScopeSections:
    """As contagens/saúde que o frontend lia do v1 agora vivem no v2."""

    def _build(self, summary: dict | None = None) -> DashboardSummaryV2:
        return build_dashboard_summary_v2(
            summary=summary or _sample_summary(),
            days=7,
            generated_at=datetime.now(timezone.utc),
            funnel_data=_sample_funnel_data(),
        )

    def test_organizations_counts_present(self):
        v2 = self._build()
        assert v2.organizations.total == 3
        assert v2.organizations.active == 3

    def test_integrations_counts_and_by_platform(self):
        v2 = self._build()
        assert v2.integrations.total == 4
        assert v2.integrations.active == 4
        assert v2.integrations.authenticated == 3
        assert v2.integrations.by_platform == {"wazuh": 2, "sophos": 2}

    def test_integrations_health_counts(self):
        v2 = self._build()
        assert v2.integrations.health.healthy == 3
        assert v2.integrations.health.degraded == 1
        assert v2.integrations.health.error == 0
        assert v2.integrations.health.inactive == 0

    def test_degraded_items_carried_through(self):
        v2 = self._build()
        assert len(v2.integrations.degraded_items) == 1
        item = v2.integrations.degraded_items[0]
        assert item.integration_id == 7
        assert item.integration_name == "Wazuh Prod"
        assert item.status == "degraded"
        assert item.last_error == "indexer: Authentication failed"

    def test_comparison_carried_through(self):
        v2 = self._build()
        comp = v2.integrations.comparison.degraded_integrations
        assert comp.current == 1
        assert comp.previous == 0
        assert comp.trend == "up"


# ── Degradação graciosa — subsistemas vazios/ausentes ────────────────────────


class TestGracefulDegradation:
    def test_no_funnel_data_does_not_raise(self):
        """funnel_data=None → KPIs degrade to 0/'—' without exception."""
        v2 = build_dashboard_summary_v2(
            summary=_sample_summary(),
            days=1,
            generated_at=datetime.now(timezone.utc),
            funnel_data=None,
        )
        assert v2.schema_version == 2
        kpi_ids = {k.id for k in v2.kpis}
        assert "ingest_eps" in kpi_ids
        # With no funnel data: ingest_eps=0, destinations=0/0 ok
        eps = next(k for k in v2.kpis if k.id == "ingest_eps")
        assert eps.value == 0.0

    def test_empty_funnel_data_does_not_raise(self):
        empty_funnel: dict[str, Any] = {
            "ph_items": [], "dest_rows": [], "dest_dlq": {},
            "dest_eps": {}, "route_rows": [], "route_metrics": {},
        }
        v2 = build_dashboard_summary_v2(
            summary=_sample_summary(),
            days=7,
            generated_at=datetime.now(timezone.utc),
            funnel_data=empty_funnel,
        )
        assert v2.schema_version == 2
        assert v2.window == "7d"

    def test_empty_summary_does_not_raise(self):
        empty_summary: dict[str, Any] = {
            "organizations": {"total": 0, "active": 0},
            "integrations": {
                "total": 0, "active": 0, "authenticated": 0,
                "by_platform": {},
                "health": {"healthy": 0, "degraded": 0, "error": 0, "unknown": 0},
                "degraded_items": [],
                "comparison": {"degraded_integrations": {}},
            },
        }
        v2 = build_dashboard_summary_v2(
            summary=empty_summary,
            days=1,
            generated_at=datetime.now(timezone.utc),
        )
        assert v2.schema_version == 2
        assert v2.window == "24h"
        assert v2.organizations.total == 0
        assert v2.integrations.health.inactive == 0

    def test_missing_sections_do_not_raise(self):
        """summary sem organizations/integrations → defaults (aditivo)."""
        v2 = build_dashboard_summary_v2(
            summary={},
            days=7,
            generated_at=datetime.now(timezone.utc),
        )
        assert v2.organizations.total == 0
        assert v2.integrations.total == 0

    @pytest.mark.parametrize("days,expected_window", [
        (1, "24h"), (7, "7d"), (8, "30d"), (30, "30d"), (90, "30d"),
    ])
    def test_window_parametrized(self, days: int, expected_window: str):
        v2 = build_dashboard_summary_v2(
            summary=_sample_summary(),
            days=days,
            generated_at=datetime.now(timezone.utc),
        )
        assert v2.window == expected_window


# ── Router integration tests ──────────────────────────────────────────────────


@pytest.fixture()
def client_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_session():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_get_session
    clients: list[TestClient] = []

    def factory() -> TestClient:
        client = TestClient(app)
        clients.append(client)
        return client

    yield factory

    for c in clients:
        c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _bootstrap_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPass1!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    r2 = client.post("/api/auth/login", json={"username": "admin", "password": "AdminPass1!"})
    assert r2.status_code == 200, r2.text


_EMPTY_FUNNEL_DB = {"ph_items": [], "dest_rows": [], "dest_dlq": {}, "route_rows": []}
_EMPTY_FUNNEL = {
    "ph_items": [], "dest_rows": [], "dest_dlq": {}, "route_rows": [],
    "dest_eps": {}, "route_metrics": {},
}


class TestDashboardEndpointSingleShape:
    def test_v2_is_the_only_shape(self, client_factory):
        client = client_factory()
        _bootstrap_admin(client)

        with patch("backend.app.routers.dashboard._collect_funnel_db", return_value=dict(_EMPTY_FUNNEL_DB)), \
             patch("backend.app.routers.dashboard._collect_funnel_redis", return_value=dict(_EMPTY_FUNNEL)):
            r = client.get("/api/dashboard/summary")

        assert r.status_code == 200, r.text
        data = r.json()
        assert data["schema_version"] == 2
        assert "kpis" in data
        assert "top_buckets" in data
        assert "alerts" not in data
        assert "X-API-Deprecation" not in r.headers

    def test_v2_contains_funnel_kpi_ids(self, client_factory):
        client = client_factory()
        _bootstrap_admin(client)

        with patch("backend.app.routers.dashboard._collect_funnel_db", return_value=dict(_EMPTY_FUNNEL_DB)), \
             patch("backend.app.routers.dashboard._collect_funnel_redis", return_value=dict(_EMPTY_FUNNEL)):
            r = client.get("/api/dashboard/summary")

        assert r.status_code == 200, r.text
        data = r.json()
        kpi_ids = {k["id"] for k in data["kpis"]}
        assert "ingest_eps" in kpi_ids
        assert "mapping_coverage" in kpi_ids
        assert "quarantine_rate" in kpi_ids
        assert "routed_events" in kpi_ids
        assert "destinations_healthy" in kpi_ids
        assert "active_sources" in kpi_ids
        assert "total_alerts" not in kpi_ids

    def test_v2_does_not_contain_vendor_bucket_ids(self, client_factory):
        client = client_factory()
        _bootstrap_admin(client)

        with patch("backend.app.routers.dashboard._collect_funnel_db", return_value=dict(_EMPTY_FUNNEL_DB)), \
             patch("backend.app.routers.dashboard._collect_funnel_redis", return_value=dict(_EMPTY_FUNNEL)):
            r = client.get("/api/dashboard/summary")

        assert r.status_code == 200, r.text
        data = r.json()
        bucket_ids = {b["id"] for b in data["top_buckets"]}
        # Vendor-specific buckets must be absent
        assert "top_hosts" not in bucket_ids
        assert "top_rules" not in bucket_ids
        assert "top_mitre" not in bucket_ids
        assert "top_agent_groups" not in bucket_ids
        assert "alerts_by_severity" not in bucket_ids

    def test_v1_accept_header_still_returns_v2(self, client_factory):
        """O ramo v1 foi REMOVIDO — Accept vendorizado não muda o shape."""
        client = client_factory()
        _bootstrap_admin(client)

        with patch("backend.app.routers.dashboard._collect_funnel_db", return_value=dict(_EMPTY_FUNNEL_DB)), \
             patch("backend.app.routers.dashboard._collect_funnel_redis", return_value=dict(_EMPTY_FUNNEL)):
            r = client.get(
                "/api/dashboard/summary",
                headers={"Accept": "application/vnd.centralops.v1+json"},
            )

        assert r.status_code == 200, r.text
        data = r.json()
        assert data["schema_version"] == 2
        assert "alerts" not in data
        assert "X-API-Deprecation" not in r.headers

    def test_v2_contains_scope_sections(self, client_factory):
        """organizations/integrations (contagens + health + inactive) no v2."""
        client = client_factory()
        _bootstrap_admin(client)

        with patch("backend.app.routers.dashboard._collect_funnel_db", return_value=dict(_EMPTY_FUNNEL_DB)), \
             patch("backend.app.routers.dashboard._collect_funnel_redis", return_value=dict(_EMPTY_FUNNEL)):
            r = client.get("/api/dashboard/summary")

        assert r.status_code == 200, r.text
        data = r.json()
        assert {"total", "active"} <= set(data["organizations"].keys())
        ints = data["integrations"]
        assert {"total", "active", "authenticated", "by_platform", "health", "degraded_items", "comparison"} <= set(ints.keys())
        # Shape inclui inactive + unknown explícito (sem subtração frágil)
        assert {"healthy", "degraded", "error", "unknown", "inactive"} <= set(ints["health"].keys())

    def test_requires_auth(self, client_factory):
        client = client_factory()
        r = client.get("/api/dashboard/summary")
        assert r.status_code in (401, 403)


# ── _collect_integration_health unit tests ────────────────────────────────────


def _mock_integration(
    id: int,
    *,
    platform: str = "wazuh",
    is_active: bool = True,
    name: str | None = None,
    organization_id: int = 1,
) -> MagicMock:
    intg = MagicMock(spec=["id", "platform", "is_active", "name", "organization_id", "organization", "last_error", "last_checked_at"])
    intg.id = id
    intg.platform = platform
    intg.is_active = is_active
    intg.name = name or f"Integration-{id}"
    intg.organization_id = organization_id
    intg.organization = MagicMock()
    intg.organization.name = "Test Org"
    intg.last_error = None
    intg.last_checked_at = None
    return intg


def _mock_health_check(integration_id: int, status: str) -> MagicMock:
    hc = MagicMock()
    hc.integration_id = integration_id
    hc.status = status
    return hc


def _make_health_repo(
    latest: dict[int, MagicMock] | None = None,
    previous: dict[int, MagicMock] | None = None,
) -> MagicMock:
    repo = MagicMock()
    repo.get_latest_bulk.return_value = latest or {}
    repo.get_latest_before_bulk.return_value = previous or {}
    return repo


class TestCollectIntegrationHealthInactive:
    """Cenário 1 — integrações inativas não contam nos buckets de saúde."""

    def test_health_separates_inactive(self):
        anchor = datetime(2026, 1, 1, 0, 0, 0)

        active_1 = _mock_integration(1, is_active=True)
        active_2 = _mock_integration(2, is_active=True)
        active_3 = _mock_integration(3, is_active=True)
        inactive_4 = _mock_integration(4, is_active=False)
        active_no_check_5 = _mock_integration(5, is_active=True)

        integrations = [active_1, active_2, active_3, inactive_4, active_no_check_5]

        repo = _make_health_repo(
            latest={
                1: _mock_health_check(1, "healthy"),
                2: _mock_health_check(2, "healthy"),
                3: _mock_health_check(3, "healthy"),
                4: _mock_health_check(4, "healthy"),  # inativa com check — não deve contar
                # 5 not present → unknown
            }
        )

        result = _collect_integration_health(integrations, health_repo=repo, comparison_anchor=anchor)

        assert result["healthy_count"] == 3
        assert result["unknown_count"] == 1
        assert result["inactive_count"] == 1
        assert result["degraded_count"] == 0
        assert result["error_count"] == 0
        assert result["degraded_items"] == []

    def test_health_partner_child_without_check_is_unknown(self):
        """Child ativa sem health check → conta em unknown, não em healthy."""
        anchor = datetime(2026, 1, 1, 0, 0, 0)

        parent = _mock_integration(10, is_active=True, name="Partner")
        child = _mock_integration(11, is_active=True, name="Child-Tenant")

        integrations = [parent, child]
        repo = _make_health_repo(
            latest={
                10: _mock_health_check(10, "healthy"),
                # child (11) has no health check
            }
        )

        result = _collect_integration_health(integrations, health_repo=repo, comparison_anchor=anchor)

        assert result["healthy_count"] == 1
        assert result["unknown_count"] == 1
        assert result["inactive_count"] == 0

    def test_empty_integrations_returns_zeros(self):
        anchor = datetime(2026, 1, 1, 0, 0, 0)
        repo = _make_health_repo()

        result = _collect_integration_health([], health_repo=repo, comparison_anchor=anchor)

        assert result["healthy_count"] == 0
        assert result["degraded_count"] == 0
        assert result["error_count"] == 0
        assert result["unknown_count"] == 0
        assert result["inactive_count"] == 0
        assert result["degraded_items"] == []

    @pytest.mark.parametrize(
        "statuses, expected",
        [
            (["healthy", "healthy", "degraded", "error", "unknown"], {"healthy": 2, "degraded": 1, "error": 1, "unknown": 1}),
            (["error", "error", "error"], {"healthy": 0, "degraded": 0, "error": 3, "unknown": 0}),
            (["unknown", "unknown"], {"healthy": 0, "degraded": 0, "error": 0, "unknown": 2}),
        ],
    )
    def test_status_distribution_parametrized(self, statuses: list[str], expected: dict[str, int]):
        anchor = datetime(2026, 1, 1, 0, 0, 0)
        integrations = [_mock_integration(i, is_active=True) for i in range(len(statuses))]
        latest = {i: _mock_health_check(i, s) for i, s in enumerate(statuses)}
        repo = _make_health_repo(latest=latest)

        result = _collect_integration_health(integrations, health_repo=repo, comparison_anchor=anchor)

        assert result["healthy_count"] == expected["healthy"]
        assert result["degraded_count"] == expected["degraded"]
        assert result["error_count"] == expected["error"]
        assert result["unknown_count"] == expected["unknown"]
