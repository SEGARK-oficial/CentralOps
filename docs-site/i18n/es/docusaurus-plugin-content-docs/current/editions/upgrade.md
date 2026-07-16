---
sidebar_position: 2
title: Actualización a Enterprise
description: Paso a paso para activar la edición Enterprise — desde la suscripción hasta el producto en ejecución con las funcionalidades MSSP, sin reinstalar.
---

# Actualización a Enterprise

Activar la edición Enterprise **no reinstala** CentralOps. Cambias las imágenes
Community por las imágenes Enterprise (el mismo producto, con los módulos de pago
compilados) y proporcionas tu **licencia**. Sin una licencia válida, las funciones
Enterprise quedan bloqueadas (la imagen sigue ejecutándose como Community) — así que el
upgrade y el downgrade son reversibles.

## Lo que vas a necesitar

La licencia viene en **dos archivos**, y ambos son obligatorios:

| Archivo | Qué es | Adónde va |
|---|---|---|
| `segark-pipeline-license-<kid>.jwt` | El **token** firmado (EdDSA) de tu licencia. | `CENTRALOPS_LICENSE_TOKEN` (o la pantalla **Configuración → Licencia**). |
| `key.prod.pem` | La clave **pública** que el producto usa para **verificar** el token offline. | Un directorio apuntado por `CENTRALOPS_LICENSE_KEYS_DIR`. |

:::warning[Sin la clave pública, la licencia no se activa]

El token solo **no basta**: sin el `key.prod.pem` en el keyring, el producto no puede
verificar la firma y responde `unknown key id: 'key.prod'` — quedándose en Community por
diseño. Descarga siempre **los dos** archivos.

:::

## Visión general

```text
  Portal segark.com          Registry                    Ejecutar
 ┌───────────────────┐      ┌────────────────┐      ┌──────────────────────────┐
 │ página License:   │ ───▶ │ docker login → │ ───▶ │ subir imágenes EE con    │
 │ token + key.pem   │      │ pull imágenes  │      │ token + keyring montado  │
 └───────────────────┘      └────────────────┘      └───────────┬──────────────┘
                                                                ▼
                                                    edition=enterprise ✅
```

## 1. Descarga la licencia en el portal

1. Suscríbete a un plan en el portal (**segark.com**). La licencia se emite para tu cuenta.
2. Inicia sesión en el portal y abre la página **License**.
3. Descarga los dos archivos:
   - **Download token (.jwt)** — el token firmado de la licencia.
   - **Download key (key.prod.pem)** — la clave pública del keyring.

La propia página muestra el resumen de activación ("How to activate") con estos pasos.

## 2. Autentícate en el registry

Las imágenes Enterprise son **privadas** en el GitHub Container Registry:
`ghcr.io/segark-oficial/centralops-ee` (API/workers) y
`ghcr.io/segark-oficial/centralops-ee-frontend` (frontend). Usa la credencial de pull
proporcionada con tu suscripción (bundle de instalación del portal, o
**support@segark.com**):

```bash
echo "<password de la credencial>" | \
  docker login ghcr.io -u "<username de la credencial>" --password-stdin
```

El token va por stdin para que no quede en el historial del shell.

:::info[Seguridad]

La credencial de pull solo controla la **descarga** de la imagen. La activación real de
las funcionalidades es la **licencia**, verificada offline dentro del producto.

:::

## 3. Arranca con las imágenes Enterprise

Las tags EE siguen la versión del Core: `vX.Y.Z-ee` acompaña la release (p. ej. `v1.0.1-ee`)
y `vX.Y.Z-ee.<sha>` es inmutable (p. ej. `v1.0.1-ee.2e8917d` — **prefiere esta en producción**).

### Docker Compose

Coloca la clave pública junto a los archivos compose y configura el `compose/.env`:

```bash
mkdir -p compose/license-keys
cp ~/Downloads/key.prod.pem compose/license-keys/
```

En `compose/.env`, agrega:

```dotenv
CENTRALOPS_LICENSE_TOKEN=<contenido del archivo .jwt>
LICENSE_KEYS_DIR=./license-keys
CENTRALOPS_EE_IMAGE=ghcr.io/segark-oficial/centralops-ee:v1.0.1-ee
CENTRALOPS_WEB_EE_IMAGE=ghcr.io/segark-oficial/centralops-ee-frontend:v1.0.1-ee
```

Y levanta el CE + la overlay Enterprise (desde la raíz del proyecto):

```bash
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml pull
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml up -d
```

:::warning[La overlay Enterprise no es permanente ("sticky")]

Compose solo aplica el `docker-compose.ee.yml` cuando se pasa con `-f` — y eso vale
para **todo comando futuro**. Un `docker compose up -d` (o `pull`, o cualquier
recreación) solo con el archivo base **degrada la stack a Community silenciosamente**:
la imagen vuelve a ser la CE, el mount del keyring desaparece y la siguiente activación
falla con `unknown key id`. Para hacer la overlay permanente, define en `compose/.env`:

```dotenv
COMPOSE_FILE=docker-compose.yml:docker-compose.ee.yml
```

