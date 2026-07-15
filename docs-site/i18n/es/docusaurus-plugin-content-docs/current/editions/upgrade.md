---
sidebar_position: 2
title: Actualización a Enterprise
description: Paso a paso para activar la edición Enterprise — desde la suscripción hasta el producto en ejecución con las funcionalidades MSSP, sin reinstalar.
---

# Actualización a Enterprise

Activar la edición Enterprise **no reinstala** CentralOps. Cambias las imágenes
Community por las imágenes Enterprise (el mismo producto, con los módulos de pago compilados) y
proporcionas tu **licencia**. Sin una licencia válida, la misma imagen se ejecuta como Community —
así que el upgrade y el downgrade son reversibles.

## Visión general

```text
  Suscribir        Bundle del portal          Ejecutar
 ┌────────┐       ┌────────────────────┐       ┌─────────────────────────┐
 │ portal │ ────▶ │ licencia + keyring │ ────▶ │ docker login → pull EE  │
 │ segark │       │ + credencial pull  │       │ + arrancar con licencia │
 └────────┘       └────────────────────┘       └───────────┬─────────────┘
                                                           ▼
                                               edition=enterprise ✅
```

## 1. Suscríbete y obtén el bundle

1. Suscríbete a un plan en el portal (**segark.com**). Se emite una **licencia** para tu cuenta.
2. En el portal, obtén el **bundle de instalación** (`GET /api/portal/install/{license_id}`).
   Incluye todo lo que necesitas:
   - **`license_token`** — el JWT firmado (EdDSA) de tu licencia.
   - **`keyring`** — la clave **pública** (`<kid>.pem`) para verificar la licencia offline.
   - **`registry_credential`** — usuario + token para descargar las imágenes Enterprise privadas.
   - **`images`** — las refs exactas de las imágenes Enterprise (fija estas tags).

:::info[Seguridad]
La credencial de pull solo controla la **descarga** de la imagen. La activación real de las funcionalidades es
la **licencia**, verificada offline. Guarda el `license_token` y usa siempre las dos — usuario
**y** token — del `registry_credential`.
:::

## 2. Autentícate en el registry

Usa `username` y `password` del `registry_credential` del bundle (el token va por stdin,
nunca en el historial del shell):

```bash
echo "<registry_credential.password>" | \
  docker login ghcr.io -u "<registry_credential.username>" --password-stdin
```

## 3. Arranca con las imágenes Enterprise

### Docker Compose

Guarda la clave pública del keyring y exporta la licencia + las refs de imagen:

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

Apunta las imágenes a las refs Enterprise, monta el keyring público y pasa la licencia mediante un
Secret:

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

## 4. Verifica

```bash
curl -fsS http://localhost:3000/readyz
docker compose logs api | grep edition
# edition=enterprise plan=mssp features=3
```

Si ves **`edition=enterprise`**, el upgrade está completo — las funcionalidades MSSP
(multi-tenancy jerárquica, reseller, búsqueda federada) ya están activas. Si aparece
`edition=community`, la licencia no se encontró o es inválida: revisa el
`CENTRALOPS_LICENSE_TOKEN` y que el `<kid>.pem` esté en `LICENSE_KEYS_DIR`.

## Downgrade

Vuelve a las imágenes Community (o elimina el `CENTRALOPS_LICENSE_TOKEN`) y arranca de nuevo — la
plataforma vuelve a Community por diseño, sin perder datos.

## ¿Necesitas ayuda?

Contacta a **support@segark.com**.
