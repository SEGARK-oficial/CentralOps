---
sidebar_position: 2
title: Upgrade to Enterprise
description: Step by step to activate the Enterprise edition — from subscription to a running product with the MSSP features, without reinstalling.
---

# Upgrade to Enterprise

Activating the Enterprise edition **does not reinstall** CentralOps. You swap the
Community images for the Enterprise images (the same product, with the paid modules
compiled in) and provide your **license**. Without a valid license, the same image runs as
Community — so the upgrade and the downgrade are reversible.

## Overview

```text
  Assinar          Bundle do portal            Rodar
 ┌────────┐       ┌──────────────────┐        ┌─────────────────────────┐
 │ portal │ ────▶ │ licença + keyring │ ────▶ │ docker login → pull EE  │
 │ segark │       │ + credencial pull │        │ + subir com a licença   │
 └────────┘       └──────────────────┘        └───────────┬─────────────┘
                                                          ▼
                                              edition=enterprise ✅
```

## 1. Subscribe and grab the bundle

1. Subscribe to a plan on the portal (**segark.com**). A **license** is issued for your account.
2. On the portal, obtain the **install bundle** (`GET /api/portal/install/{license_id}`).
   It carries everything you need:
   - **`license_token`** — your license's signed (EdDSA) JWT.
   - **`keyring`** — the **public** key (`<kid>.pem`) to verify the license offline.
   - **`registry_credential`** — username + token to pull the private Enterprise images.
   - **`images`** — the exact Enterprise image refs (pin these tags).

:::info[Security]
The pull credential only controls the image **download**. The actual feature activation is
the **license**, verified offline. Keep the `license_token` and always use both — username
**and** token — from the `registry_credential`.
:::

## 2. Authenticate with the registry

Use the `username` and `password` from the bundle's `registry_credential` (the token goes
through stdin, never in the shell history):

```bash
echo "<registry_credential.password>" | \
  docker login ghcr.io -u "<registry_credential.username>" --password-stdin
```

## 3. Bring it up with the Enterprise images

### Docker Compose

Save the keyring's public key and export the license + the image refs:

```bash
mkdir -p license-keys
echo "<keyring[<kid>] do bundle>" > license-keys/<kid>.pem

export CENTRALOPS_LICENSE_TOKEN="<license_token do bundle>"
export LICENSE_KEYS_DIR=./license-keys
export CENTRALOPS_EE_IMAGE="<images.backend do bundle>"    # ex.: ghcr.io/segark-oficial/centralops-ee:v1.0.0
export CENTRALOPS_WEB_EE_IMAGE="<images.frontend do bundle>"

# sobe o CE + a overlay Enterprise (a partir da raiz do projeto)
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml up -d
```

### Kubernetes (Helm)

Point the images to the Enterprise refs, mount the public keyring, and pass the license via
a Secret:

```bash
kubectl -n centralops create secret generic centralops-license \
  --from-literal=CENTRALOPS_LICENSE_TOKEN="<license_token>" \
  --from-file=license-keys/<kid>.pem

helm upgrade centralops kubernetes/helm/centralops -n centralops \
  --set image.repository=ghcr.io/segark-oficial/centralops-ee \
  --set frontendImage.repository=ghcr.io/segark-oficial/centralops-ee-frontend \
  --set image.tag=v1.0.0 --set frontendImage.tag=v1.0.0 \
  -f values.override.yaml
```

## 4. Verify

```bash
curl -fsS http://localhost:3000/readyz
docker compose logs api | grep edition
# edition=enterprise plan=mssp features=3
```

If you see **`edition=enterprise`**, the upgrade is complete — the MSSP features
(hierarchical multi-tenancy, reseller, federated search) are already active. If
`edition=community` shows up, the license was not found or is invalid: check the
`CENTRALOPS_LICENSE_TOKEN` and whether `<kid>.pem` is in the `LICENSE_KEYS_DIR`.

## Downgrade

Go back to the Community images (or remove the `CENTRALOPS_LICENSE_TOKEN`) and bring it up
again — the platform falls back to Community by design, without losing data.

## Need help?

Reach out to **support@segark.com**.
