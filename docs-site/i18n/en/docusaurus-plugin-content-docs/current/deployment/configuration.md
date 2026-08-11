---
sidebar_position: 3
title: Configuration (environment variables)
description: Reference for CentralOps environment variables — what is required, what has a safe default, and what to tune per environment.
---

# Configuration

CentralOps is configured through **environment variables**. In Docker Compose they come from
`compose/.env`; in Kubernetes, from a `Secret`/`ConfigMap`.

The repository ships two example files, with distinct roles:

| File | Role |
| --- | --- |
| `compose/.env.example` | **Quickstart** — the one you copy and edit to bring the stack up. |
| `.env.example` (repo root) | **Full reference** — for lookup. Variables that never reach the compose file (rate limiting, auth lockout, PII, Vault/KMS, sessions, trusted proxies) are documented only here. |

:::caution[The `.env` lives in `compose/`, not the repo root]
Compose reads `.env` from the compose file's directory. A `.env` created at the repo
root is **silently ignored** — the stack boots on defaults as if the file did not
exist. Always copy it with `cp compose/.env.example compose/.env`.
:::

:::info[Bare minimum]
To go live in production you only need **`POSTGRES_PASSWORD`** and, ideally, an
**`APP_MASTER_KEY`** that you define yourself. Everything else has a safe default.
:::

## Essentials

| Variable | Default | Description |
|---|---|---|
| `APP_MASTER_KEY` | *(generated and persisted at `/app/data/app_master_key`)* | Master encryption key for secrets (≥ 32 characters). **Set it yourself** in production and store it securely — losing it makes secrets unreadable. |
| `APP_ENV` | `production` | `production` requires HTTPS/secure cookie; use `development` for local dev without TLS. |
| `APP_COMPANY_NAME` | `Your Company` | Name shown in the interface. |
| `APP_COMPANY_PORTAL_NAME` | `Login Portal` | Subtitle on the login screen. |

## Database (Postgres)

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_PASSWORD` | *(empty — **required**)* | Postgres password. Without a value, the compose stack refuses to start. |
| `POSTGRES_USER` | `centralops` | Database user. |
| `POSTGRES_DB` | `centralops` | Database name. |
| `DATABASE_URL` | *(derived from the vars above)* | Override only to use an **external/managed Postgres** (RDS, Neon…) or to fall back to SQLite in dev: `sqlite:////app/data/app.db`. |

Docker Compose brings up a **Postgres 16** with a named volume by default. In serious
production, prefer a managed Postgres and point `DATABASE_URL` at it.

## HTTPS and networking (Nginx)

| Variable | Default | Description |
|---|---|---|
| `ENABLE_HTTPS` | `0` | `1` enables Nginx with TLS. If no certificate is provided in `certs/`, a self-signed one is generated. |
| `NGINX_SERVER_NAME` | `_` | Value of `server_name` in Nginx (use your domain in production, e.g. `centralops.yourcompany.com`). |

## Session and security

| Variable | Default | Description |
|---|---|---|
| `SESSION_SECURE_COOKIE` | `true` | Use `true` when the primary access is HTTPS (required with `APP_ENV=production`). |
| `DEBUG_REQUESTS` | `0` | `1` writes `debug_requests.log` with the calls to external APIs (diagnostics; turn it off in production). |

## Integration secrets

The credentials for each integration are encrypted at rest. The default cipher provider is
**`local_fernet`** (AES derived from `APP_MASTER_KEY`). Details and rotation in
**[Secrets and master key](/en/administration/secrets-and-master-key)**.

## Best practices

- **Pin versions:** use an immutable image tag (e.g. `v1.0.0`) in production, not `latest`.
- **External APP_MASTER_KEY:** define it via a Secret and back it up — it is the key to everything.
- **HTTPS always:** `ENABLE_HTTPS=1` + `SESSION_SECURE_COOKIE=true` + `NGINX_SERVER_NAME` set to your domain.
- **Managed Postgres** in production (backup, HA, and patching handled by the provider).
