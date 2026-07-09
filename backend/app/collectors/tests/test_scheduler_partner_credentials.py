"""Execução de scheduled query VENDOR-NEUTRA.

A convergência matou o carve-out Sophos do scheduler: ``_run_query_for_integration``
não resolve mais credencial/region/tenant nem chama ``XDRQueryService`` — executa o
ponto canônico ``get_provider(integration).run_query()`` gateado por
``integration_query_capability()``. A resolução de credenciais (incl. token-sharing
parent→child do Sophos Partner) é responsabilidade do PROVIDER
(``SophosProvider._credential_holder``), coberta nos testes do provider.

Aqui testamos só o contrato do scheduler:
- fonte com capability → roda via provider.run_query → SearchResult ``finished`` → ``answered``.
- provider.run_query levanta → SearchResult ``failed`` → ``failed`` (não propaga).
- fonte SEM capability de query → skip, sem SearchResult → ``skipped``.
- integração inexistente → skip → ``skipped``.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import logging
from unittest.mock import MagicMock, patch

from backend.app.providers.base import QueryResult

_TASKS_MOD = "backend.app.collectors.scheduler_tasks"


# ── Fakes ──────────────────────────────────────────────────────────────


class _FakeIntegration:
    def __init__(self, *, id: int = 20, name: str = "Tenant", platform: str = "sophos",
                 kind: str = "tenant", organization_id: int | None = 42,
                 parent_integration_id: int | None = None):
        self.id = id
        self.name = name
        self.platform = platform
        self.kind = kind
        self.organization_id = organization_id
        self.parent_integration_id = parent_integration_id


class _FakeScheduledQuery:
    def __init__(self, id: int = 1, query_id: int = 1, client_ids: str = "20"):
        self.id = id
        self.query_id = query_id
        self.client_ids = client_ids
        self.notify_on_results = False


class _FakePredefinedQuery:
    statement = "SELECT * FROM alerts"
    table = "alerts"
    title = "Test Query"


class _FakeSearchResult:
    def __init__(self, id: int = 1):
        self.id = id
        self.status = "submitted"


class _FakeIntegrationRepo:
    def __init__(self, integration: _FakeIntegration | None):
        self._integration = integration

    def get(self, integration_id: int) -> _FakeIntegration | None:
        if self._integration is not None and integration_id == self._integration.id:
            return self._integration
        return None


class _FakeResultsRepo:
    def __init__(self):
        self.results: list[_FakeSearchResult] = []

    def add_run(self, *args, **kwargs) -> _FakeSearchResult:
        r = _FakeSearchResult(id=len(self.results) + 1)
        self.results.append(r)
        return r

    def update_result(self, record, status, result_json, *, result_count, error_message):
        record.status = status

    def mark_failed(self, record, message: str) -> None:
        record.status = "failed"


class _FakeHistory:
    def add_entry(self, *args, **kwargs) -> None:
        pass


class _FakeQC:
    dialect = "xdr_data_lake"
    ocsf_mapping_version = "1"


def _provider(items=None, raise_exc=None):
    class _P:
        def run_query(self, statement, from_ts, to_ts, **kw):
            if raise_exc:
                raise raise_exc
            return QueryResult(items=list(items or [{"x": 1}]), total=len(items or [1]))
    return _P()


def _run(integration, *, qc=_FakeQC(), provider=None, repo_override=None):
    sched = _FakeScheduledQuery(client_ids=str(integration.id) if integration else "20")
    results_repo = _FakeResultsRepo()
    integ_repo = repo_override or _FakeIntegrationRepo(integration)
    with (
        patch(f"{_TASKS_MOD}.integration_query_capability", return_value=qc),
        patch(f"{_TASKS_MOD}.get_provider", return_value=provider or _provider()),
        patch(f"{_TASKS_MOD}.send_email"),
        patch(f"{_TASKS_MOD}._dispatch_scheduled_query_alert"),
    ):
        from backend.app.collectors.scheduler_tasks import _run_query_for_integration
        status = _run_query_for_integration(
            db=MagicMock(),
            integration_id=integration.id if integration else 20,
            sched=sched, query_def=_FakePredefinedQuery(),
            from_ts="2026-01-01T00:00:00Z", to_ts="2026-01-02T00:00:00Z",
            email_repo=MagicMock(), notify_on_results=False, actor_user_id=None,
            integration_repo=integ_repo, results_repo=results_repo, history=_FakeHistory(),
        )
    return status, results_repo


# ── Tests ──────────────────────────────────────────────────────────────


def test_runs_via_provider_and_creates_search_result():
    """Child Partner (ou qualquer fonte com capability) roda via provider.run_query.
    O token-sharing parent→child é resolvido DENTRO do SophosProvider."""
    child = _FakeIntegration(id=20, kind="tenant", parent_integration_id=10)
    status, results_repo = _run(child, provider=_provider(items=[{"a": 1}]))
    assert status == "answered"
    assert len(results_repo.results) == 1
    assert results_repo.results[0].status == "finished"


def test_provider_failure_marks_failed():
    child = _FakeIntegration(id=20, kind="tenant", parent_integration_id=10)
    status, results_repo = _run(child, provider=_provider(raise_exc=RuntimeError("creds do parent não resolvíveis")))
    assert status == "failed"
    assert len(results_repo.results) == 1
    assert results_repo.results[0].status == "failed"


def test_skipped_when_no_query_capability(caplog):
    integ = _FakeIntegration(id=20, platform="ninjaone")
    with caplog.at_level(logging.WARNING):
        status, results_repo = _run(integ, qc=None)
    assert status == "skipped"
    assert len(results_repo.results) == 0
    assert any("capability de query" in r.message for r in caplog.records)


def test_skipped_when_integration_missing(caplog):
    with caplog.at_level(logging.WARNING):
        status, results_repo = _run(None, repo_override=_FakeIntegrationRepo(None))
    assert status == "skipped"
    assert len(results_repo.results) == 0


def test_standalone_tenant_still_works():
    """Regressão: standalone (sem parent) roda igual — a distinção é do provider."""
    standalone = _FakeIntegration(id=30, kind="tenant", parent_integration_id=None)
    status, results_repo = _run(standalone)
    assert status == "answered"
    assert results_repo.results[0].status == "finished"
