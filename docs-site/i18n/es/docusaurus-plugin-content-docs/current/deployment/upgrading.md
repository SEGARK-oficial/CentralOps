---
sidebar_position: 4
title: Actualizar de versión
description: "Paso a paso para llevar CentralOps de una versión a la más reciente (p. ej. 1.1.0 → 2.0.0) — la mecánica genérica en Compose y Helm, la migración idempotente en el arranque, verificación y rollback — más las notas de la versión 2.0.0."
---

# Actualizar de versión

Pasar de una **versión** a la más reciente (p. ej. `1.1.0` → `2.0.0`) es, en la mecánica,
una operación de rutina: cambias la **tag de la imagen**, haces pull de la nueva imagen y
recreas los servicios. No hay reinstalación, no hay paso manual de migración, y tus
**datos se conservan**. Esta página cubre la mecánica genérica (vale para cualquier
versión) y trae, al final, las **notas de la versión** con lo que cambia en cada release.

:::danger[2.0.0 es un major con un breaking change]

La **2.0.0** sube el número **mayor** a propósito: **elimina la superficie de Alertas** (la
ruta `/alerts`, los endpoints de alerts de la API, el Accept `v1` de `/dashboard/summary` y
la herramienta MCP `list_integration_alerts`). **Los datos y el esquema se conservan** — lo
que cambia es el **contrato de lectura**. Si tienes bookmarks, automatizaciones o
integraciones que golpean esas rutas, **migralas antes de actualizar** (detalles en
[Notas de la versión → 2.0.0](#200)). La **ingesta** de alertas Sophos/Wazuh no cambia.

:::

:::note[Esto es distinto de "Actualización a Enterprise"]

Esta página trata de subir de **versión** (p. ej. `1.1.0` → `2.0.0`), dentro de la misma
edición. Para cambiar de **edición** — Community → Enterprise, activando los módulos MSSP
con tu licencia — consulta **[Actualización a Enterprise](../editions/upgrade.md)**. Los
dos procesos son independientes: actualizas la versión de una stack Community o Enterprise
exactamente de la misma forma.

:::

## Antes de empezar

- **Haz un backup de la base de datos.** Un `pg_dump` rápido antes de cualquier upgrade
  (`docker compose -f compose/docker-compose.yml exec postgres pg_dump -U centralops centralops > backup.sql`).
- **Lee las [notas de la versión](#notas-de-la-versión)** de destino — sobre todo los
  cambios que rompen compatibilidad (*breaking changes*).
- **Fija una tag inmutable** (con `sha`) en producción, para saber exactamente qué está
  corriendo y poder hacer rollback de forma fiable.

## Cómo se identifican las versiones

Las imágenes de cada release reciben dos tags — una **móvil** (acompaña la versión) y una
**inmutable** (nunca cambia de contenido). En producción, **fija la inmutable**.

| Edición | Tag de release (móvil) | Tag inmutable (fija en producción) | Tag extra |
|---|---|---|---|
| **Community** | `vX.Y.Z` — p. ej. `v2.0.0` | `sha-<shortsha>` — p. ej. `sha-a1b2c3d` | — |
| **Enterprise** | `vX.Y.Z-ee` — p. ej. `v2.0.0-ee` | `vX.Y.Z-ee.<sha>` — p. ej. `v2.0.0-ee.9f8e7d6` | `core-<coresha>` |

- La **tag de release** es ideal para seguir la versión, pero puede re-publicarse — malo
  para la reproducibilidad.
- La **tag inmutable** (con `<sha>`) es la misma imagen para siempre — **úsala en
  producción** y guarda la tag de la versión anterior para el rollback.
- En **Community**, la inmutable es la tag `sha-<shortsha>` (p. ej. `sha-a1b2c3d`). Si
  prefieres no fijar por commit, sigue la tag de release móvil `vX.Y.Z` o mantén tu propia
  tag estable (p. ej. `production`).
- Las **imágenes Enterprise son privadas** en GHCR y exigen `docker login` con la
  credencial de pull de tu suscripción — consulta
  **[Actualización a Enterprise](../editions/upgrade.md)**.

## Docker Compose

### Community

En `compose/.env`, apunta a la nueva tag:

```dotenv
IMAGE_NAME=ghcr.io/segark-oficial/centralops
IMAGE_TAG=sha-a1b2c3d   # la tag inmutable de la nueva versión
```

Haz pull de las imágenes y recrea los servicios (desde la raíz del repositorio):

```bash
docker compose -f compose/docker-compose.yml pull
docker compose -f compose/docker-compose.yml up -d
```

No hay build local — las imágenes vienen listas del registry.

### Enterprise

En una stack Enterprise, cambia las **dos** imágenes EE en `compose/.env`:

```dotenv
CENTRALOPS_EE_IMAGE=ghcr.io/segark-oficial/centralops-ee:v2.0.0-ee.9f8e7d6
CENTRALOPS_WEB_EE_IMAGE=ghcr.io/segark-oficial/centralops-ee-frontend:v2.0.0-ee.9f8e7d6
```

Y recrea **siempre con los dos archivos** (`-f` base + `-f` overlay EE):

```bash
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml pull
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml up -d
```

:::warning[Enterprise: incluye la overlay en TODOS los comandos]

Un `up -d`/`pull` solo con el archivo base **degrada la stack a Community
silenciosamente** (la imagen vuelve a ser la CE y el mount del keyring de la licencia
desaparece). Si aún no lo hiciste, haz la overlay permanente con
`COMPOSE_FILE=docker-compose.yml:docker-compose.ee.yml` en `compose/.env`. La mecánica
completa está en **[Actualización a Enterprise](../editions/upgrade.md)**.

:::

## Kubernetes (Helm)

Ejecuta un `helm upgrade` cambiando solo la **tag** de la imagen.

**Community:**

```bash
helm upgrade centralops kubernetes/helm/centralops -n centralops \
  --set image.tag=sha-a1b2c3d \
  --set frontendImage.tag=sha-a1b2c3d \
  --reuse-values
```

**Enterprise** — mantén también los repositorios EE:

```bash
helm upgrade centralops kubernetes/helm/centralops -n centralops \
  --set image.repository=ghcr.io/segark-oficial/centralops-ee \
  --set image.tag=v2.0.0-ee.9f8e7d6 \
  --set frontendImage.repository=ghcr.io/segark-oficial/centralops-ee-frontend \
  --set frontendImage.tag=v2.0.0-ee.9f8e7d6 \
  --reuse-values
```

Helm hace un rollout gradual — la API, el frontend y los workers son Deployments
separados. Síguelo con:

```bash
kubectl -n centralops rollout status deploy/centralops-api
```

:::tip[Si versionas tus valores en un archivo]

Prefiere editar `image.tag`/`frontendImage.tag` en tu `values.override.yaml` y ejecutar
`helm upgrade centralops kubernetes/helm/centralops -n centralops -f values.override.yaml`
— así el estado deseado queda en Git en vez de `--set` en la línea de comandos. Los
`--set` exactos para una stack Enterprise están en
**[Actualización a Enterprise](../editions/upgrade.md)**.

:::

## Qué pasa con tus datos

En el primer arranque de la nueva versión, la API ejecuta una **migración/seed ligera e
idempotente** — **no hay paso manual de Alembic** en este release. Tus **datos se
conservan**:

- Las definiciones de mapping **existentes** (las que ya tienen una versión activa) **no
  se sobrescriben**.
- Solo las definiciones **vacías** reciben una `v1` — así aparecen los **nuevos defaults**
  de un release (ver [notas de la versión](#notas-de-la-versión)).
- Volver a ejecutar la migración (en un restart) **no cambia nada** — es idempotente.

Como el seed es **aditivo y no destructivo**, el rollback a la versión anterior es seguro
(ver abajo). Si, justo después del upgrade, la plataforma parece no disponible por un
instante mientras los servicios arrancan, espera unos segundos — la nueva versión necesita
un corto período para quedar totalmente lista.

## Verifica

1. **Edición y arranque** — el log de arranque muestra la edición resuelta (y confirma que
   la API subió en la nueva versión):

   ```bash
   docker compose -f compose/docker-compose.yml logs centralops | grep edition=
   # edition=community        (o "edition=enterprise plan=... features=..." en una stack EE)
   ```

2. **Salud general** — abre **Operación → Flujo de datos** (`/flow`) y **Normalización →
   Salud del Pipeline** (`/pipeline-health`) y confirma que los eventos siguen fluyendo
   con normalidad.

:::note[Cómo NO verificar versión/edición]

`/readyz` solo reporta **prontitud** (db/redis) — **no** la edición ni la versión. El
endpoint `/api/edition` existe, pero **exige autenticación**. Para la edición, usa el log
de arranque (`edition=`) o la pantalla **Configuración → Licencia**.

:::

## Rollback

Como este release **no tiene migración destructiva**, volver es seguro:

- **Compose:** vuelve a apuntar la **tag inmutable anterior** en `compose/.env` y ejecuta
  `pull` + `up -d` (con los dos `-f` en una stack Enterprise).
- **Helm:** `helm rollback centralops` (vuelve a la revisión anterior) o
  `helm upgrade ... --set image.tag=<tag-anterior>`.

Los datos escritos por la nueva versión siguen siendo legibles por la anterior — los
cambios de esquema son aditivos.

## Notas de la versión

Cada versión agrega una sección aquí. Lee la de tu versión de destino **antes** de
actualizar.

### Próxima versión

:::note[Todavía no publicada]
Los cambios de abajo ya están en el código, pero aún no salieron en un tag. Cuando la
versión se publique, esta sección pasa a llevar su número.
:::

**Filtro de recolección — nace apagado.** Las integraciones cuyo proveedor permite
restringir la consulta ganaron un **filtro de recolección**: el descarte pasa a ocurrir en
la consulta hecha al proveedor, en vez de después de recolectar y normalizar. Hoy **Wazuh
(detecciones)** es la integración que lo ofrece, con un nivel mínimo de regla.

**Ninguna instalación cambia de comportamiento al actualizar.** El filtro nace en el valor
que no corta nada, y la consulta enviada al proveedor es **idéntica** a la de la versión
anterior mientras nadie abra la pantalla. No hay nada que configurar ni nada que revertir.

Existe para un caso concreto: cuando el ruteo descarta la mayor parte de lo que entra, el
recolector está gastando cada ciclo transportando ruido — y esa es la causa de
recolecciones que no alcanzan el presente. Lee
[Filtro de recolección](../pipelines/collection-filters) antes de encenderlo: lo que se
filtra en el origen **nunca entra en la plataforma** (no aparece en la captura en vivo, no
genera campo nuevo en el Drift Explorer, no queda disponible para una ruta futura), y
encenderlo o apagarlo **no es retroactivo**.

**Los ciclos concurrentes del mismo stream ahora se saltan.** Cuando un ciclo de
recolección tarda más que el intervalo agendado, el ciclo siguiente de ese mismo
`(integración, stream)` se **salta** en vez de correr en paralelo. Si monitoreas los
workers, verás **un** ciclo donde antes veías dos o tres simultáneos.

:::note[Esto no es una regresión de throughput]
Los ciclos simultáneos leían la **misma** posición de recolección y buscaban **los mismos**
eventos — en producción, ciclos concurrentes llegaron a terminar con 34 ms de diferencia
sobre el mismo lote. Solo uno avanzaba la posición; el resto era trabajo tirado a la basura
que además presionaba la fuente y hacía cada ciclo más lento. Recolectar dejó de hacerse
por duplicado; la cantidad de eventos recolectados por hora no baja.

El contador `collector_cycles_skipped_locked_total` muestra cuántos ciclos se saltaron.
Subiendo de forma sostenida, indica que el ciclo pasó a durar más que el intervalo
agendado — es decir, hay acumulación. Ver
[Eventos que llegan horas después](../runbooks/collection-lag-backlog).
:::

**Salud del Pipeline: retraso de los datos.** La tarjeta de cada integración pasa a
mostrar, además del tiempo desde la última recolección, el **Retraso de los datos** — de
cuándo es el evento más reciente que la recolección ya trajo. Son preguntas distintas: la
primera responde "¿la recolección está corriendo?", la segunda responde "¿lo que estoy
viendo es de ahora?".

:::warning[Una tarjeta que se ponga amarilla tras el upgrade probablemente ya estaba atrasada antes]
El indicador anterior medía solo el tiempo desde la última recolección exitosa — y ese
número se pone en cero en cada ciclo que termina sin error, **incluso cuando el ciclo
procesó eventos de ayer**. Un recolector que estaba 15 horas atrás reportaba un retraso de
`0 s` y estado **Saludable**.

Al actualizar, ese punto ciego se cierra. Una tarjeta que se ponga amarilla (o que pase a
exhibir horas en el Retraso de los datos) justo después del upgrade casi seguro **ya estaba
atrasada antes** — la actualización no creó el retraso, lo hizo visible. Trátalo como
diagnóstico, no como regresión, y sigue
[Eventos que llegan horas después](../runbooks/collection-lag-backlog).
:::

La tarjeta solo se pone **amarilla por backlog** cuando valen las **dos** condiciones al
mismo tiempo: el último ciclo terminó en el tope de eventos **y** el Retraso de los datos de
ese stream pasa de 30 minutos. Retraso de los datos alto por sí solo no cambia el color —
un stream sin eventos mantiene la posición detenida a propósito. Detalles en
[Salud del Pipeline](../operations/pipeline-health).

### 2.0.0

La **2.0.0** es un **major**: elimina la superficie de Alertas — por eso el salto de `1.x`
a `2.0`. Es el **único** cambio que rompe compatibilidad; el resto son features,
correcciones y mejoras de rendimiento (sin acción necesaria).

:::danger[Breaking: la superficie de Alertas fue ELIMINADA]

El área de **Alertas** fue **eliminada por completo** en esta versión. El cambio **sí
está** en el changelog automático (marcado como `⚠ BREAKING CHANGE`) — es lo que hizo que
el release pasara a `2.0.0`. Lo que sale:

- La ruta **`/alerts`** deja de existir (bookmarks antiguos → **404**).
- Los **endpoints de alerts de la API** fueron eliminados.
- El camino **Accept v1** de `GET /dashboard/summary`
  (`application/vnd.centralops.v1+json`) fue eliminado.
- La herramienta **MCP `list_integration_alerts`** fue eliminada.

**Qué hacer:** el triaje ahora es vendor-neutral, vía **Operación → Investigaciones /
Búsqueda federada** y **Detecciones**. Si tienes automatizaciones o integraciones que
llaman a los endpoints de alerts (o al camino Accept v1 de `/dashboard/summary`),
**migralas** a esos caminos antes de actualizar.

La **ingesta** de `sophos.alert` (el dato que entra en el pipeline) **no cambia** — solo
salió la superficie de lectura de "alertas".

:::

**Novedades (nada que configurar — ya vienen activas):**

- **Exportación CSV robusta de la Búsqueda federada**, con etiquetas localizadas
  (PT/EN/ES) — en **Operación → Investigaciones**.
- **Mapa de flujo `/flow` que escala.** El **Flujo de datos** (Operación → Flujo de datos)
  colapsa columnas densas en un nodo **"+N"** expansible y se ajusta solo a la pantalla
  (fit-to-view), con resaltado de camino al pasar el mouse — legible incluso con decenas de
  fuentes/rutas/destinos.
- **Etiquetas de condición de ruta legibles.** En el editor de rutas, los operadores de
  condición aparecen con nombres humanos y localizados en vez de la etiqueta cruda.
- **Validación de mapping de detección de Wazuh** + corrección de una definición de seed
  faltante.

**Metering de costo activado por defecto.** `COST_METERING_ENABLED` ahora viene en
**`true`** por defecto. Con eso, el card **"Reducción de volumen y costo"** empieza a
aparecer en **Operación → Flujo de datos**: en Community muestra el volumen, el porcentaje
y los bytes ahorrados; en Enterprise suma el valor en **US$** (a partir del `cost_per_gb`
configurado en cada destino). Para desactivarlo, define `COST_METERING_ENABLED=false`.

**Correcciones operativas** (informativo — nada que hacer):

- Los colectores ya no entran en **crash-loop de RedBeat** (lock, límite de bucle y
  registro idempotente del scheduler corregidos). Ver también
  **Observabilidad** (Operación → Observabilidad) para seguir la salud del Beat.
- El **soft-timeout de recolección** ya no envenena el pool de conexiones de la base de
  datos (dispose del pool + inicialización temprana evitan `UnboundLocalError`).
- Un `SESSION_SECURE_COOKIE` **vacío** ya no tumba el arranque; se corrigió el anclaje de
  ruta del recurso OCSF.
- Los IDs de **service account (shim)** se sanitizan — sin más violación de FK en
  auditoría/mapping.
- La **validación OCSF** vuelve a ejecutarse en la imagen compilada.

**Rendimiento:** la medición de volumen de la ingesta pasó a ser **por lote**
(`InVolumeAccumulator`), reduciendo la latencia de I/O en el Redis del hot-path.

**Nuevos defaults de mapping.** Esta versión seedea definiciones por defecto para
**Wazuh** y para **CrowdStrike, Entra ID, Okta y CloudTrail**. Solo rellenan
**definiciones vacías** — los mappings que ya personalizaste no se tocan (ver
[qué pasa con tus datos](#qué-pasa-con-tus-datos)).

## Próximos pasos

- **[Actualización a Enterprise](../editions/upgrade.md)** — cambiar de **edición**
  (Community → Enterprise), no de versión.
- **[Deploy con Docker Compose](./docker-compose.md)** — operación de la stack single-host.
- **[Deploy con Kubernetes (Helm)](./kubernetes.md)** — rollout, HPA y rollback en el
  clúster.
- **[Configuración](./configuration.md)** — todas las variables de entorno.
