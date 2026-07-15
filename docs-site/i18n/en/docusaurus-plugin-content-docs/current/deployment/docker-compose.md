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

Copy the example and set the required values:

```bash
cp .env.example .env
```

At a minimum, set the following in `.env`:

- **`POSTGRES_PASSWORD`** — database password (required; without it, compose refuses to start).
- **`APP_MASTER_KEY`** — master encryption key (≥ 32 characters). If you leave it
  blank, the container **generates one on first startup** and persists it at
  `/app/data/app_master_key` — keep that file safe.

For production with HTTPS, keep `APP_ENV=production` and `SESSION_SECURE_COOKIE=true`
(the example default). The full reference is in **[Configuration](./configuration.md)**.

### 3. Bring up the stack

```bash
cd compose
docker compose up --build -d
```

The first build pulls the images and compiles the frontend — subsequent builds are nearly
instant.

### 4. Access

- **HTTP:** `http://localhost:3000`
- **HTTPS:** `https://localhost:3443`

If `certs/tls.crt` and `certs/tls.key` don't exist, the container **generates a
self-signed certificate** automatically (the browser will warn you; accept it for
testing). For your own certificate, mount the files in `certs/`.

### 5. Check health

```bash
curl -fsS http://localhost:3000/readyz
# {"status":"ready","checks":{"db":"ok","redis":"ok"}}
```

`ready` means the API, database, and Redis are up. Now head over to
**[First Login](../getting-started/first-login.md)** to create the administrator account.

## Single image (without cloning the repo)

The images are published to the GitHub Container Registry. To run without cloning:

```bash
docker run -d --name centralops \
  -p 3000:80 -p 3443:443 \
  -e APP_MASTER_KEY="set-a-key-of-at-least-32-characters" \
  -e ENABLE_HTTPS=1 \
  -v centralops-data:/app/data \
  ghcr.io/segark-oficial/centralops:latest
```

Settings can be provided via `--env-file .env` or via an `/app/.env` file mounted into the
container. Pin an **immutable tag** (e.g., `v1.0.0`) in production — avoid `latest`.

## Basic operations

| Action | Command |
|---|---|
| View logs | `docker compose logs -f api` |
| Stop | `docker compose down` |
| Update version | `docker compose pull && docker compose up -d` |
| Database backup | `docker compose exec postgres pg_dump -U centralops centralops > backup.sql` |

## Next steps

- **[Configuration](./configuration.md)** — all environment variables.
- **[First Login](../getting-started/first-login.md)** — create the admin and the team.
- **[Quickstart](../getting-started/quickstart.md)** — connect your first source.
