"""MCP tools for in-stream enrichment (ADR-LOCAL-0002).

Backend source of truth: backend/app/routers/enrichment.py
(APIRouter prefix="/collectors/enrichment", mounted under /api). Every endpoint
requires an ADMIN token and is organization-scoped: an org-scoped admin sees
and edits only their subtree; out-of-scope ids return 404 (anti-enumeration).

What an agent can do here, and what it deliberately cannot:

- READ everything that answers "is enrichment working for this tenant, and
  why not?": catalog, sources (never the credential — the API only says
  ``secret_configured``), tables (metadata, not rows), policies with their
  current rules and history, readiness, per-rule metrics, provider activity.
- CHANGE policies: create, publish a version (gated by an ack_token from
  ``dry_run_enrichment``, because publishing REPLACES every rule), enable,
  roll back, and the parent-template flow (mark / sync / apply to children).
- CHANGE a source's SHARING (which child organizations use it, whole subtree,
  enabled) — never its credential or endpoint. Credentials do not travel
  through an agent conversation; create and rotate them in the UI.
"""

from __future__ import annotations

from typing import Any

from ..ack_cache import AckCache, _fingerprint
from ._base import CentralOpsClient, ToolSpec, _integer, _object, _string

_P = "/collectors/enrichment"

_ADMIN_NOTE = (
    "Requires an admin token; returns 403 otherwise. Organization-scoped: ids "
    "outside the caller's scope return 404."
)

#: Namespace of the ack token, so a token minted by dry_run_mapping for an id
#: can never confirm an enrichment commit (and vice versa).
_ACK_NS = "enrichment-policy:"


def _by_org(rows: Any, organization_id: int | None) -> Any:
    """Client-side org filter: the list endpoints return the caller's whole
    visible scope, which for a global admin is every tenant."""
    if organization_id is None or not isinstance(rows, list):
        return rows
    return [r for r in rows if isinstance(r, dict) and r.get("organization_id") == organization_id]


# ── read ────────────────────────────────────────────────────────────────────


async def _list_enrichers(client: CentralOpsClient) -> Any:
    return await client.get(f"{_P}/enrichers")


async def _list_sources(client: CentralOpsClient, *, organization_id: int | None = None) -> Any:
    return _by_org(await client.get(f"{_P}/sources"), organization_id)


async def _list_tables(client: CentralOpsClient, *, organization_id: int | None = None) -> Any:
    return _by_org(await client.get(f"{_P}/tables"), organization_id)


async def _list_policies(client: CentralOpsClient, *, organization_id: int | None = None) -> Any:
    return _by_org(await client.get(f"{_P}/policies"), organization_id)


async def _get_policy(client: CentralOpsClient, *, policy_id: str) -> Any:
    """Policy + current rules + version history in one call.

    There is no ``GET /policies/{id}``; the editor composes the same view from
    the list and the current version.
    """
    policies = await client.get(f"{_P}/policies")
    policy = next(
        (p for p in policies or [] if isinstance(p, dict) and p.get("id") == policy_id),
        None,
    )
    if policy is None:
        return {"error": f"policy {policy_id!r} not found in the caller's scope"}
    versions = await client.get(f"{_P}/policies/{policy_id}/versions")
    current = None
    if policy.get("current_version_id"):
        current = await client.get(
            f"{_P}/policies/{policy_id}/versions/{policy['current_version_id']}"
        )
    return {"policy": policy, "current_version": current, "versions": versions}


async def _get_readiness(client: CentralOpsClient, *, organization_id: int | None = None) -> Any:
    return await client.get(f"{_P}/readiness", params={"organization_id": organization_id})


async def _get_metrics(
    client: CentralOpsClient,
    *,
    organization_id: int | None = None,
    range_minutes: int | None = None,
) -> Any:
    return await client.get(
        f"{_P}/metrics",
        params={"organization_id": organization_id, "range_minutes": range_minutes},
    )


