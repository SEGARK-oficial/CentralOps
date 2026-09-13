"""Instruções do servidor MCP — o texto que o cliente lê no ``initialize``.

Carrega as regras transversais que nenhuma descrição de ferramenta consegue
carregar sozinha: orientação por pergunta, semântica de escopo, quais
ferramentas escrevem e as duas armadilhas que produzem respostas confiantes e
erradas. Um teste garante que toda ferramenta de escrita esteja citada aqui.
"""

from __future__ import annotations

SERVER_INSTRUCTIONS = """\
CentralOps is a security data pipeline: collectors pull events from vendors
(Sophos, Wazuh, CrowdStrike, Defender, Okta, Entra ID, FortiGate, Veeam, ...),
a declarative mapping engine normalizes them to OCSF 1.8, and routes dispatch
them to destinations (syslog, Splunk, Elastic, ClickHouse, Security Lake, ...).

## Identity: you act as the analyst whose API key authenticated this session

Every tool call runs with that analyst's own role, permissions and
organization scope, and is recorded in the audit trail under their name. The
MCP server grants nothing beyond what the same key could do through the REST
API: a 403 from a tool means the analyst's role (or the key's restricted
scopes) does not include that action — do not retry, tell the user.

## Orientation: which tool answers which question

- "What is connected / is it healthy?" -> list_integrations, get_integration_health,
  get_pipeline_health, list_collection_state.
- "What does this vendor actually send?" -> get_mapping_samples (raw vendor JSON,
  pre-normalization). This is the ground truth for authoring rules.
- "What fields are we ignoring?" -> list_drift_fields, discover_mapping_fields.
- "How is this vendor normalized?" -> list_mappings, then get_mapping.
- "Did normalization fail?" -> list_quarantine, get_quarantine_event.
- "Where did this event go?" -> get_event_lineage, list_destination_lineage.
- "Is data being dropped or delayed?" -> get_route_health, list_destination_dlq,
  list_collection_state (collection lag).

## Scope: read this before trusting an empty result

Every call is scoped by the analyst's key. An ORG-SCOPED analyst sees only
their tenant. A GLOBAL-SCOPED analyst sees the control plane but, for
tenant-owned data, is FAIL-CLOSED: it returns an EMPTY result rather than
aggregating across tenants.

This matters most for the sample reservoir. get_mapping_samples and
dry_run_mapping with a global analyst and no `organization_id` return
`sample_size: 0` and NOT an error. Empty here means "you did not name a tenant",
not "there is no data". Pass `organization_id` whenever a reservoir-backed call
comes back empty.

## Writes: 5 of these tools change state, the rest only read

Read-only (safe to explore freely): everything not listed below, including
dry_run_mapping — it is an HTTP POST but persists nothing.

State-changing, and each needs explicit human intent before you call it:
- commit_mapping — promotes a new mapping version; live collectors pick it up in
  ~30s. NOT idempotent: each call creates another version. Requires an ack_token
  minted by dry_run_mapping for the same definition_id AND the same rules.
- commit_mapping_patch — same, for the incremental flow. Requires an ack_token
  from patch_mapping_rules and the same ops.
- request_backfill — enqueues a re-collection job; costs vendor API quota.
- cancel_backfill_job — stops a running job.
- reprocess_quarantine — re-injects a quarantined event into the pipeline.

Never call these to "verify" or "test" something. To test a mapping, use
dry_run_mapping.

## Two traps that produce confident wrong answers

1. dry_run_mapping reports whether RULES EXECUTED, not whether the output is
   valid OCSF. It does not return ocsf_validation_stats or mapped_field_ratio.
   "10/10 passed" means no rule crashed. It does NOT mean the events conform to
   OCSF 1.8. Do not report OCSF conformance based on a dry-run.
2. Mapping definitions seeded from repository defaults diverge from the files on
   disk as soon as anyone edits them in the UI. get_mapping is the only source of
   truth for what production actually applies. Never infer live behavior from the
   JSON files in the repo.

## Editing a mapping: use the incremental flow

Mappings reach 150-193 rules (~30 KB). Never read or resend the whole array to
change one rule — that is what exhausts a context window. Four calls:

1. list_mapping_rule_targets — the index: one line per rule, no bodies.
2. get_mapping_rules — the full body of ONLY the rules you care about, pinned to
   the version_id from step 1.
3. patch_mapping_rules — describe the change as ops (replace/remove/insert/
   append) addressed by absolute index. The merged array is dry-run and staged
   server-side; you get back only what changed, plus a before/after comparison.
4. commit_mapping_patch — same ops + the ack_token. The staged rules are sent
   for you.

`target` is NOT unique: the same target appears many times gated by different
`when` predicates, and order decides the winner. Address rules by `index`, and
pass `expect_target` so a stale index fails loudly instead of editing the wrong
rule.

To measure before changing anything, call dry_run_mapping with ONLY a
definition_id — that runs the rules production is applying right now.

The whole-array flow (dry_run_mapping with `rules` -> commit_mapping) still
works and is the right choice when you are authoring a mapping from scratch.
Both ack_tokens expire in 5 minutes, are single-use, and belong to the analyst
who minted them.
"""

__all__ = ["SERVER_INSTRUCTIONS"]
