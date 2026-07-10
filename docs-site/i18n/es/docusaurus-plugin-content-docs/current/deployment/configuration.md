---
sidebar_position: 3
title: Configuración (variables de entorno)
description: Referencia de las variables de entorno de CentralOps — qué es obligatorio, qué tiene un valor por defecto seguro y qué ajustar según el entorno.
---

# Configuración

CentralOps se configura mediante **variables de entorno**. En Docker Compose provienen del
archivo `.env`; en Kubernetes, de un `Secret`/`ConfigMap`. Esta página es la referencia —
empieza por el `.env.example`, que las incluye todas con comentarios.

:::info Mínimo obligatorio
Para desplegar en producción solo necesitas **`POSTGRES_PASSWORD`** e, idealmente, una
**`APP_MASTER_KEY`** definida por ti. El resto tiene un valor por defecto seguro.
:::

## Esenciales

| Variable | Valor por defecto | Descripción |
|---|---|---|
| `APP_MASTER_KEY` | *(generada y persistida en `/app/data/app_master_key`)* | Clave maestra de cifrado de los secretos (≥ 32 caracteres). **Defínela tú** en producción y guárdala de forma segura — perderla vuelve los secretos ilegibles. |
| `APP_ENV` | `production` | `production` exige HTTPS/cookie segura; usa `development` para desarrollo local sin TLS. |
| `APP_COMPANY_NAME` | `Sua Empresa` | Nombre mostrado en la interfaz. |
| `APP_COMPANY_PORTAL_NAME` | `Portal de Login` | Subtítulo de la pantalla de inicio de sesión. |

## Base de datos (Postgres)

| Variable | Valor por defecto | Descripción |
|---|---|---|
| `POSTGRES_PASSWORD` | *(vacío — **obligatorio**)* | Contraseña de Postgres. Sin valor, el compose se niega a arrancar. |
| `POSTGRES_USER` | `centralops` | Usuario de la base de datos. |
| `POSTGRES_DB` | `centralops` | Nombre de la base de datos. |
| `DATABASE_URL` | *(derivada de las variables anteriores)* | Sobrescríbela solo para usar un **Postgres externo/gestionado** (RDS, Neon…) o volver a SQLite en desarrollo: `sqlite:////app/data/app.db`. |

Docker Compose levanta un **Postgres 16** con volumen nombrado por defecto. En producción
seria, prefiere un Postgres gestionado y apunta `DATABASE_URL` hacia él.

## HTTPS y red (Nginx)

| Variable | Valor por defecto | Descripción |
|---|---|---|
| `ENABLE_HTTPS` | `0` | `1` habilita Nginx con TLS. Sin un certificado provisto en `certs/`, se genera uno autofirmado. |
| `NGINX_SERVER_NAME` | `_` | Valor de `server_name` en Nginx (usa tu dominio en producción, ej.: `centralops.suaempresa.com`). |

## Sesión y seguridad

| Variable | Valor por defecto | Descripción |
|---|---|---|
| `SESSION_SECURE_COOKIE` | `true` | Usa `true` cuando el acceso principal sea HTTPS (obligatorio con `APP_ENV=production`). |
| `DEBUG_REQUESTS` | `0` | `1` registra `debug_requests.log` con las llamadas a APIs externas (diagnóstico; desactívalo en producción). |

## Secretos de las integraciones

Las credenciales de cada integración se cifran en reposo. El proveedor de cifrado por defecto es
**`local_fernet`** (AES derivado de la `APP_MASTER_KEY`). Detalles y rotación en
**[Secretos y clave maestra](../administration/secrets-and-master-key.md)**.

## Buenas prácticas

- **Fija las versiones:** usa una etiqueta inmutable de imagen (ej.: `v1.0.0`) en producción, no `latest`.
- **APP_MASTER_KEY externa:** defínela mediante Secret y haz copia de seguridad — es la clave de todo.
- **HTTPS siempre:** `ENABLE_HTTPS=1` + `SESSION_SECURE_COOKIE=true` + `NGINX_SERVER_NAME` con tu dominio.
- **Postgres gestionado** en producción (backup, HA y parcheo a cargo del proveedor).
