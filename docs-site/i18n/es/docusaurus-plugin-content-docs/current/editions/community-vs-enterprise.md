---
sidebar_position: 1
title: Ediciones — Community vs Enterprise
description: Lo que CentralOps Community (open-source) ya ofrece y lo que la edición Enterprise agrega para MSSPs y operaciones a escala.
---

# Ediciones: Community vs Enterprise

CentralOps sigue un modelo **open-core**. La edición **Community** de este repositorio es
open-source (AGPLv3), completa y utilizable en producción por tu cuenta. La edición
**Enterprise** agrega las funciones que escalan con el **tamaño y la madurez** de la
organización — pensada para MSSPs y SOCs multi-tenant.

## Lo que siempre está libre (Community)

La seguridad de base **nunca** queda detrás de un muro de pago:

- **Ingesta de todas las fuentes** — Sophos, Microsoft Defender, Wazuh, NinjaOne y
  cualquier proveedor mediante el registry de plugins; syslog, APIs, S3, Kafka y push de edge.
- **Normalización OCSF** con la DSL versionada (CML), dry-run y rollback.
- **Enrutamiento hacia 14+ destinos** (SIEMs, data lakes) con cuarentena y drift detection.
- **SSO / OIDC (Entra)** + **RBAC** — sin "impuesto de SSO".
- **Cifrado / KMS** y **redacción de PII** en el pipeline.
- **Auditoría append-only** de base y el **servidor MCP** para automatización.

## Lo que agrega Enterprise

| Función | Por qué es Enterprise |
|---|---|
| **Multi-tenancy jerárquica / reseller (MSSP)** | Jerarquía de tenants y programa de reventa — escala con el tamaño de la operación. |
| **Búsqueda federada cross-org / asíncrona** | Búsqueda activa que cruza organizaciones y fuentes. |
| **Audit & compliance cross-tenant** | Auditoría tamper-evident (WORM), retención prolongada, export firmado. |
| **HA / fleet** | Alta disponibilidad, multi-node y orquestación de flota. |

## Cómo funciona el gate

La separación es **honesta y verificable**:

- El artefacto Community **nunca** contiene el código Enterprise — los módulos pagos se
  distribuyen como una **imagen separada, activada por licencia**.
- La licencia es un **JWT firmado (EdDSA)** verificado **offline** contra un keyring
  **público** que **tú montas junto al producto** — la clave pública (`<kid>.pem`) se
  entrega con la licencia (descarga en el portal); el producto **no embebe** ninguna
  clave. Sin una licencia válida, las funciones Enterprise quedan **bloqueadas** — nada
  se rompe, la imagen sigue ejecutándose como Community.
- La licencia tiene **expiración corta** y una **lista de revocación** offline: suscribir activa,
  churn desactiva.

Este es el mismo patrón de proyectos como GitLab y Grafana: el core es genuinamente abierto y
útil, y la monetización proviene de las capacidades que escalan con la organización.

## ¿Listo para Enterprise?

Mira el paso a paso en **[Upgrade a Enterprise](./upgrade.md)** o escribe a
**support@segark.com**.
