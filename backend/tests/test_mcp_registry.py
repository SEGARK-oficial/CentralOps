"""Contrato do registro de ferramentas MCP (portado de ``centralops-mcp``).

An agent decides whether a tool is safe to call from ``ToolAnnotations``. A
write tool that forgets to declare itself is advertised as read-only, which is
exactly the failure mode these tests exist to prevent — so the source of truth
here is the HTTP verb in the handler module, not a hand-maintained list.
"""

from __future__ import annotations

import inspect
import re

import pytest

from backend.app.mcp.ack_cache import AckCache
from backend.app.mcp.instructions import SERVER_INSTRUCTIONS
from backend.app.mcp.registry import build_specs

EXPECTED_TOOLS = {
    # Diagnostic / read-only — see the vendor side
    "list_integrations",
    "get_integration",
    "get_integration_health",
    "get_integration_overview",
    "list_supported_platforms",
    "list_collector_vendors",
    "list_collection_state",
    "get_collector_summary",
    "get_collector_cost_summary",
    "list_drift_fields",
    "list_quarantine",
    "get_quarantine_event",
    "reprocess_quarantine",
    "get_sophos_licenses",
    # Mapping engine
    "list_mappings",
    "get_mapping",
    "get_mapping_version",
    "list_mapping_rule_targets",
    "get_mapping_rules",
    "get_mapping_samples",
    "discover_mapping_fields",
    "diff_mapping_versions",
    "list_mapping_audit",
    "dry_run_mapping",
    "commit_mapping",
    "patch_mapping_rules",
    "commit_mapping_patch",
    # Backfill
    "list_backfill_jobs",
    "request_backfill",
    "get_backfill_job",
    "cancel_backfill_job",
    "backfill_diagnostics",
    "wait_for_backfill_job",
    # Pipeline health
    "get_pipeline_health",
    "get_integration_pipeline_health",
    # Destinations & lineage
    "list_destinations",
    "get_destinations_health",
    "get_destination_health",
    "list_destination_dlq",
    "get_destination_metrics",
    "list_destination_audit",
    "list_destination_lineage",
    "get_event_lineage",
    # Routes
    "list_routes",
    "get_routes_topology",
    "get_routes_flow",
    "get_route_health",
    "get_route_metrics",
    # Detections, dashboard, queries
    "list_detections",
    "get_detection",
    "get_dashboard_summary",
    "list_scheduled_queries",
    "get_scheduled_query_history",
    "list_search_history",
    "get_search_result",
    "list_audit_log",
    "get_query_capabilities",
}

#: Every tool that changes server-side state. ``dry_run_mapping`` is deliberately
#: absent: it POSTs but persists nothing, so it is genuinely read-only.
WRITE_TOOLS = {
    "commit_mapping",
    "commit_mapping_patch",
    "request_backfill",
    "cancel_backfill_job",
    "reprocess_quarantine",
}

#: Writes whose effect is not undone by calling them again.
NON_IDEMPOTENT_TOOLS = {"commit_mapping", "commit_mapping_patch", "request_backfill"}

#: POST but read-only: they compute and stage, they never persist.
READ_ONLY_POSTERS = {"dry_run_mapping", "patch_mapping_rules"}

_MUTATING_CALL = re.compile(r"client\.(post|put|patch|delete)\(")


@pytest.fixture(scope="module")
def specs():
    return build_specs(AckCache())


def test_tool_registry_has_expected_names(specs):
    assert set(specs.keys()) == EXPECTED_TOOLS
    assert len(specs) == 57


def test_every_spec_has_object_schema_with_no_extra_props(specs):
    for name, spec in specs.items():
        assert spec.input_schema["type"] == "object", name
        assert spec.input_schema.get("additionalProperties") is False, name
        assert spec.description, f"{name} must have description"


def test_destructive_tool_requires_ack_token(specs):
    commit = specs["commit_mapping"]
    required = set(commit.input_schema["required"])
    assert {"definition_id", "rules", "commit_message", "ack_token"} <= required


def test_write_tools_are_not_advertised_as_read_only(specs):
    for name in WRITE_TOOLS:
        assert specs[name].read_only is False, (
            f"{name} changes state but is advertised read_only=True — an agent "
            f"would treat it as safe to call while exploring"
        )
        assert specs[name].destructive is True, f"{name} should be destructive"


def test_non_idempotent_tools_are_declared(specs):
    for name in NON_IDEMPOTENT_TOOLS:
        assert specs[name].idempotent is False


def test_everything_else_is_read_only(specs):
    for name, spec in specs.items():
        if name in WRITE_TOOLS:
            continue
        assert spec.read_only is True, f"{name} is not in WRITE_TOOLS but says read_only=False"
        assert spec.destructive is False, name


@pytest.mark.source_only
def test_handlers_that_mutate_are_declared_as_writes(specs):
    """Any handler that POSTs must be either a declared write or a declared
    read-only poster — there is no third category. Reads ``.py`` source, so it
    only runs where the source tree exists (not in the compiled image)."""
    for name, spec in specs.items():
        source = inspect.getsource(inspect.unwrap(spec.handler))
        mutates = bool(_MUTATING_CALL.search(source))
        if mutates:
            assert name in WRITE_TOOLS or name in READ_ONLY_POSTERS, (
                f"{name} calls a mutating HTTP verb but is neither a declared "
                f"write nor a declared read-only poster"
            )
        else:
            assert name not in WRITE_TOOLS, f"{name} is declared a write but never mutates"


@pytest.mark.parametrize("name", sorted(READ_ONLY_POSTERS))
def test_read_only_posters_are_declared_read_only(specs, name):
    assert specs[name].read_only is True


def test_server_instructions_cover_identity_scope_and_writes():
    assert "organization_id" in SERVER_INSTRUCTIONS
    # O embutido age como o analista dono da chave — o modelo precisa saber
    # que um 403 é limite do papel, não falha transitória.
    assert "analyst" in SERVER_INSTRUCTIONS
    assert "403" in SERVER_INSTRUCTIONS
    for name in WRITE_TOOLS:
        assert name in SERVER_INSTRUCTIONS, f"instructions must list write tool {name}"


def test_wait_for_backfill_cap_fits_one_http_request(specs):
    """Embutido, uma tool call é UMA requisição HTTP aberta; o teto de espera
    não pode ultrapassar o que um proxy reverso tolera."""
    schema = specs["wait_for_backfill_job"].input_schema
    assert schema["properties"]["timeout_s"]["maximum"] <= 120
