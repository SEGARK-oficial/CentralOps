<div align="center">

# CentralOps

**The security data pipeline built for multi-tenant SOCs and MSSPs.**

[![Build](https://img.shields.io/github/actions/workflow/status/SEGARK-oficial/CentralOps/build-and-publish.yml?branch=main)](https://github.com/SEGARK-oficial/CentralOps/actions/workflows/build-and-publish.yml)
[![Release](https://img.shields.io/github/v/release/SEGARK-oficial/CentralOps)](https://github.com/SEGARK-oficial/CentralOps/releases)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-segark--oficial.github.io-informational)](https://segark-oficial.github.io/CentralOps)

</div>

---

CentralOps is an open-source **Security Data Pipeline Platform (SDPP)**. It ingests
telemetry from heterogeneous security vendors, normalizes every event to a canonical
**OCSF** envelope through a declarative, versioned mapping language, and routes the
result to any SIEM or data lake — with per-organization isolation, formal quarantine of
malformed events, and a full audit trail.

Unlike general-purpose pipelines, CentralOps was designed from day one for the **MSSP
workflow**: automatic tenant discovery with human approval, per-customer data isolation,
and operator-facing tooling (web UI + MCP server) built for SOC analysts — not only for
detection engineers.

```text
   Vendor APIs                Normalize (CML → OCSF)            Destinations
 ┌──────────────┐            ┌────────────────────┐          ┌──────────────┐
 │ Sophos / XDR │            │  declarative DSL   │   ┌────▶ │ Wazuh / SIEM │
 │ MS Defender  │  collect   │  versioned +       │   │      ├──────────────┤
 │ Wazuh        │ ─────────▶ │  dry-run + rollback │ ─┼────▶ │ Splunk / S3  │
 │ NinjaOne     │  (Celery)  │  drift detection   │   │      ├──────────────┤
 │ … pluggable  │            │  quarantine        │   └────▶ │ Elastic /    │
 └──────────────┘            └────────────────────┘          │ Sentinel …   │
                                                             └──────────────┘
```

## Why CentralOps

- **Multi-vendor by design** — a registry of providers; adding a vendor is registering a
  `BaseProvider` class. Sophos, Microsoft Defender, Wazuh, and NinjaOne ship out of the box.
- **CML — CentralOps Mapping Language** — a declarative JSON DSL for vendor → OCSF
  normalization: JMESPath extraction, `value_map`, `type_cast`, predicates, OCSF observable
  builders, and `fallback` chains. Versioned, with diff between versions, dry-run against a
  sample reservoir, and append-only rollback.
- **Nothing gets dropped silently** — malformed events land in a formal **quarantine** with
  an `error_kind` and idempotent reprocessing; new vendor fields surface in a **drift**
  triage UI instead of vanishing.
- **OCSF-native envelope** — canonical output carries `_centralops` (metadata + lineage),
  `normalized` (OCSF), and `raw` (preserved payload for audit).
- **Pluggable destinations** — a `_Target` protocol (`send_batch`/`close`) fans out to
  Wazuh today and Splunk HEC, Elastic, S3 JSONL, Sentinel, or Kafka next.
- **Operator-first** — a React UI plus an **MCP server** (typed tools for mappings,
  quarantine, backfill, and drift) for AI-assisted operations.
- **SSO, RBAC, KMS, and PII redaction are free** — base security belongs in the open core,
  not behind a paywall.

## Who it's for

- **MSSPs and MDRs** operating tens to hundreds of customers across heterogeneous vendors.
- **Multi-tenant SOC teams** that need a unified collect → normalize → route flow.
- **Security engineers** who want versioned normalization rules with native diff / dry-run / rollback.
- **Platform teams** that need extensibility — a new vendor is one Python class; a new
  destination is one `_Target` implementation.

## Editions

CentralOps follows an **open-core** model. The Community core in this repository is
complete and production-usable on its own; Enterprise modules that scale with the size and
maturity of an organization live in a separate, commercially licensed package.

| | **Community** (this repo, AGPLv3) | **Enterprise** (commercial) |
|---|---|---|
| Ingestion, CML normalization, routing | ✅ | ✅ |
| Quarantine, drift detection, backfill | ✅ | ✅ |
| **SSO / OIDC (Entra)**, base RBAC, KMS, PII redaction | ✅ | ✅ |
| Base audit trail, MCP server | ✅ | ✅ |
| Hierarchical multi-tenancy & subtree RBAC | — | ✅ |
| Reseller / partner program | — | ✅ |
| Active federated search across sources | — | ✅ |
| Compliance-grade (tamper-evident) audit, HA | — | ✅ |

> The Community artifact **never** ships Enterprise code. Enterprise features are delivered
> as a separate, license-activated image — the open core stays fully open. Commercial
> inquiries: **support@segark.com**.

## Quick start

```bash
git clone https://github.com/SEGARK-oficial/CentralOps.git
cd CentralOps/compose
docker compose up --build
```

Defaults:

- HTTP at `http://localhost:3000`
- HTTPS at `https://localhost:3443` (a self-signed cert is generated if `certs/tls.crt`
  and `certs/tls.key` are absent)

The backend runs behind Nginx; the frontend consumes the API via `/api`.

### Single image

```bash
docker build -f compose/Dockerfile -t centralops:latest .
docker run -p 3000:80 -p 3443:443 \
  -e APP_MASTER_KEY='set-a-key-of-at-least-32-characters' \
  -e ENABLE_HTTPS=1 \
  centralops:latest
```

Prebuilt images are published to `ghcr.io/segark-oficial/centralops`. Config can also be
supplied via `--env-file` or a mounted `/app/.env`.

### Essential configuration

| Variable | Purpose |
|---|---|
| `APP_MASTER_KEY` | Encryption master key. Required in production; auto-generated and persisted to `/app/data/app_master_key` on first boot if unset. |
| `APP_ENV` | `production` disables the OpenAPI docs by default. |
| `APP_COMPANY_NAME` / `APP_COMPANY_PORTAL_NAME` | UI branding and login subtitle. |
| `SESSION_SECURE_COOKIE` | Set `1` when the primary entry point is HTTPS. |
| `ENABLE_HTTPS` / `NGINX_SERVER_NAME` | Enable TLS and set the Nginx `server_name`. |

## Architecture

- **Nginx** terminates HTTP/HTTPS and proxies `/api` to the backend.
- **React** serves the UI and consumes the API.
- **FastAPI** owns authentication, tenancy, auditing, scheduling, queries, and actions.
- **PostgreSQL** (or SQLite in dev) stores app data, versioned mappings, quarantined
  events, and the audit log.
- **Celery Beat + RedBeat** schedule and dispatch collections dynamically (no restart).
- **Redis** backs the task queue, rate limiting, collection cursors, and the dynamic schedule.

## Extending

CentralOps is pluggable on both sides — **input** (vendors) and **output** (destinations).

- **Add a vendor** — register a provider under `backend/app/providers/<vendor>/` and a
  collector under `backend/app/collectors/vendors/<vendor>.py` (`register_provider`,
  `register_collector`). The backend renders UI dynamically from these registries.
- **Add a destination** — implement the `_Target` protocol (`send_batch`/`close`) under
  `backend/app/collectors/output/`. A `_CompositeTarget` lets you multiplex sinks.

Full guides — install, concepts, CML reference and cookbook, integrations, and SRE
runbooks — live in the documentation:

📖 **[segark-oficial.github.io/CentralOps](https://segark-oficial.github.io/CentralOps)**

## Community & support

CentralOps is maintained as an open-source project with clear boundaries:

- **Questions & usage help** → [GitHub Discussions](https://github.com/SEGARK-oficial/CentralOps/discussions), not Issues.
- **Bugs & feature requests** → [Issues](https://github.com/SEGARK-oficial/CentralOps/issues). There is **no support SLA** on community issues.
- **Security disclosures** → follow [`SECURITY.md`](SECURITY.md) (never open a public issue for a vulnerability).
- **Commercial support, Enterprise, and incident-response services** → **support@segark.com**.

Contributions are welcome under the [Developer Certificate of Origin](CONTRIBUTING.md)
(a `Signed-off-by` sign-off) — no copyright assignment required. Start with
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

The Community core is licensed under the **GNU Affero General Public License v3.0**
(AGPLv3) — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Enterprise features are
proprietary and distributed separately under a commercial license; the AGPL artifact never
embeds Enterprise code.

## Trademarks

CentralOps is an independent project and is **not affiliated with, endorsed by, or
sponsored by Sophos, Microsoft, Wazuh Inc., or NinjaOne**. *Sophos* and *Sophos Central*
are trademarks of Sophos Ltd. *Microsoft Defender* is a trademark of Microsoft Corporation.
*Wazuh* is a trademark of Wazuh Inc. *NinjaOne* is a trademark of NinjaOne LLC. All other
trademarks are the property of their respective owners.
