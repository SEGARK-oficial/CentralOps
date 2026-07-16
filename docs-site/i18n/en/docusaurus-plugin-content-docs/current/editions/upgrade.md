---
sidebar_position: 2
title: Upgrade to Enterprise
description: Step by step to activate the Enterprise edition — from subscription to a running product with the MSSP features, without reinstalling.
---

# Upgrade to Enterprise

Activating the Enterprise edition **does not reinstall** CentralOps. You swap the
Community images for the Enterprise images (the same product, with the paid modules
compiled in) and provide your **license**. Without a valid license, the same image runs
as Community — so the upgrade and the downgrade are reversible.

## What you will need

The license comes as **two files**, and both are required:

| File | What it is | Where it goes |
|---|---|---|
| `segark-pipeline-license-<kid>.jwt` | The signed (EdDSA) license **token**. | `CENTRALOPS_LICENSE_TOKEN` (or the **Settings → License** screen). |
| `key.prod.pem` | The **public** key the product uses to **verify** the token offline. | A directory pointed to by `CENTRALOPS_LICENSE_KEYS_DIR`. |

:::warning[Without the public key, the license won't activate]

The token alone is **not enough**: without `key.prod.pem` in the keyring, the product
can't verify the signature and answers `unknown key id: 'key.prod'` — staying on
Community by design. Always download **both** files.

:::

## Overview

```text
  segark.com portal          Registry                    Run
 ┌───────────────────┐      ┌────────────────┐      ┌──────────────────────────┐
 │ License page:     │ ───▶ │ docker login → │ ───▶ │ start the EE images with │
 │ token + key.pem   │      │ pull EE images │      │ token + mounted keyring  │
 └───────────────────┘      └────────────────┘      └───────────┬──────────────┘
                                                                ▼
                                                    edition=enterprise ✅
```

## 1. Download the license from the portal

1. Subscribe to a plan on the portal (**segark.com**). The license is issued to your account.
2. Sign in to the portal and open the **License** page.
3. Download both files:
   - **Download token (.jwt)** — the signed license token.
   - **Download key (key.prod.pem)** — the public keyring key.

The page itself shows the "How to activate" summary with these steps.

## 2. Authenticate to the registry

The Enterprise images are **private** on the GitHub Container Registry:
`ghcr.io/segark-oficial/centralops-ee` (API/workers) and
`ghcr.io/segark-oficial/centralops-ee-frontend` (frontend). Use the pull credential
provided with your subscription (portal install bundle, or **support@segark.com**):

```bash
echo "<credential password>" | \
  docker login ghcr.io -u "<credential username>" --password-stdin
```

The token goes through stdin so it never lands in your shell history.

:::info[Security]

The pull credential only controls the image **download**. The real feature activation is
the **license**, verified offline inside the product.

:::

## 3. Start with the Enterprise images

EE tags follow the Core version: `v1.0.0-ee` (tracks the release) and
`v1.0.0-ee.<sha>` (immutable — **prefer this one in production**).

### Docker Compose

Put the public key next to the compose files and configure `compose/.env`:

```bash
mkdir -p compose/license-keys
cp ~/Downloads/key.prod.pem compose/license-keys/
```

In `compose/.env`, add:

```dotenv
CENTRALOPS_LICENSE_TOKEN=<contents of the .jwt file>
LICENSE_KEYS_DIR=./license-keys
CENTRALOPS_EE_IMAGE=ghcr.io/segark-oficial/centralops-ee:v1.0.0-ee
CENTRALOPS_WEB_EE_IMAGE=ghcr.io/segark-oficial/centralops-ee-frontend:v1.0.0-ee
```

And start CE + the Enterprise overlay (from the project root):

```bash
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml pull
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml up -d
```

:::tip[Prefer activating through the UI?]

With `key.prod.pem` mounted (the `LICENSE_KEYS_DIR` above), you can leave
`CENTRALOPS_LICENSE_TOKEN` out and paste the token on the product's
**Settings → License** screen as an administrator. The license is stored (encrypted) in
the database and survives restarts.

:::

### Kubernetes (Helm)

Create the image pull secret (the chart uses `ghcr-secret` by default) and upgrade
pointing at the images, token and keyring:

```bash
kubectl -n centralops create secret docker-registry ghcr-secret \
  --docker-server=ghcr.io \
  --docker-username="<credential username>" \
  --docker-password="<credential password>"

helm upgrade centralops kubernetes/helm/centralops -n centralops \
  --set image.repository=ghcr.io/segark-oficial/centralops-ee \
  --set image.tag=v1.0.0-ee \
  --set frontendImage.repository=ghcr.io/segark-oficial/centralops-ee-frontend \
  --set frontendImage.tag=v1.0.0-ee \
  --set secrets.licenseToken="<contents of the .jwt file>" \
  --set-file "secrets.licenseKeyring.key\.prod\.pem=./key.prod.pem" \
  -f values.override.yaml
```

The chart mounts the keyring on every pod and sets `CENTRALOPS_LICENSE_KEYS_DIR`
automatically. For GitOps/ExternalSecrets, use `secrets.existingSecret` and
`secrets.existingLicenseKeyring` instead of the inline values.

## 4. Verify

At boot, the API logs the resolved edition:

```bash
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml \
  logs centralops | grep edition=
# edition=enterprise plan=mssp features=3
```

You can also check it in the UI, under **Settings → License** (shows the edition, plan
and active features).

If you see **`edition=community`**, the license wasn't found or couldn't be verified:

- **`unknown key id: 'key.prod'`** — `key.prod.pem` is not in the keyring. Check that
  the file is in the `LICENSE_KEYS_DIR` directory (Compose) or in
  `secrets.licenseKeyring` (Helm) and restart: the keyring is read at boot.
- **Missing/expired token** — check `CENTRALOPS_LICENSE_TOKEN` (or re-activate through
  the License screen) and the validity on the portal.

## Downgrade

Go back to the Community images (or remove `CENTRALOPS_LICENSE_TOKEN`) and start again —
the platform falls back to Community by design, without losing data.

## Need help?

Talk to **support@segark.com**.
