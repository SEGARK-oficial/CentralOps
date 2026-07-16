---
sidebar_position: 1
title: Deploy with Docker Compose
description: Bring up CentralOps on a single host with Docker Compose — API, workers, database, and frontend in minutes.
---

# Deploy with Docker Compose

The fastest way to run CentralOps is with **Docker Compose**. It brings up everything the
platform needs — API, collection workers (Celery), scheduler, Redis, Postgres, and the
frontend (Nginx) — on a single host. Ideal for **evaluation, development, and single-host
production**.

:::tip[When to use]
- **Docker Compose** (this page): 1 server, get started fast.
- **[Kubernetes / Helm](./kubernetes.md)**: production at scale, multi-node, HA.
:::

## Prerequisites

- **Docker** 24+ and the **Docker Compose** v2 plugin (`docker compose version`).
- ~2 vCPU / 4 GB of RAM to start.
- Free ports on the host: **3000** (HTTP) and **3443** (HTTPS).

## Step by step

### 1. Get the code

```bash
git clone https://github.com/SEGARK-oficial/CentralOps.git
cd CentralOps
```

### 2. Configure the environment

`docker compose` reads the `.env` **from the compose file's directory** (`compose/`) — not
from the repository root. Copy the example there and adjust the secrets:

```bash
cp compose/.env.example compose/.env
```

Set in `compose/.env` (compose **refuses to start** without the first two):

- **`POSTGRES_PASSWORD`** — Postgres password (required).
- **`REDIS_PASSWORD`** — Redis password; Redis AUTH is always enforced (required).
- **`APP_MASTER_KEY`** — master encryption key (≥ 32 characters). **Required**
  with `APP_ENV=production` (the example's default): without it the container **aborts
  at boot**. Generate it with `openssl rand -hex 32` (below). Auto-generation (persisted
  at `/app/data/app_master_key`) only exists in dev/test, with `APP_ENV=development`.

Generate strong secrets:

```bash
openssl rand -base64 24   # POSTGRES_PASSWORD and REDIS_PASSWORD
openssl rand -hex 32      # APP_MASTER_KEY
```

For production with HTTPS, keep `APP_ENV=production` (the example's default — it forces
`SESSION_SECURE_COOKIE=true`). The full reference is in
**[Configuration](./configuration.md)**.

### 3. Bring up the stack

From the **repository root**, pointing at the file in `compose/`:

```bash
docker compose -f compose/docker-compose.yml up --build -d
```

The first build compiles the backend and the frontend — subsequent startups are nearly
instant.

### 4. Access

- **HTTP:** `http://localhost:3000`
- **HTTPS:** `https://localhost:3443`

If `certs/tls.crt` and `certs/tls.key` don't exist, the container **generates a
self-signed certificate** automatically (the browser will warn you; accept it for
testing). For your own certificate, mount the files in `certs/`.

### 5. Check health

Actual readiness (Postgres + Redis) is verified by the **container healthcheck** — the
API's `/readyz` is not published at the edge. Check the state of the services:

```bash
docker compose -f compose/docker-compose.yml ps
```

The `centralops` (API) and `frontend` services should show up as **`healthy`**. To read
the readiness JSON straight from the API:

```bash
docker compose -f compose/docker-compose.yml exec centralops \
  curl -fsS http://127.0.0.1:8000/readyz
# {"status":"ready","checks":{"db":"ok","redis":"ok"}}
```

With everything `healthy`, head over to
**[First Login](../getting-started/first-login.md)** to create the administrator
account.

## Run prebuilt images (no build)

The official images are published to the GitHub Container Registry on every release:
`ghcr.io/segark-oficial/centralops` (API) and `ghcr.io/segark-oficial/centralops-frontend`
(frontend). To bring the stack up **without compiling locally**, point compose at them in
`compose/.env`:

```dotenv
IMAGE_NAME=ghcr.io/segark-oficial/centralops
IMAGE_TAG=v1.0.0   # pin a release tag; avoid `latest` in production
```

And start by pulling the images instead of building:

```bash
docker compose -f compose/docker-compose.yml pull
docker compose -f compose/docker-compose.yml up -d
```

:::note

The stack needs several services (API, frontend, workers, Postgres, Redis) — there is no
single image that runs everything in one container. `compose/docker-compose.yml` is what
orchestrates the set, whether building (step 3) or pulling the prebuilt images.

:::

## Basic operations

Running from the repository root (every command points at `compose/docker-compose.yml`):

:::warning[Enterprise install? Include the overlay in ALL commands]

If your stack runs the **Enterprise** edition
([Upgrade to Enterprise](../editions/upgrade.md)), **every** `docker compose` command on
this page must also include `-f compose/docker-compose.ee.yml` — for example:
`docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml up -d`.
An `up -d`/`pull` with only the base file **silently downgrades the stack to Community**
(the EE image and the license keyring mount are removed). Alternative: make the overlay
permanent with `COMPOSE_FILE=docker-compose.yml:docker-compose.ee.yml` in
`compose/.env` and run the commands from inside `compose/`, without `-f`.

:::

| Action | Command |
|---|---|
| View API logs | `docker compose -f compose/docker-compose.yml logs -f centralops` |
| View frontend logs | `docker compose -f compose/docker-compose.yml logs -f frontend` |
| Stop | `docker compose -f compose/docker-compose.yml down` |
| Update images | `docker compose -f compose/docker-compose.yml pull && docker compose -f compose/docker-compose.yml up -d` |
| Database backup | `docker compose -f compose/docker-compose.yml exec postgres pg_dump -U centralops centralops > backup.sql` |

## Next steps

- **[Configuration](./configuration.md)** — all environment variables.
- **[First Login](../getting-started/first-login.md)** — create the admin and the team.
- **[Quickstart](../getting-started/quickstart.md)** — connect your first source.
