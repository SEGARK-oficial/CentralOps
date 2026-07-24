---
sidebar_position: 4
title: Upgrade to a newer version
description: "Step by step to move CentralOps from one version to the latest (e.g. 1.1.0 → 2.0.0) — the generic mechanics on Compose and Helm, the idempotent boot migration, verification and rollback — plus the 2.0.0 release notes."
---

# Upgrade to a newer version

Moving from one **version** to the latest (e.g. `1.1.0` → `2.0.0`) is, mechanically, a
routine operation: you swap the **image tag**, pull the new image and recreate the
services. There is no reinstall, no manual migration step, and your **data is
preserved**. This page covers the generic mechanics (they apply to any version) and, at
the end, the **release notes** with what changes in each release.

:::danger[2.0.0 is a major with a breaking change]

**2.0.0** bumps the **major** on purpose: it **removes the Alerts surface** (the `/alerts`
route, the alerts API endpoints, the `v1` Accept path of `/dashboard/summary`, and the MCP
`list_integration_alerts` tool). **Data and schema are preserved** — what changes is the
**read contract**. If you have bookmarks, automations or integrations hitting those paths,
**migrate them before upgrading** (details in [Release notes → 2.0.0](#200)). Sophos/Wazuh
alert **ingestion** does not change.

:::

:::note[This is different from "Upgrade to Enterprise"]

This page is about moving up a **version** (e.g. `1.1.0` → `2.0.0`), within the same
edition. To change **edition** — Community → Enterprise, activating the MSSP modules with
your license — see **[Upgrade to Enterprise](../editions/upgrade.md)**. The two processes
are independent: you upgrade the version of a Community or Enterprise stack in exactly the
same way.

:::

## Before you start

- **Back up the database.** A quick `pg_dump` before any upgrade
  (`docker compose -f compose/docker-compose.yml exec postgres pg_dump -U centralops centralops > backup.sql`).
- **Read the [release notes](#release-notes)** for your target version — especially the
  breaking changes.
- **Pin an immutable tag** (with `sha`) in production, so you know exactly what is running
  and can roll back reliably.

## How versions are tagged

Each release's images get two tags — a **moving** one (tracks the version) and an
**immutable** one (never changes content). In production, **pin the immutable one**.

| Edition | Release tag (moving) | Immutable tag (pin in production) | Extra tag |
|---|---|---|---|
| **Community** | `vX.Y.Z` — e.g. `v2.0.0` | `sha-<shortsha>` — e.g. `sha-a1b2c3d` | — |
| **Enterprise** | `vX.Y.Z-ee` — e.g. `v2.0.0-ee` | `vX.Y.Z-ee.<sha>` — e.g. `v2.0.0-ee.9f8e7d6` | `core-<coresha>` |

- The **release tag** is great for tracking the version, but it can be re-published — bad
  for reproducibility.
- The **immutable tag** (with `<sha>`) is the same image forever — **use it in
  production** and keep the previous version's tag for the rollback.
- On **Community**, the immutable one is the `sha-<shortsha>` tag (e.g. `sha-a1b2c3d`). If
  you'd rather not pin by commit, track the moving `vX.Y.Z` release tag or keep your own
  stable tag (e.g. `production`).
- The **Enterprise images are private** on GHCR and require `docker login` with the pull
  credential from your subscription — see
  **[Upgrade to Enterprise](../editions/upgrade.md)**.

## Docker Compose

### Community

In `compose/.env`, point at the new tag:

```dotenv
IMAGE_NAME=ghcr.io/segark-oficial/centralops
IMAGE_TAG=sha-a1b2c3d   # the immutable tag of the new version
```

Pull the images and recreate the services (from the repository root):

```bash
docker compose -f compose/docker-compose.yml pull
docker compose -f compose/docker-compose.yml up -d
```

There is no local build — the images come ready from the registry.

### Enterprise

On an Enterprise stack, swap **both** EE images in `compose/.env`:

```dotenv
CENTRALOPS_EE_IMAGE=ghcr.io/segark-oficial/centralops-ee:v2.0.0-ee.9f8e7d6
CENTRALOPS_WEB_EE_IMAGE=ghcr.io/segark-oficial/centralops-ee-frontend:v2.0.0-ee.9f8e7d6
```

And recreate **always with both files** (`-f` base + `-f` EE overlay):

```bash
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml pull
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml up -d
```

:::warning[Enterprise: include the overlay in EVERY command]

An `up -d`/`pull` with only the base file **silently downgrades the stack to Community**
(the image goes back to CE and the license keyring mount disappears). If you haven't
already, make the overlay permanent with
`COMPOSE_FILE=docker-compose.yml:docker-compose.ee.yml` in `compose/.env`. The full
mechanics are in **[Upgrade to Enterprise](../editions/upgrade.md)**.

:::

## Kubernetes (Helm)

Run a `helm upgrade` changing only the image **tag**.

**Community:**

```bash
helm upgrade centralops kubernetes/helm/centralops -n centralops \
  --set image.tag=sha-a1b2c3d \
  --set frontendImage.tag=sha-a1b2c3d \
  --reuse-values
```

**Enterprise** — keep the EE repositories too:

```bash
helm upgrade centralops kubernetes/helm/centralops -n centralops \
  --set image.repository=ghcr.io/segark-oficial/centralops-ee \
  --set image.tag=v2.0.0-ee.9f8e7d6 \
  --set frontendImage.repository=ghcr.io/segark-oficial/centralops-ee-frontend \
  --set frontendImage.tag=v2.0.0-ee.9f8e7d6 \
  --reuse-values
```

Helm performs a rolling update — the API, the frontend and the workers are separate
Deployments. Follow it with:

```bash
kubectl -n centralops rollout status deploy/centralops-api
```

:::tip[If you version your values in a file]

Prefer editing `image.tag`/`frontendImage.tag` in your `values.override.yaml` and running
`helm upgrade centralops kubernetes/helm/centralops -n centralops -f values.override.yaml`
— that keeps the desired state in Git instead of `--set` on the command line. The exact
`--set` flags for an Enterprise stack are in
**[Upgrade to Enterprise](../editions/upgrade.md)**.

:::

## What happens to your data

On the first boot of the new version, the API runs a **lightweight, idempotent
migration/seed** — **there is no manual Alembic step** in this release. Your **data is
preserved**:

- **Existing** mapping definitions (those that already have an active version) are **not
  overwritten**.
- Only **empty** definitions get a `v1` — that's how a release's **new defaults** show up
  (see [release notes](#release-notes)).
- Running the migration again (on a restart) **changes nothing** — it is idempotent.

Because the seed is **additive and non-destructive**, rolling back to the previous version
is safe (see below). If, right after the upgrade, the platform looks unavailable for a
moment while the services come up, give it a few seconds — the new version needs a short
time to become fully ready.

## Verify

1. **Edition and boot** — the boot log shows the resolved edition (and confirms the API
   came up on the new version):

   ```bash
   docker compose -f compose/docker-compose.yml logs centralops | grep edition=
   # edition=community        (or "edition=enterprise plan=... features=..." on an EE stack)
   ```

2. **Overall health** — open **Operations → Data flow** (`/flow`) and **Normalization →
   Pipeline health** (`/pipeline-health`) and confirm events keep flowing normally.

:::note[How NOT to check version/edition]

`/readyz` only reports **readiness** (db/redis) — **not** the edition or the version. The
`/api/edition` endpoint exists, but it **requires authentication**. For the edition, use
the boot log (`edition=`) or the **Settings → License** screen.

:::

## Rollback

Because this release has **no destructive migration**, going back is safe:

- **Compose:** re-point the **previous immutable tag** in `compose/.env` and run `pull` +
  `up -d` (with both `-f` files on an Enterprise stack).
- **Helm:** `helm rollback centralops` (reverts to the previous revision) or
  `helm upgrade ... --set image.tag=<previous-tag>`.

Data written by the new version stays readable by the previous one — the schema changes
are additive.

## Release notes

Each version adds a section here. Read the one for your target version **before**
upgrading.

### 2.3.0

A **minor** release: nothing breaks compatibility. Upgrading is the routine mechanic
described above, and an installation that never opens the new screens behaves exactly as
it did on 2.2.0.

**Collection filter — off by default.** Integrations whose vendor allows restricting the
query gained a **collection filter**: the discard now happens in the query sent to the
vendor, instead of after collecting and normalizing. Today **Wazuh (detections)** is the
integration that offers it, with a minimum rule level.

**No installation changes behavior on upgrade.** The filter is born at the value that cuts
nothing, and the query sent to the vendor is **identical** to the previous version's for as
long as nobody opens the screen. There is nothing to configure and nothing to revert.

It exists for a concrete case: when routing discards most of what comes in, the collector
is spending every cycle hauling noise — and that is the cause of collections that never
catch up to the present. Read [Collection filter](../pipelines/collection-filters)
before turning it on: what is filtered at the source **never enters the platform** (it does
not show up in live capture, does not raise a new field in the Drift Explorer, and is not
available to a future route), and turning it on or off **is not retroactive**.

**Concurrent cycles of the same stream are now skipped.** When a collection cycle takes
longer than the scheduled interval, the next cycle for that same `(integration, stream)` is
**skipped** instead of running in parallel. If you monitor the workers, you will see **one**
cycle where you used to see two or three at once.

:::note[This is not a throughput regression]
The simultaneous cycles read the **same** collection position and fetched **the same**
events — in production, concurrent cycles finished 34 ms apart over the same batch. Only
one of them advanced the position; the rest was work thrown away that still pressured the
source and made every cycle slower. Collecting stopped being done in duplicate; the amount
of events collected per hour does not drop.

The `collector_cycles_skipped_locked_total` counter shows how many cycles were skipped.
Rising steadily, it means the cycle now takes longer than the scheduled interval — that is,
there is a backlog. See
[Events arriving hours late](../runbooks/collection-lag-backlog).
:::

**Pipeline Health: data lag.** Each integration card now shows, on top of the time since
the last collection, the **Data lag** — how old the most recent event the collection has
brought in is. These are different questions: the first answers "is collection running?",
the second answers "is what I am looking at from now?".

:::warning[A card that turns yellow after the upgrade was most likely already behind]
The old indicator measured only the time since the last successful collection — and that
number resets on every cycle that finishes without an error, **even when the cycle processed
yesterday's events**. A collector that was 15 hours behind reported `0 s` of lag and a
**Healthy** status.

Upgrading closes that blind spot. A card that turns yellow (or starts showing hours of Data
lag) right after the upgrade was almost certainly **already behind before** — the update did
not create the lag, it made it visible. Treat it as a diagnosis, not a regression, and
follow [Events arriving hours late](../runbooks/collection-lag-backlog).
:::

The card only goes **yellow for backlog** when **both** conditions hold at the same time: the
last cycle ended at the per-cycle event cap **and** that stream's Data lag is over 30
minutes. High Data lag on its own does not change the color — a stream with no events keeps
its position parked on purpose. Details in
[Pipeline Health](../operations/pipeline-health).

### 2.2.0

**In-flight detection (correlation on the hot path).** Correlation rules can now be
evaluated during ingestion, not only at the end of a federated search. The screen gained a
preview of a rule against real samples **without persisting anything**, 24h per-rule
counters, and documentation of why a rule stays silent.

Nothing changes for anyone who does not create an in-flight rule — the evaluation mode
starts at the previous behaviour.

### 2.1.0

**OCSF fidelity and collection fixes.** `timestamp_t` is now emitted in **milliseconds**
(it was seconds — a 1000× error across every mapping), Veeam now maps to *Scheduled Job
Activity* and CloudWatch to *Base Event*.

**Paginating collectors gained a per-cycle cap.** Without it, a large backlog was drained
in a single run until the task time limit blew, which reverted the collection position and
started over — the collector was stuck making no progress. With the cap, the cycle ends and
the next one resumes where it stopped.

:::note[The cap fixed the stall, not the backlog]
It bounds the **raw** volume pulled per cycle, with no knowledge of how much of that
routing will discard next. A source with a high discard rate still spends every cycle
hauling what will be thrown away. That is the problem the **collection filter** in 2.3.0
attacks.
:::

**Per-route counters and savings metrics** are now recorded unconditionally, not only when
sampling was on.

### 2.0.0

**2.0.0** is a **major**: it removes the Alerts surface — that is why the jump from `1.x`
to `2.0`. It is the **only** compatibility-breaking change; everything else is features,
fixes and performance improvements (no action required).

:::danger[Breaking: the Alerts surface has been REMOVED]

The **Alerts** area has been **removed entirely** in this version. The change **is** in the
automatic changelog (marked as `⚠ BREAKING CHANGE`) — it is what made the release become
`2.0.0`. What goes away:

- The **`/alerts`** route no longer exists (old bookmarks → **404**).
- The **alerts API endpoints** were removed.
- The **Accept v1** path of `GET /dashboard/summary`
  (`application/vnd.centralops.v1+json`) was removed.
- The **MCP tool `list_integration_alerts`** was removed.

**What to do:** triage is now vendor-neutral, via **Operations → Investigations /
Federated search** and **Detections**. If you have automations or integrations hitting the
alerts endpoints (or the Accept v1 path of `/dashboard/summary`), **migrate them** to
those paths before upgrading.

The **ingestion** of `sophos.alert` (the data that enters the pipeline) **does not
change** — only the "alerts" read surface is gone.

:::

**New (nothing to configure — already on):**

- **Robust CSV export from Federated search**, with localized labels (PT/EN/ES) — under
  **Operations → Investigations**.
- **A `/flow` map that scales.** The **Data flow** view (Operations → Data flow)
  collapses dense columns into an expandable **"+N"** node and fits itself to the screen
  (fit-to-view), with path highlight on hover — readable even with dozens of
  sources/routes/destinations.
- **Readable route-condition labels.** In the route editor, condition operators show
  human, localized names instead of the raw label.
- **Wazuh detection-mapping validation** + a fix for a missing seed definition.

**Cost metering on by default.** `COST_METERING_ENABLED` now defaults to **`true`**. As a
result, the **"Volume & cost reduction"** card starts showing up under **Operations →
Data flow**: on Community it shows the volume, the percentage and the bytes saved; on
Enterprise it adds the **US$** figure (from the `cost_per_gb` configured on each
destination). To turn it off, set `COST_METERING_ENABLED=false`.

**Operational fixes** (informational — nothing to do):

- Collectors no longer enter a **RedBeat crash-loop** (lock, loop-cap and idempotent
  scheduler registration fixed). See also **Observability** (Operations → Observability)
  to track Beat health.
- The **collection soft-timeout** no longer poisons the database connection pool (pool
  disposal + early initialization avoid `UnboundLocalError`).
- An **empty `SESSION_SECURE_COOKIE`** no longer breaks boot; OCSF resource path anchoring
  was fixed.
- **Service account (shim) IDs** are sanitized — no more FK violations in audit/mapping.
- **OCSF validation** runs again on the compiled image.

**Performance:** ingestion volume metering is now **batched** (`InVolumeAccumulator`),
cutting Redis I/O latency on the hot path.

**New mapping defaults.** This version seeds default definitions for **Wazuh** and for
**CrowdStrike, Entra ID, Okta and CloudTrail**. They only fill **empty definitions** —
mappings you have already customized are left untouched (see
[what happens to your data](#what-happens-to-your-data)).

## Next steps

- **[Upgrade to Enterprise](../editions/upgrade.md)** — change **edition**
  (Community → Enterprise), not version.
- **[Deploy with Docker Compose](./docker-compose.md)** — operating the single-host stack.
- **[Deploy with Kubernetes (Helm)](./kubernetes.md)** — rollout, HPA and rollback in the
  cluster.
- **[Configuration](./configuration.md)** — all the environment variables.