async def _list_activity(
    client: CentralOpsClient,
    *,
    organization_id: int | None = None,
    limit: int | None = None,
    kind: str | None = None,
    only_failures: bool | None = None,
) -> Any:
    return await client.get(
        f"{_P}/activity",
        params={
            "organization_id": organization_id,
            "limit": limit,
            "kind": kind,
            "only_failures": only_failures,
        },
    )


async def _template_preflight(client: CentralOpsClient, *, policy_id: str) -> Any:
    return await client.post(f"{_P}/policies/{policy_id}/template-preflight")


def _make_dry_run(ack_cache: AckCache):
    async def _dry_run_enrichment(
        client: CentralOpsClient,
        *,
        rules: Any,
        sample: dict[str, Any] | None = None,
        tables: dict[str, Any] | None = None,
        policy_id: str | None = None,
    ) -> Any:
        result = await client.post(
            f"{_P}/dry-run",
            json={
                "rules": rules,
                "sample": sample if sample is not None else {"normalized": {}},
                "tables": tables or {},
            },
        )
        ack_token = None
        if policy_id:
            ack_token = await ack_cache.issue(_ACK_NS + policy_id, rules)
        return {
            "rules_fingerprint": _fingerprint(rules),
            "dry_run": result,
            "ack_token": ack_token,
            "ack_token_note": (
                "Pass this ack_token to commit_enrichment_policy with the same "
                "policy_id and rules within 5 minutes."
                if ack_token
                else "No ack_token: pass `policy_id` to be able to publish these rules."
            ),
        }

    return _dry_run_enrichment


# ── write ───────────────────────────────────────────────────────────────────


async def _create_policy(
    client: CentralOpsClient,
    *,
    name: str,
    organization_id: int | None = None,
    description: str | None = None,
) -> Any:
    return await client.post(
        f"{_P}/policies",
        json={"name": name, "organization_id": organization_id, "description": description},
    )


def _make_commit(ack_cache: AckCache):
    async def _commit_enrichment_policy(
        client: CentralOpsClient,
        *,
        policy_id: str,
        rules: Any,
        commit_message: str,
        ack_token: str,
    ) -> Any:
        await ack_cache.consume(ack_token, _ACK_NS + policy_id, rules)
        return await client.post(
            f"{_P}/policies/{policy_id}/versions",
            json={"rules": rules, "commit_message": commit_message},
        )

    return _commit_enrichment_policy


async def _set_enabled(client: CentralOpsClient, *, policy_id: str, enabled: bool) -> Any:
    return await client.post(f"{_P}/policies/{policy_id}/enable", params={"enabled": enabled})


async def _rollback(client: CentralOpsClient, *, policy_id: str, version_id: str) -> Any:
    return await client.post(
        f"{_P}/policies/{policy_id}/rollback", json={"version_id": version_id}
    )


async def _set_template(
    client: CentralOpsClient,
    *,
    policy_id: str,
    is_template: bool,
    sync: bool | None = None,
    enable_children: bool | None = None,
) -> Any:
    return await client.post(
        f"{_P}/policies/{policy_id}/template",
        params={"is_template": is_template, "sync": sync, "enable_children": enable_children},
    )


async def _apply_template(
    client: CentralOpsClient,
    *,
    policy_id: str,
    organization_ids: list[int],
    enable: bool | None = None,
    commit_message: str | None = None,
) -> Any:
    body: dict[str, Any] = {"organization_ids": organization_ids}
    if enable is not None:
        body["enable"] = enable
    if commit_message:
        body["commit_message"] = commit_message
    return await client.post(f"{_P}/policies/{policy_id}/apply-template", json=body)


async def _update_source_sharing(
    client: CentralOpsClient,
    *,
    source_id: str,
    shared_organization_ids: list[int] | None = None,
    share_with_descendants: bool | None = None,
    enabled: bool | None = None,
    description: str | None = None,
) -> Any:
    # Allow-list explícita: credencial e config NUNCA saem daqui, mesmo que o
    # schema um dia deixe passar um campo a mais.
    body = {
        k: v
        for k, v in {
            "shared_organization_ids": shared_organization_ids,
            "share_with_descendants": share_with_descendants,
            "enabled": enabled,
            "description": description,
        }.items()
        if v is not None
    }
    if not body:
        return {"error": "nothing to change: pass at least one field"}
    return await client.patch(f"{_P}/sources/{source_id}", json=body)


