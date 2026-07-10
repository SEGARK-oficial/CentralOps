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

:::tip Cuándo usar
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

Copia el ejemplo y define los valores obligatorios:

```bash
cp .env.example .env
```

Como mínimo, define en el `.env`:

- **`POSTGRES_PASSWORD`** — contraseña de la base de datos (obligatoria; sin ella, compose se niega a levantar).
- **`APP_MASTER_KEY`** — clave maestra de cifrado (≥ 32 caracteres). Si la dejas
  en blanco, el contenedor **genera una en el primer arranque** y la persiste en
  `/app/data/app_master_key` — guarda ese archivo.

Para producción con HTTPS, mantén `APP_ENV=production` y `SESSION_SECURE_COOKIE=true`
(valor por defecto del ejemplo). La referencia completa está en **[Configuración](./configuration.md)**.

### 3. Levanta el stack

```bash
cd compose
docker compose up --build -d
```

El primer build descarga las imágenes y compila el frontend — los siguientes son casi
instantáneos.

### 4. Accede

- **HTTP:** `http://localhost:3000`
- **HTTPS:** `https://localhost:3443`

Si `certs/tls.crt` y `certs/tls.key` no existen, el contenedor **genera un certificado
autofirmado** automáticamente (el navegador te avisará; acéptalo para probar). Para un
certificado propio, monta los archivos en `certs/`.

### 5. Verifica el estado

```bash
curl -fsS http://localhost:3000/readyz
# {"status":"ready","checks":{"db":"ok","redis":"ok"}}
```

`ready` significa que la API, la base de datos y Redis están activos. Ahora continúa hacia el
**[Primer Inicio de Sesión](../getting-started/first-login.md)** para crear la cuenta de
administrador.

## Imagen única (sin clonar el repo)

Las imágenes se publican en GitHub Container Registry. Para ejecutar sin clonar:

```bash
docker run -d --name centralops \
  -p 3000:80 -p 3443:443 \
  -e APP_MASTER_KEY="define-una-clave-de-al-menos-32-caracteres" \
  -e ENABLE_HTTPS=1 \
  -v centralops-data:/app/data \
  ghcr.io/segark-oficial/centralops:latest
```

Las configuraciones pueden venir por `--env-file .env` o por un archivo `/app/.env` montado en el
contenedor. Fija una **tag inmutable** (ej.: `v1.0.0`) en producción — evita `latest`.

## Operación básica

| Acción | Comando |
|---|---|
| Ver logs | `docker compose logs -f api` |
| Detener | `docker compose down` |
| Actualizar versión | `docker compose pull && docker compose up -d` |
| Backup de la base de datos | `docker compose exec postgres pg_dump -U centralops centralops > backup.sql` |

## Próximos pasos

- **[Configuración](./configuration.md)** — todas las variables de entorno.
- **[Primer Inicio de Sesión](../getting-started/first-login.md)** — crear el admin y el equipo.
- **[Quickstart](../getting-started/quickstart.md)** — conectar la primera fuente.
