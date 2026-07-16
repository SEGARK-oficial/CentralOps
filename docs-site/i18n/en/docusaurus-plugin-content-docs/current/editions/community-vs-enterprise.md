---
sidebar_position: 1
title: Editions — Community vs Enterprise
description: What CentralOps Community (open-source) already delivers and what the Enterprise edition adds for MSSPs and operations at scale.
---

# Editions: Community vs Enterprise

CentralOps follows an **open-core** model. The **Community** edition in this repository is
open-source (AGPLv3), complete, and usable in production on your own. The **Enterprise**
edition adds the capabilities that scale with the **size and maturity** of the
organization — designed for MSSPs and multi-tenant SOCs.

## What is always free (Community)

Baseline security **never** sits behind a paywall:

- **Ingestion from every source** — Sophos, Microsoft Defender, Wazuh, NinjaOne, and
  any vendor via the plugin registry; syslog, APIs, S3, Kafka, and edge push.
- **OCSF normalization** with the versioned DSL (CML), dry-run, and rollback.
- **Routing to 14+ destinations** (SIEMs, data lakes) with quarantine and drift detection.
- **SSO / OIDC (Entra)** + **RBAC** — no "SSO tax".
- **Encryption / KMS** and **PII redaction** in the pipeline.
- **Baseline append-only auditing** and the **MCP server** for automation.

## What Enterprise adds

| Capability | Why it's Enterprise |
|---|---|
| **Hierarchical multi-tenancy / reseller (MSSP)** | Tenant hierarchy and reseller program — scales with the size of the operation. |
| **Cross-org / asynchronous federated search** | Active search that spans organizations and sources. |
| **Cross-tenant audit & compliance** | Tamper-evident auditing (WORM), long retention, signed export. |
| **HA / fleet** | High availability, multi-node, and fleet orchestration. |

## How the gate works

The separation is **honest and verifiable**:

- The Community artifact **never** contains the Enterprise code — the paid modules are
  distributed as a **separate, license-activated image**.
- The license is a **signed JWT (EdDSA)** verified **offline** against a **public** keyring
  **you mount alongside the product** — the public key (`<kid>.pem`) is delivered with
  the license (downloaded from the portal); the product **embeds no keys**. Without a
  valid license, the Enterprise features are **blocked** — nothing breaks, the image
  keeps running as Community.
- The license has a **short expiration** and an offline **revocation list**: subscribing
  activates, churn deactivates.

This is the same pattern used by projects like GitLab and Grafana: the core is genuinely
open and useful, and monetization comes from the capabilities that scale with the
organization.

## Ready for Enterprise?

See the step-by-step guide in **[Upgrade to Enterprise](./upgrade.md)** or reach out to
**support@segark.com**.