# ── specs ───────────────────────────────────────────────────────────────────

_ORG = _integer(
    "Filter/target organization id. A global admin sees every tenant: pass this "
    "to look at one.",
    minimum=1,
)
_POLICY_ID = _string("Enrichment policy id (uuid).")
_RULES = {
    "description": (
        "Policy document {\"version\": 1, \"enrichment\": [rule, ...]} or the bare "
        "rule list. Each rule: id, enricher, table (local lookups) or source "
        "(remote enrichers, by NAME), key {source: dotted path in the event, kind}, "
        "outputs [{from, target under _centralops.enrichment.*}], optional when, "
        "tags, on_miss, on_multi."
    ),
}


def specs(ack_cache: AckCache) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="list_enrichers",
            description=(
                "Enricher catalog: every kind the platform can run (local tables "
                "such as table_exact/table_cidr, geoip, and remote providers such "
                "as opencti, virustotal, abuseipdb, otx, greynoise), with egress "
                "(whether data leaves the environment), mode (per-event vs batch), "
                "required_secrets and the key kinds each resolves. " + _ADMIN_NOTE
            ),
            input_schema=_object(properties={}),
            handler=_list_enrichers,
        ),
        ToolSpec(
            name="list_enrichment_sources",
            description=(
                "Configured sources: a remote enricher instance (endpoint + "
                "credential) owned by one organization and optionally shared with "
                "child organizations. Fields: id, organization_id (owner), name "
                "(what rules cite in `source`), enricher, enabled, "
                "secret_configured (the credential itself is NEVER returned), "
                "shared_organization_ids, share_with_descendants (whole subtree, "
                "including children created later), last_test_at/_ok/_message "
                "(provider's own error text from the last connection test). "
                + _ADMIN_NOTE
            ),
            input_schema=_object(properties={"organization_id": _ORG}),
            handler=_list_sources,
        ),
        ToolSpec(
            name="list_enrichment_tables",
            description=(
                "Customer lookup tables (CMDB, allowlists, network plans) per "
                "organization: name (what rules cite in `table`), match_mode, "
                "current_version_id (null = never published, rules citing it "
                "enrich nothing), entry_count. Tables are NOT shared across "
                "organizations. Row contents are not returned. " + _ADMIN_NOTE
            ),
            input_schema=_object(properties={"organization_id": _ORG}),
            handler=_list_tables,
        ),
        ToolSpec(
            name="list_enrichment_policies",
            description=(
                "Enrichment policies. ONE policy is applied per organization: "
                "`is_active` says which one the worker actually runs (`enabled` "
                "is what was requested). Also: rule_count, current_version_id, "
                "is_template (parent template), template_sync (children kept in "
                "sync), template_enable_children, derived_from_version_id (set "
                "when this policy's current version came from a parent "
                "template). " + _ADMIN_NOTE
            ),
            input_schema=_object(properties={"organization_id": _ORG}),
            handler=_list_policies,
        ),
        ToolSpec(
            name="get_enrichment_policy",
            description=(
                "One policy with its CURRENT rules and full version history "
                "(append-only; rollback re-points to an old version). Use this "
                "before editing: publishing replaces the whole rule list. "
                + _ADMIN_NOTE
            ),
            input_schema=_object(properties={"policy_id": _POLICY_ID}, required=["policy_id"]),
            handler=_get_policy,
        ),
        ToolSpec(
            name="get_enrichment_readiness",
            description=(
                "\"Is enrichment working here, and what is missing?\" — the "
                "ordered checklist the UI overview shows. Steps (key): subsystem "
                "(master switch), cache_l2 (shared cache batch providers need), "
                "sources (and their last connection test), tables (published "
                "version), policy (one active policy with a version). Each step: "
                "status ok|warning|blocked|not_applicable, blocking, detail, "
                "action; `ready` is true when nothing blocks. Best "
                "first read when a tenant reports that enrichment 'does nothing'. "
                + _ADMIN_NOTE
            ),
            input_schema=_object(properties={"organization_id": _ORG}),
            handler=_get_readiness,
        ),
        ToolSpec(
            name="get_enrichment_metrics",
            description=(
                "Per-rule counters of the ACTIVE policy over a window: hit, miss, "
                "skipped (no answer: credential, network, budget, missing cache), "
                "error. Answers \"does this rule still match?\". Only the policy "
                "the worker applies is reported. " + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "organization_id": _ORG,
                    "range_minutes": _integer("Window (5-180, default 60).", minimum=5, maximum=180),
                }
            ),
            handler=_get_metrics,
        ),
        ToolSpec(
            name="list_enrichment_activity",
            description=(
                "Recent provider calls for an organization (ring buffer, newest "
                "first): the 401/DNS/timeout text a provider returned, table "
                "loads, breaker trips. Answers \"why did it stop?\". " + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "organization_id": _ORG,
                    "limit": _integer("Max entries (1-200, default 100).", minimum=1, maximum=200),
                    "kind": _string("Optional entry kind filter."),
                    "only_failures": {"type": "boolean", "description": "Only failed calls."},
                }
            ),
            handler=_list_activity,
        ),
        ToolSpec(
            name="preflight_enrichment_template",
            description=(
                "For a parent TEMPLATE policy: what would happen in EACH child "
                "organization if it were applied. Per child: status ready | "
                "up_to_date | blocked (missing_tables / missing_sources by name) "
                "| overridden (overriding_policy: the child's own enabled policy "
                "wins), and enabled (whether the child's inherited policy is on). "
                "Changes nothing. Enterprise only (403 in Community). " + _ADMIN_NOTE
            ),
            input_schema=_object(properties={"policy_id": _POLICY_ID}, required=["policy_id"]),
            handler=_template_preflight,
        ),
        ToolSpec(
            name="dry_run_enrichment",
            description=(
                "Compile rules and apply them to a sample event WITHOUT publishing. "
                "Returns summary, the enriched event, hits/misses/skipped/errors "
                "per rule and bytes_added. Only LOCAL rules run; remote ones show "
                "as skipped (no third-party call, no quota spent). `tables` lets "
                "you simulate table contents {rule_id: {key: {field: value}}}. "
                "Pass policy_id to receive the ack_token that "
                "commit_enrichment_policy requires. Persists nothing. " + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "rules": _RULES,
                    "sample": {
                        "type": "object",
                        "description": (
                            "Event envelope {_centralops, normalized, raw}. Omitted "
                            "= empty event (validates compilation only)."
                        ),
                    },
                    "tables": {"type": "object", "description": "Simulated table rows per rule id."},
                    "policy_id": _string("Policy you intend to publish these rules to."),
                },
                required=["rules"],
            ),
            handler=_make_dry_run(ack_cache),
        ),
        ToolSpec(
            name="create_enrichment_policy",
            description=(
                "Create an EMPTY, DISABLED policy in an organization (publish a "
                "version and enable it as separate steps). 422 if the name "
                "already exists there. " + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "name": _string("Policy name, unique per organization."),
                    "organization_id": _ORG,
                    "description": _string("Optional description."),
                },
                required=["name"],
            ),
            handler=_create_policy,
            read_only=False,
            destructive=True,
            idempotent=False,
        ),
        ToolSpec(
            name="commit_enrichment_policy",
            description=(
                "Publish a new policy version. It REPLACES the whole rule list "
                "(no merge) — start from get_enrichment_policy's current rules. "
                "Requires the ack_token from dry_run_enrichment for the same "
                "policy_id and the same rules. 422 if a rule cites a table or a "
                "source the organization cannot see. On a parent template with "
                "sync on, children are re-applied and listed in "
                "template_sync_applied / template_sync_skipped. NOT idempotent. "
                + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "policy_id": _POLICY_ID,
                    "rules": _RULES,
                    "commit_message": _string("Why this change (required)."),
                    "ack_token": _string("Token from dry_run_enrichment."),
                },
                required=["policy_id", "rules", "commit_message", "ack_token"],
            ),
            handler=_make_commit(ack_cache),
            read_only=False,
            destructive=True,
            idempotent=False,
        ),
        ToolSpec(
            name="set_enrichment_policy_enabled",
            description=(
                "Enable or disable a policy. Only ONE policy can be enabled per "
                "organization: enabling a second returns 409 "
                "enrichment.policy_already_active naming the one in force — "
                "disable it first. Enabling a policy with no published version is "
                "422. " + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "policy_id": _POLICY_ID,
                    "enabled": {"type": "boolean", "description": "true = enable."},
                },
                required=["policy_id", "enabled"],
            ),
            handler=_set_enabled,
            read_only=False,
            destructive=True,
        ),
        ToolSpec(
            name="rollback_enrichment_policy",
            description=(
                "Point the policy back at an OLDER version (from "
                "get_enrichment_policy's history). No version is deleted. On a "
                "synced parent template, children follow. " + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "policy_id": _POLICY_ID,
                    "version_id": _string("Version to make current."),
                },
                required=["policy_id", "version_id"],
            ),
            handler=_rollback,
            read_only=False,
            destructive=True,
        ),
        ToolSpec(
            name="set_enrichment_policy_template",
            description=(
                "Mark/unmark a parent policy as TEMPLATE for its child "
                "organizations (Enterprise). sync=true keeps children in sync: "
                "applies now, re-applies on every publish/rollback, and a child "
                "created later receives it. enable_children=true enables the "
                "child's inherited policy (default: children get it disabled). "
                "A child whose OWN policy is enabled is never overwritten. "
                "Unmarking turns sync off without touching what children have. "
                "422 if the organization has no children. " + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "policy_id": _POLICY_ID,
                    "is_template": {"type": "boolean", "description": "Mark as template."},
                    "sync": {"type": "boolean", "description": "Keep children in sync."},
                    "enable_children": {
                        "type": "boolean",
                        "description": "Enable the inherited policy on children when syncing.",
                    },
                },
                required=["policy_id", "is_template"],
            ),
            handler=_set_template,
            read_only=False,
            destructive=True,
        ),
        ToolSpec(
            name="apply_enrichment_template",
            description=(
                "Apply the parent template's current version to the chosen child "
                "organizations now: each gets its own derived version. Re-checked "
                "per child: blocked/overridden/up_to_date children come back in "
                "`skipped` and nothing is written there. enable=true also enables "
                "the child's inherited policy. Safe to repeat (up_to_date children "
                "are skipped). Run preflight_enrichment_template first. "
                + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "policy_id": _POLICY_ID,
                    "organization_ids": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 1},
                        "description": "Child organizations (must be in the template's subtree).",
                    },
                    "enable": {"type": "boolean", "description": "Enable on the children."},
                    "commit_message": _string("Optional commit message for the derived versions."),
                },
                required=["policy_id", "organization_ids"],
            ),
            handler=_apply_template,
            read_only=False,
            destructive=True,
        ),
        ToolSpec(
            name="update_enrichment_source_sharing",
            description=(
                "Change WHO uses a source — never its credential or endpoint "
                "(rotate those in the UI; secrets do not travel through MCP). "
                "shared_organization_ids replaces the explicit child list (the "
                "owner always stays); share_with_descendants=true serves the "
                "whole subtree including children created later (turning it off "
                "keeps current children as an explicit list); enabled pauses the "
                "source. Sharing is Enterprise (403 in Community). " + _ADMIN_NOTE
            ),
            input_schema=_object(
                properties={
                    "source_id": _string("Source id (uuid)."),
                    "shared_organization_ids": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 1},
                        "description": "Child organizations that use the source.",
                    },
                    "share_with_descendants": {
                        "type": "boolean",
                        "description": "Serve the whole subtree, including future children.",
                    },
                    "enabled": {"type": "boolean", "description": "Pause/resume the source."},
                    "description": _string("Free-text description."),
                },
                required=["source_id"],
            ),
            handler=_update_source_sharing,
            read_only=False,
            destructive=True,
        ),
    ]
