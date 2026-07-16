---
sidebar_position: 1
title: Despliegue con Docker Compose
description: Levanta CentralOps en un único host con Docker Compose — API, workers, base de datos y frontend en minutos.
---

# Despliegue con Docker Compose

La forma más rápida de ejecutar CentralOps es con **Docker Compose**. Levanta todo lo que la
plataforma necesita — API, workers de recolección (Celery), planificador, Redis, Postgres y el
frontend (Nginx) — en un único host. Ideal para **evaluación, desarrollo y producción
en single-host**.

:::tip[Cuándo usar]
- **Docker Compose** (esta página): 1 servidor, empezar rápido.
- **[Kubernetes / Helm](./kubernetes.md)**: producción a escala, multi-nodo, HA.
:::

## Requisitos previos

- **Docker** 24+ y el plugin **Docker Compose** v2 (`docker compose version`).
- ~2 vCPU / 4 GB de RAM para empezar.
- Puertos libres en el host: **3000** (HTTP) y **3443** (HTTPS).

## Paso a paso

### 1. Obtén el código

```bash
git clone https://github.com/SEGARK-oficial/CentralOps.git
cd CentralOps
```

### 2. Configura el entorno

El `docker compose` lee el `.env` **del directorio del archivo compose** (`compose/`) — no
de la raíz del repositorio. Copia el ejemplo allí y ajusta los secretos:

```bash
cp compose/.env.example compose/.env
```

Define en `compose/.env` (el compose **se niega a levantar** sin los dos primeros):

- **`POSTGRES_PASSWORD`** — contraseña de Postgres (obligatoria).
- **`REDIS_PASSWORD`** — contraseña de Redis; el AUTH de Redis se exige siempre (obligatoria).
- **`APP_MASTER_KEY`** — clave maestra de cifrado (≥ 32 caracteres). **Obligatoria**
  con `APP_ENV=production` (valor por defecto del ejemplo): sin ella el contenedor
  **aborta el arranque**. Genérala con `openssl rand -hex 32` (abajo). La generación
  automática (persistida en `/app/data/app_master_key`) solo existe en dev/test, con
  `APP_ENV=development`.

Genera secretos fuertes:

```bash
openssl rand -base64 24   # POSTGRES_PASSWORD y REDIS_PASSWORD
openssl rand -hex 32      # APP_MASTER_KEY
```

Para producción con HTTPS, mantén `APP_ENV=production` (valor por defecto del ejemplo —
fuerza `SESSION_SECURE_COOKIE=true`). La referencia completa está en
**[Configuración](./configuration.md)**.

### 3. Levanta el stack

Desde la **raíz del repositorio**, apuntando al archivo en `compose/`:

```bash
docker compose -f compose/docker-compose.yml up --build -d
```

El primer build compila el backend y el frontend — los siguientes arranques son casi
instantáneos.

### 4. Accede

- **HTTP:** `http://localhost:3000`
- **HTTPS:** `https://localhost:3443`

Si `certs/tls.crt` y `certs/tls.key` no existen, el contenedor **genera un certificado
autofirmado** automáticamente (el navegador te avisará; acéptalo para probar). Para un
certificado propio, monta los archivos en `certs/`.

### 5. Verifica el estado

La preparación real (Postgres + Redis) la verifica el **healthcheck del contenedor** — el
`/readyz` de la API no se publica en el borde. Comprueba el estado de los servicios:

```bash
docker compose -f compose/docker-compose.yml ps
```

Los servicios `centralops` (API) y `frontend` deben aparecer como **`healthy`**. Para
leer el JSON de preparación directo en la API:

```bash
docker compose -f compose/docker-compose.yml exec centralops \
  curl -fsS http://127.0.0.1:8000/readyz
# {"status":"ready","checks":{"db":"ok","redis":"ok"}}
```

Con todo `healthy`, continúa hacia el
**[Primer Inicio de Sesión](../getting-started/first-login.md)** para crear la cuenta de
administrador.

## Ejecutar imágenes listas (sin build)

Las imágenes oficiales se publican en GitHub Container Registry en cada release:
`ghcr.io/segark-oficial/centralops` (API) y `ghcr.io/segark-oficial/centralops-frontend`
(frontend). Para levantar **sin compilar localmente**, apunta el compose hacia ellas en
`compose/.env`:

```dotenv
IMAGE_NAME=ghcr.io/segark-oficial/centralops
IMAGE_TAG=v1.0.0   # fija una tag de release; evita `latest` en producción
```

Y levanta descargando las imágenes en vez de compilar:

```bash
docker compose -f compose/docker-compose.yml pull
docker compose -f compose/docker-compose.yml up -d
```

:::note

El stack necesita varios servicios (API, frontend, workers, Postgres, Redis) — no hay
una imagen única que ejecute todo en un solo contenedor. El `compose/docker-compose.yml`
es el que orquesta el conjunto, sea compilando (paso 3) o descargando las imágenes
listas.

:::

## Operación básica

Ejecutando desde la raíz del repositorio (todos los comandos apuntan a `compose/docker-compose.yml`):

:::warning[¿Instalación Enterprise? Incluye la overlay en TODOS los comandos]

Si tu stack ejecuta la edición **Enterprise**
([Upgrade a Enterprise](../editions/upgrade.md)), **todo** comando `docker compose` de
esta página debe incluir también `-f compose/docker-compose.ee.yml` — por ejemplo:
`docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml up -d`.
Un `up -d`/`pull` solo con el archivo base **degrada la stack a Community
silenciosamente** (la imagen EE y el mount del keyring de la licencia se eliminan).
Alternativa: haz la overlay permanente con
`COMPOSE_FILE=docker-compose.yml:docker-compose.ee.yml` en `compose/.env` y ejecuta los
comandos desde dentro de `compose/`, sin `-f`.

:::

| Acción | Comando |
|---|---|
| Ver logs de la API | `docker compose -f compose/docker-compose.yml logs -f centralops` |
| Ver logs del frontend | `docker compose -f compose/docker-compose.yml logs -f frontend` |
| Detener | `docker compose -f compose/docker-compose.yml down` |
| Actualizar imágenes | `docker compose -f compose/docker-compose.yml pull && docker compose -f compose/docker-compose.yml up -d` |
| Backup de la base de datos | `docker compose -f compose/docker-compose.yml exec postgres pg_dump -U centralops centralops > backup.sql` |

## Próximos pasos

- **[Configuración](./configuration.md)** — todas las variables de entorno.
- **[Primer Inicio de Sesión](../getting-started/first-login.md)** — crear el admin y el equipo.
- **[Quickstart](../getting-started/quickstart.md)** — conectar la primera fuente.