Con eso, un simple `docker compose up -d` (ejecutado desde dentro de `compose/`, sin
`-f`) ya aplica la overlay, y los comandos de día 2 no degradan la stack.

:::

:::tip[¿Prefieres activar por la interfaz?]

Con el `key.prod.pem` montado (el `LICENSE_KEYS_DIR` de arriba), puedes dejar fuera el
`CENTRALOPS_LICENSE_TOKEN` y pegar el token en la pantalla
**Configuración → Licencia** del producto, como administrador. La licencia queda
guardada (cifrada) en la base de datos y sobrevive a los reinicios.

La pantalla de Licencia también existe — y acepta el paste — en una stack Community
levantada **sin** la overlay; en ese caso el keyring del contenedor está vacío y la
activación falla exactamente con `unknown key id: 'key.prod'`. Antes de pegar, confirma
que la clave está visible dentro del contenedor:

```bash
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml \
  exec centralops ls /licensing
# key.prod.pem
```

:::

### Kubernetes (Helm)

Crea el secret de pull de las imágenes (el chart usa `ghcr-secret` por defecto) y haz el
upgrade apuntando imágenes, token y keyring:

```bash
kubectl -n centralops create secret docker-registry ghcr-secret \
  --docker-server=ghcr.io \
  --docker-username="<username de la credencial>" \
  --docker-password="<password de la credencial>"

helm upgrade centralops kubernetes/helm/centralops -n centralops \
  --set image.repository=ghcr.io/segark-oficial/centralops-ee \
  --set image.tag=v1.0.1-ee \
  --set frontendImage.repository=ghcr.io/segark-oficial/centralops-ee-frontend \
  --set frontendImage.tag=v1.0.1-ee \
  --set secrets.licenseToken="<contenido del archivo .jwt>" \
  --set-file "secrets.licenseKeyring.key\.prod\.pem=./key.prod.pem" \
  -f values.override.yaml
```

El chart monta el keyring en todos los pods y define `CENTRALOPS_LICENSE_KEYS_DIR`
automáticamente. Para GitOps/ExternalSecrets, usa `secrets.existingSecret` y
`secrets.existingLicenseKeyring` en lugar de los valores inline.

## 4. Verifica

En el arranque, la API registra la edición resuelta:

```bash
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml \
  logs centralops | grep edition=
# edition=enterprise plan=mssp features=3
```

También puedes comprobarlo en la interfaz, en **Configuración → Licencia** (muestra la
edición, el plan y las funcionalidades activas).

Si aparece **`edition=community`**, la licencia no se encontró o no pudo verificarse:

- **`unknown key id: 'key.prod'`** — el keyring que **ve el contenedor** está vacío o
  no contiene el `key.prod.pem`. Sigue el paso a paso de abajo.
- **Token ausente/expirado** — comprueba el `CENTRALOPS_LICENSE_TOKEN` (o reactívalo por
  la pantalla de Licencia) y la validez en el portal.

Una instalación Enterprise cuya licencia **no cubre una funcionalidad** (un plan que no
la incluye, una licencia ausente o expirada más allá del período de tolerancia) rechaza
la acción correspondiente con el estado **`license_required`** — por ejemplo, al
sincronizar los tenants de un partner. En ese caso la solución no es el keyring:
comprueba el plan y la validez en **Configuración → Licencia** o en el portal.

### Corrigiendo `unknown key id`

La causa dominante es el **keyring vacío dentro del contenedor** — en general porque la
stack se levantó (o se recreó) **sin la overlay Enterprise**. El `key.prod.pem` puede
estar perfecto en el host y aun así nunca llegar al contenedor. Diagnostica de dentro
hacia fuera:

**1. Imagen y mount** — ¿el contenedor de la API usa la imagen EE y tiene el mount
`/licensing`?

```bash
docker inspect --format '{{.Config.Image}} {{json .Mounts}}' \
  $(docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml ps -q centralops)
```

**2. Lo que ve el proceso** — la variable y el directorio dentro del contenedor:

```bash
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml \
  exec centralops sh -c 'echo $CENTRALOPS_LICENSE_KEYS_DIR; ls -la /licensing'
```

**3. Permisos** — la API corre como uid `10001`: el `.pem` debe ser legible por ella
(archivo `0644`, directorio `0755`). Un `key.prod.pem` con `0600 root:root` se ignora
en silencio.

**4. Logs del keyring** — el arranque registra lo que se cargó (o no):

```bash
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml \
  logs centralops | grep -iE 'skipping|license keyring'
```

Si falta el mount o la variable, **recrea** los contenedores con los dos `-f`
(`up -d`) — `docker compose restart` **no** aplica mounts ni variables de entorno
nuevas. Con el keyring corregido, pega el token de nuevo en la pantalla de Licencia
**sin reiniciar nada**: el keyring se relee en cada activación (y en cada refresh
periódico). En Helm, comprueba el `secrets.licenseKeyring` y el mount `/licensing` en
los pods.

## Downgrade

Vuelve a las imágenes Community (o quita el `CENTRALOPS_LICENSE_TOKEN`) y levanta de
nuevo — la plataforma cae a Community por diseño, sin perder datos.

## ¿Necesitas ayuda?

Habla con **support@segark.com**.
