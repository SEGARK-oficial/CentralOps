"""Bug B: scheduled query com resultados dispara alerta syslog (Critical, PRI=130).

O alerta agendado NÃO vai mais direto a um
``dispatch_to_wazuh`` dedicado — é ROTEADO via ``pipeline._enqueue_dispatch`` e
entregue como qualquer destino (incl. wazuh-default, uma Destination syslog comum).

Cobre:
- items presentes → _dispatch_scheduled_query_alert chamado 1x com envelope correto.
- items ausentes → _dispatch_scheduled_query_alert NÃO chamado.
- _dispatch_scheduled_query_alert levanta → erro não propaga, SearchResult preservado.
- customer_id None (org sem id) → envelope criado, pipeline trata quarentena.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# env de teste tem APP_MASTER_KEY -> encrypt/decrypt reais funcionam.
from backend.app.core.crypto import encrypt as _real_encrypt

_TASKS_MOD = "backend.app.collectors.scheduler_tasks"


class _FakeCred:
    """Espelha integration_credentials.IntegrationCredential o suficiente para
    read_secret/has_secret (logical_name/secret_ref/revoked_at)."""

    def __init__(self, logical_name: str, plaintext: str):
        self.logical_name = logical_name
        self.secret_ref = _real_encrypt(plaintext)
        self.revoked_at = None


# ── Fake objects (alinhados ao padrão de test_scheduler_partner_credentials.py) ──


class _FakeOrg:
    # customer_id do envelope = Organization.id interno (não mais IRIS).
    def __init__(self, *, org_id: int | None = 42, name: str = "Acme Corp"):
        self.id = org_id
        self.name = name


class _FakeIntegration:
    _id_counter = 0

    def __init__(
        self,
        *,
        name: str = "test",
        platform: str = "sophos",
        kind: str = "tenant",
        is_active: bool = True,
        tenant_id: str | None = "tenant-xyz",
        region: str | None = "us03",
        access_token: str | None = None,
        refresh_token: str | None = None,
        client_id: str | None = "cid",
        client_secret: str | None = "enc::sec",
        parent_integration_id: int | None = None,
        api_host: str | None = None,
        org_id: int | None = 42,
        org_name: str = "Acme Corp",
        id: int | None = None,
    ):
        _FakeIntegration._id_counter += 1
        self.id = id if id is not None else _FakeIntegration._id_counter
        self.name = name
        self.platform = platform
        self.kind = kind
        self.is_active = is_active
        self.tenant_id = tenant_id
        self.region = region
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.parent_integration_id = parent_integration_id
        self.api_host = api_host
        self.organization_id = org_id  # lido p/ add_run + escopo de e-mail
        self.organization = _FakeOrg(org_id=org_id, name=org_name)
        # secrets vivem no store integration_credentials, lidos via
        # integration_secrets (has_secret/read_secret) e não mais nas colunas.
        # Constrói .credentials a partir dos campos-segredo setados; aceita o
        # estilo legado "enc::X" (descasca o prefixo, guardando o plaintext).
        self.credentials = []
        for logical_name, value in (
            ("client_secret", client_secret),
            ("access_token", access_token),
            ("refresh_token", refresh_token),
        ):
            plain = value[5:] if (value and value.startswith("enc::")) else value
            if plain is not None:
                self.credentials.append(_FakeCred(logical_name, plain))


class _FakeScheduledQuery:
    def __init__(
        self,
        id: int = 1,
        query_id: int = 1,
        client_ids: str = "1",
        notify_on_results: bool = False,
    ):
        self.id = id
        self.query_id = query_id
        self.client_ids = client_ids
        self.notify_on_results = notify_on_results


class _FakePredefinedQuery:
    def __init__(self, id: int = 99, title: str = "Test Query"):
        self.id = id
        self.statement = "SELECT * FROM alerts"
        self.table = "alerts"
        self.title = title


class _FakeSearchResult:
    def __init__(self, id: int = 7):
        self.id = id
        self.status = "submitted"


class _FakeIntegrationRepo:
    def __init__(
        self,
        integration: _FakeIntegration,
        credential_source: _FakeIntegration | None = None,
    ):
        self._integration = integration
        self._credential_source = credential_source or integration

    def get(self, integration_id: int) -> _FakeIntegration | None:
        if integration_id == self._integration.id:
            return self._integration
        return None

    def get_credential_source(self, integration: _FakeIntegration) -> _FakeIntegration | None:
        return self._credential_source


class _FakeResultsRepo:
    def __init__(self, fetch_result: dict | None = None):
        self._fetch_result = fetch_result or {"items": [{"x": 1}, {"y": 2}]}
        self.results: list[_FakeSearchResult] = []

    def add_run(self, *args, **kwargs) -> _FakeSearchResult:
        r = _FakeSearchResult(id=len(self.results) + 7)
        self.results.append(r)
        return r

    def update_result(self, record, status, result_json, *, result_count, error_message):
        record.status = status

    def mark_failed(self, record, message: str) -> None:
        record.status = "failed"


class _FakeHistory:
    def add_entry(self, *args, **kwargs) -> None:
        pass


class _FakeQueryCapability:
    """Espelha o QueryCapability o suficiente p/ o scheduler (dialect + versão OCSF)."""

    dialect = "xdr_data_lake"
    ocsf_mapping_version = "1"


def _make_provider(items: list | None = None):
    """Provider fake cujo run_query devolve um QueryResult com ``items`` (a execução
    agora passa por get_provider().run_query(), não XDRQueryService)."""
    from backend.app.providers.base import QueryResult

    _items = items if items is not None else [{"x": 1}, {"y": 2}]

    class _FakeProvider:
        def run_query(self, statement, from_ts, to_ts, **kwargs):
            return QueryResult(items=list(_items), total=len(_items))

    return _FakeProvider()


def _run_query(
    integration: _FakeIntegration,
    sched: _FakeScheduledQuery,
    query_def: _FakePredefinedQuery,
    results_repo: _FakeResultsRepo,
    *,
    items: list | None = None,
    mock_dispatch: MagicMock | None = None,
    emails: list[str] | None = None,
) -> None:
    """Helper que chama _run_query_for_integration com os mocks vendor-neutros."""
    from contextlib import ExitStack

    integration_repo = _FakeIntegrationRepo(integration)
    history = _FakeHistory()

    with ExitStack() as stack:
        # execução canônica via get_provider().run_query(), gateada
        # por integration_query_capability() — sem TokenManager/XDRQueryService aqui.
        stack.enter_context(
            patch(f"{_TASKS_MOD}.get_provider", return_value=_make_provider(items))
        )
        stack.enter_context(
            patch(
                f"{_TASKS_MOD}.integration_query_capability",
                return_value=_FakeQueryCapability(),
            )
        )
        stack.enter_context(patch(f"{_TASKS_MOD}.send_email"))
        if mock_dispatch is not None:
            stack.enter_context(
                patch(f"{_TASKS_MOD}._dispatch_scheduled_query_alert", mock_dispatch)
            )

        from backend.app.collectors.scheduler_tasks import _run_query_for_integration

        # _run_query_for_integration resolve os
        # destinatários POR ORG via email_repo.list_for_org(...). Fake devolve a
        # lista configurada (independe da org, como o escopo já existisse).
        fake_email_repo = MagicMock()
        fake_email_repo.list_for_org.return_value = [
            SimpleNamespace(email=e) for e in (emails or [])
        ]

        _run_query_for_integration(
            db=MagicMock(),
            integration_id=integration.id,
            sched=sched,
            query_def=query_def,
            from_ts="2026-01-01T00:00:00Z",
            to_ts="2026-01-02T00:00:00Z",
            email_repo=fake_email_repo,
            notify_on_results=bool(emails),
            actor_user_id=None,
            integration_repo=integration_repo,
            results_repo=results_repo,
            history=history,
        )


# ── Tests ─────────────────────────────────────────────────────────────


class TestScheduledQueryAlertDispatched:
    """Bug B — itens presentes → alerta syslog Critical disparado."""

    def test_dispatch_called_once_when_items_present(self) -> None:
        integration = _FakeIntegration(id=50)
        sched = _FakeScheduledQuery(id=1, client_ids=str(integration.id))
        query_def = _FakePredefinedQuery()
        results_repo = _FakeResultsRepo()
        mock_dispatch = MagicMock()

        _run_query(integration, sched, query_def, results_repo, mock_dispatch=mock_dispatch)

        mock_dispatch.assert_called_once()

    def test_dispatch_receives_correct_args(self) -> None:
        integration = _FakeIntegration(id=51)
        sched = _FakeScheduledQuery(id=2, client_ids=str(integration.id))
        query_def = _FakePredefinedQuery(id=77, title="My Alert Query")
        results_repo = _FakeResultsRepo()
        mock_dispatch = MagicMock()

        _run_query(integration, sched, query_def, results_repo, mock_dispatch=mock_dispatch)

        call_kwargs = mock_dispatch.call_args.kwargs
        assert call_kwargs["integration"] is integration
        assert call_kwargs["sched"] is sched
        assert call_kwargs["query_def"] is query_def
        assert len(call_kwargs["items"]) == 2
        assert call_kwargs["from_ts"] == "2026-01-01T00:00:00Z"
        assert call_kwargs["to_ts"] == "2026-01-02T00:00:00Z"

    def test_search_result_created_and_finished(self) -> None:
        integration = _FakeIntegration(id=52)
        sched = _FakeScheduledQuery(id=3, client_ids=str(integration.id))
        query_def = _FakePredefinedQuery()
        results_repo = _FakeResultsRepo()
        mock_dispatch = MagicMock()

        _run_query(integration, sched, query_def, results_repo, mock_dispatch=mock_dispatch)

        assert len(results_repo.results) == 1
        assert results_repo.results[0].status == "finished"


class TestScheduledQueryNoAlertWhenEmpty:
    """Bug B — items vazios → dispatch NÃO chamado."""

    def test_no_dispatch_when_empty_items(self) -> None:
        integration = _FakeIntegration(id=60)
        sched = _FakeScheduledQuery(id=10, client_ids=str(integration.id))
        query_def = _FakePredefinedQuery()
        results_repo = _FakeResultsRepo()
        mock_dispatch = MagicMock()

        _run_query(
            integration, sched, query_def, results_repo,
            items=[],
            mock_dispatch=mock_dispatch,
        )

        mock_dispatch.assert_not_called()

    def test_search_result_created_even_when_empty(self) -> None:
        integration = _FakeIntegration(id=61)
        sched = _FakeScheduledQuery(id=11, client_ids=str(integration.id))
        query_def = _FakePredefinedQuery()
        results_repo = _FakeResultsRepo()

        _run_query(integration, sched, query_def, results_repo, items=[], mock_dispatch=MagicMock())

        assert len(results_repo.results) == 1
        assert results_repo.results[0].status == "finished"


class TestScheduledQueryAlertFailureSafe:
    """Bug B — falha no dispatch não quebra o fluxo principal."""

    def test_dispatch_failure_does_not_propagate(self) -> None:
        integration = _FakeIntegration(id=70)
        sched = _FakeScheduledQuery(id=20, client_ids=str(integration.id))
        query_def = _FakePredefinedQuery()
        results_repo = _FakeResultsRepo()
        failing_dispatch = MagicMock(side_effect=RuntimeError("Celery down"))

        # Não deve levantar — _dispatch_scheduled_query_alert é best-effort.
        _run_query(integration, sched, query_def, results_repo, mock_dispatch=failing_dispatch)

        # SearchResult ainda foi criado e marcado como finished.
        assert len(results_repo.results) == 1
        assert results_repo.results[0].status == "finished"

    def test_dispatch_failure_logged(self, caplog) -> None:
        import logging

        integration = _FakeIntegration(id=71)
        sched = _FakeScheduledQuery(id=21, client_ids=str(integration.id))
        query_def = _FakePredefinedQuery()
        results_repo = _FakeResultsRepo()
        failing_dispatch = MagicMock(side_effect=ValueError("unexpected"))

        with caplog.at_level(logging.ERROR):
            _run_query(integration, sched, query_def, results_repo, mock_dispatch=failing_dispatch)

        assert any("syslog" in r.message.lower() or "alerta" in r.message.lower() for r in caplog.records)


class TestDispatchScheduledQueryAlertEnvelope:
    """Testa _dispatch_scheduled_query_alert diretamente — envelope correto."""

    def test_envelope_has_correct_stream_and_severity(self) -> None:
        integration = _FakeIntegration(id=80, org_id=42)
        sched = _FakeScheduledQuery(id=5)
        query_def = _FakePredefinedQuery(id=99, title="XDR Threat Hunt")
        record = _FakeSearchResult(id=7)

        captured_envelopes: list[dict] = []

        def fake_enqueue(batch, *a, **k):
            captured_envelopes.extend(batch)

        mock_enqueue = MagicMock(side_effect=fake_enqueue)

        # o alerta agendado é ROTEADO via _enqueue_dispatch
        # (não mais direto ao dispatch_to_wazuh). Mockamos a chamada real do fluxo
        # e inspecionamos o envelope passado a ela — robusto à via de entrega.
        with patch(
            "backend.app.collectors.pipeline._enqueue_dispatch",
            mock_enqueue,
        ):
            # Import after env vars are set
            from backend.app.collectors.scheduler_tasks import _dispatch_scheduled_query_alert

            _dispatch_scheduled_query_alert(
                integration=integration,  # type: ignore[arg-type]
                sched=sched,  # type: ignore[arg-type]
                query_def=query_def,  # type: ignore[arg-type]
                items=[{"a": 1}, {"b": 2}],
                from_ts="2026-01-01T00:00:00Z",
                to_ts="2026-01-02T00:00:00Z",
                record=record,  # type: ignore[arg-type]
            )

        assert len(captured_envelopes) == 1
        envelope = captured_envelopes[0]

        meta = envelope["_centralops"]
        assert meta["stream"] == "scheduled_query"
        assert meta["event_type"] == "centralops.scheduled_query.match"
        assert meta["vendor"] == "centralops"
        assert meta["customer_id"] == 42
        assert meta["integration_id"] == 80

        # severity_id Critical = 5
        assert envelope["normalized"]["severity_id"] == 5

    def test_envelope_vendor_msg_id_includes_sched_and_result_ids(self) -> None:
        integration = _FakeIntegration(id=81, org_id=10)
        sched = _FakeScheduledQuery(id=5)
        query_def = _FakePredefinedQuery()
        record = _FakeSearchResult(id=7)

        captured_envelopes: list[dict] = []

        def fake_enqueue(batch, *a, **k):
            captured_envelopes.extend(batch)

        mock_enqueue = MagicMock(side_effect=fake_enqueue)

        # o alerta agendado é ROTEADO via _enqueue_dispatch
        # (não mais direto ao dispatch_to_wazuh). Mockamos a chamada real do fluxo
        # e inspecionamos o envelope passado a ela — robusto à via de entrega.
        with patch(
            "backend.app.collectors.pipeline._enqueue_dispatch",
            mock_enqueue,
        ):
            from backend.app.collectors.scheduler_tasks import _dispatch_scheduled_query_alert

            _dispatch_scheduled_query_alert(
                integration=integration,  # type: ignore[arg-type]
                sched=sched,  # type: ignore[arg-type]
                query_def=query_def,  # type: ignore[arg-type]
                items=[{"x": 1}],
                from_ts="2026-01-01T00:00:00Z",
                to_ts="2026-01-02T00:00:00Z",
                record=record,  # type: ignore[arg-type]
            )

        # event_id deve conter os IDs do schedule e do result
        event_id = captured_envelopes[0]["_centralops"]["event_id"]
        assert "sched-5" in event_id
        assert "7" in event_id

    def test_items_capped_at_50_in_raw(self) -> None:
        integration = _FakeIntegration(id=82, org_id=10)
        sched = _FakeScheduledQuery(id=6)
        query_def = _FakePredefinedQuery()
        record = _FakeSearchResult(id=8)
        big_items = [{"i": i} for i in range(100)]

        captured_envelopes: list[dict] = []

        def fake_enqueue(batch, *a, **k):
            captured_envelopes.extend(batch)

        mock_enqueue = MagicMock(side_effect=fake_enqueue)

        # o alerta agendado é ROTEADO via _enqueue_dispatch
        # (não mais direto ao dispatch_to_wazuh). Mockamos a chamada real do fluxo
        # e inspecionamos o envelope passado a ela — robusto à via de entrega.
        with patch(
            "backend.app.collectors.pipeline._enqueue_dispatch",
            mock_enqueue,
        ):
            from backend.app.collectors.scheduler_tasks import _dispatch_scheduled_query_alert

            _dispatch_scheduled_query_alert(
                integration=integration,  # type: ignore[arg-type]
                sched=sched,  # type: ignore[arg-type]
                query_def=query_def,  # type: ignore[arg-type]
                items=big_items,
                from_ts="2026-01-01T00:00:00Z",
                to_ts="2026-01-02T00:00:00Z",
                record=record,  # type: ignore[arg-type]
            )

        raw = captured_envelopes[0]["raw"]
        assert len(raw["items"]) == 50
        assert raw["items_truncated"] is True

    def test_customer_id_none_when_org_missing(self) -> None:
        """Org sem id → customer_id=None no envelope (quarentena)."""
        integration = _FakeIntegration(id=83, org_id=None)
        sched = _FakeScheduledQuery(id=7)
        query_def = _FakePredefinedQuery()
        record = _FakeSearchResult(id=9)

        captured_envelopes: list[dict] = []

        def fake_enqueue(batch, *a, **k):
            captured_envelopes.extend(batch)

        mock_enqueue = MagicMock(side_effect=fake_enqueue)

        # o alerta agendado é ROTEADO via _enqueue_dispatch
        # (não mais direto ao dispatch_to_wazuh). Mockamos a chamada real do fluxo
        # e inspecionamos o envelope passado a ela — robusto à via de entrega.
        with patch(
            "backend.app.collectors.pipeline._enqueue_dispatch",
            mock_enqueue,
        ):
            from backend.app.collectors.scheduler_tasks import _dispatch_scheduled_query_alert

            _dispatch_scheduled_query_alert(
                integration=integration,  # type: ignore[arg-type]
                sched=sched,  # type: ignore[arg-type]
                query_def=query_def,  # type: ignore[arg-type]
                items=[{"x": 1}],
                from_ts="2026-01-01T00:00:00Z",
                to_ts="2026-01-02T00:00:00Z",
                record=record,  # type: ignore[arg-type]
            )

        meta = captured_envelopes[0]["_centralops"]
        assert meta["customer_id"] is None

    def test_dispatch_routes_via_enqueue_dispatch(self) -> None:
        """O alerta agendado é ROTEADO via
        ``_enqueue_dispatch`` (segue rotas/destino default; sem rota → DLQ), não
        mais direto à fila dedicada ``dispatch.wazuh``."""
        integration = _FakeIntegration(id=84, org_id=5)
        sched = _FakeScheduledQuery(id=8)
        query_def = _FakePredefinedQuery()
        record = _FakeSearchResult(id=10)

        mock_enqueue = MagicMock()

        with patch(
            "backend.app.collectors.pipeline._enqueue_dispatch",
            mock_enqueue,
        ):
            from backend.app.collectors.scheduler_tasks import _dispatch_scheduled_query_alert

            _dispatch_scheduled_query_alert(
                integration=integration,  # type: ignore[arg-type]
                sched=sched,  # type: ignore[arg-type]
                query_def=query_def,  # type: ignore[arg-type]
                items=[{"x": 1}],
                from_ts="2026-01-01T00:00:00Z",
                to_ts="2026-01-02T00:00:00Z",
                record=record,  # type: ignore[arg-type]
            )

        mock_enqueue.assert_called_once()
        batch = mock_enqueue.call_args.args[0]
        assert len(batch) == 1
        assert batch[0]["_centralops"]["stream"] == "scheduled_query"
