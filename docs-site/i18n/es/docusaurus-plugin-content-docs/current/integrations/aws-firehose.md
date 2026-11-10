---
sidebar_position: 8
title: AWS vía Firehose (push)
description: Recibe WAF, CloudTrail, GuardDuty, Security Hub y CloudWatch Logs casi en tiempo real, empujados por la propia AWS a través de un stream de Amazon Data Firehose
---

# AWS vía Firehose (push)

Las otras integraciones de AWS **buscan** los logs: leen el bucket S3 o llaman a la API en cada ciclo. Esta integración **recibe**: la propia AWS empuja los logs a CentralOps a través de un stream de **Amazon Data Firehose** con destino **HTTP endpoint**. El retraso baja del ciclo de recolección más la entrega en el bucket (5 a 15 minutos en CloudTrail) al buffer de Firehose (60 segundos o menos).

## Cuándo usarla

- **La latencia importa.** Detección sobre WAF o sobre CloudTrail que no puede esperar 15 minutos.
- **Sin acceso de lectura a la cuenta.** El cliente no necesita crear un rol ni abrir un bucket: solo configura Firehose apuntando a CentralOps.
- **El log ya pasa por Firehose.** WAF con destino Firehose, suscripciones de log group de CloudWatch Logs y reglas de EventBridge.

Cuando la latencia no importa y ya tienes el rol de lectura, las integraciones de pull ([CloudTrail](./aws-cloudtrail.md), [WAF](./aws-waf.md), [Security Hub y GuardDuty](./aws-findings.md)) son más simples: no hay stream de Firehose que mantener ni costo de Firehose.

## Cómo funciona

```
web ACL (WAF) ───────────────────────────────┐
log group ─► suscripción de CloudWatch Logs ─┤
EventBridge (GuardDuty, Security Hub, API) ──┼─► stream de Firehose ─► HTTP endpoint ─► CentralOps
                                             │      (buffer, gzip, retry, backup S3)
                                             ┘
```

Cada tipo de log tiene su propio **stream** en la integración y su propia **URL**:

| Stream | URL | Qué entra | Tipo de evento | Clase OCSF |
|---|---|---|---|---|
| `waf` | `/api/ingest/firehose/waf` | logging de la web ACL directo a Firehose, o un log group de WAF suscrito | `aws_firehose.waf` | 4002 HTTP Activity |
| `cloudtrail` | `/api/ingest/firehose/cloudtrail` | log group del trail suscrito, o regla de EventBridge "AWS API Call via CloudTrail" | `aws_firehose.cloudtrail` | 6003 API Activity |
| `guardduty` | `/api/ingest/firehose/guardduty` | regla de EventBridge "GuardDuty Finding" | `aws_firehose.guardduty` | 2004 Detection Finding |
| `securityhub` | `/api/ingest/firehose/securityhub` | regla de EventBridge "Security Hub Findings - Imported" (formato ASFF) | `aws_firehose.securityhub` | 2004 Detection Finding |
| `cloudwatch_logs` | `/api/ingest/firehose/cloudwatch_logs` | cualquier log group suscrito | `aws_firehose.cloudwatch_logs` | 0 Base Event |

Los mappings son los mismos de las integraciones de pull. CentralOps deshace, al recibir, lo que agrega el camino por Firehose:

- **base64 y gzip.** El registro de Firehose viene en base64. La suscripción de CloudWatch Logs además lo comprime en gzip, y CentralOps lo descomprime. La opción de descompresión de Firehose puede quedar desactivada.
- **Envoltorio de CloudWatch Logs.** Cada `logEvent` se convierte en un evento. El mensaje de control que CloudWatch envía al crear la suscripción se descarta.
- **Envoltorio de EventBridge.** Entra solo el `detail`: el hallazgo de GuardDuty, cada ítem de `findings` de Security Hub, el registro de CloudTrail.
- **Formato de GuardDuty.** En EventBridge el hallazgo viene en `camelCase` (`accountId`), en la API en `PascalCase` (`AccountId`). CentralOps lo convierte, y el mismo mapping sirve para los dos.
- **Origen de WAF.** El bloque `centralops` recibe `source: firehose`, la cuenta y la región sacadas del `webaclId` y el nombre del stream de Firehose.

## Requisitos previos

- **CentralOps accesible desde internet por HTTPS, en el puerto 443.** Firehose solo entrega a ese tipo de dirección.
- **En la cuenta de AWS, permiso para crear** el stream de Firehose, un bucket S3 de respaldo y los roles IAM que usa AWS para entregar en el stream. La consola crea esos roles sola; la CLI los necesita listos (consulta [Roles que usa AWS](#papéis-que-a-aws-usa)).
- **Ningún acceso de CentralOps a la cuenta.** Aquí es AWS quien envía: no hay rol de CentralOps ni External ID.

## Configuración

El camino es siempre el mismo: **(1)** crear la integración y obtener el token, **(2)** crear un stream de Firehose que entregue en CentralOps, **(3)** enviar el log a ese stream.

### 1. Crear la integración y obtener el token

1. En CentralOps, ve a **Recolecta -> Integraciones** y agrega **AWS via Firehose (push)**. No hay credencial de AWS que indicar.
2. Abre la integración. En el panel **Ingesta push**, haz clic en **Emitir token** y copia el token en ese momento: aparece una sola vez.
3. El mismo panel lista la **URL de cada tipo de log** (`…/api/ingest/firehose/waf`, `…/cloudtrail` y las demás). Usas una URL por stream.

Un solo token sirve para todos los streams de la integración. Si se filtra, haz clic en **Rotar token** y actualiza la access key de los streams.

### 2. Crear el stream de Firehose

Crea **un stream para cada tipo de log**. Por la consola:

1. Abre **Amazon Data Firehose** y haz clic en **Create Firehose stream** (crear stream de Firehose).
2. En **Source**, elige **Direct PUT**. En **Destination**, elige **HTTP Endpoint**.
3. En **Firehose stream name**, ponle un nombre. **Para WAF, el nombre tiene que empezar con `aws-waf-logs-`** (p. ej.: `aws-waf-logs-centralops`).
4. En **Destination settings** (configuración del destino), completa:

   | Campo | Valor |
   |---|---|
   | HTTP endpoint name | `CentralOps` |
   | HTTP endpoint URL | la URL del tipo de log, copiada del panel **Ingesta push** |
   | Authentication | **Use access key**, y pega el token. (O **Use AWS Secrets Manager**, con un secreto en el formato `{"api_key": "<token>"}`.) |
   | Content encoding | **GZIP** |
   | Retry duration | `3600` segundos (el máximo es 7200) |
   | Buffer hints | **Buffer size** de 1 a 5 MiB y **Buffer interval** de 60 segundos |

5. En **Backup settings** (configuración de respaldo), elige **Failed data only** y un bucket S3 tuyo (o crea uno ahí mismo). Ese bucket guarda lo que CentralOps no haya podido recibir después de agotar el retry. No se pierde nada.
6. En **Advanced settings → Service access**, deja **Create or update IAM role**. La consola crea el rol que usa Firehose para escribir en el bucket de respaldo.
7. Haz clic en **Create Firehose stream** y espera el estado **Active**.

Por la AWS CLI, el stream de WAF queda así. El panel de la integración muestra el mismo comando, ya con la URL:

```bash
aws firehose create-delivery-stream \
  --delivery-stream-name aws-waf-logs-centralops \
  --delivery-stream-type DirectPut \
  --http-endpoint-destination-configuration '{
    "EndpointConfiguration": {"Url": "https://<host>/api/ingest/firehose/waf", "Name": "CentralOps", "AccessKey": "<token>"},
    "RequestConfiguration": {"ContentEncoding": "GZIP"},
    "BufferingHints": {"SizeInMBs": 1, "IntervalInSeconds": 60},
    "RetryOptions": {"DurationInSeconds": 3600},
    "S3BackupMode": "FailedDataOnly",
    "RoleARN": "arn:aws:iam::<cuenta>:role/<rol-de-firehose>",
    "S3Configuration": {
      "RoleARN": "arn:aws:iam::<cuenta>:role/<rol-de-firehose>",
      "BucketARN": "arn:aws:s3:::<bucket-de-backup>"
    }
  }'
```

### 3. Enviar el log al stream

**WAF** (stream `waf`). El stream tiene que estar en la misma cuenta y en la misma región que la web ACL. Para CloudFront, usa `us-east-1`.

1. Abre **WAF & Shield → Web ACLs**, elige la región y la web ACL.
2. En la pestaña **Logging and metrics**, haz clic en **Enable** (o **Edit**) en **Logging**.
3. En **Logging destination**, elige **Amazon Data Firehose stream** y selecciona el stream `aws-waf-logs-…`.
4. Guarda. WAF crea solo el rol de servicio `AWSServiceRoleForWAFV2Logging`. Para eso, quien guarda necesita el permiso `iam:CreateServiceLinkedRole`.

Por la CLI:

```bash
aws wafv2 put-logging-configuration --logging-configuration \
  ResourceArn=<arn-da-web-acl>,LogDestinationConfigs=<arn-do-stream>
```

**CloudTrail** (stream `cloudtrail`). CloudTrail no escribe directo en Firehose: tiene que enviar los eventos a un log group de CloudWatch Logs, que los pasa al stream.

1. Abre **CloudTrail → Trails → tu trail**. En **CloudWatch Logs**, haz clic en **Edit**, activa **Enabled** y deja que la consola cree el log group y el rol. Guarda.
2. Abre **CloudWatch → Log groups** y marca el log group del trail.
3. En **Actions → Subscription filters** (filtros de suscripción), elige **Create Amazon Data Firehose subscription filter**.
4. Selecciona el stream de Firehose. En **Grant permission**, elige el rol que deja a CloudWatch Logs escribir en el stream (consulta [Roles que usa AWS](#papéis-que-a-aws-usa)).
5. En **Subscription filter pattern**, déjalo vacío (envía todo). Ponle un nombre al filtro y haz clic en **Start streaming**.

Otra opción es una regla de EventBridge con el patrón `{ "detail-type": ["AWS API Call via CloudTrail"] }` y el stream como destino, como en GuardDuty más abajo. Solo recibe los eventos de escritura (las llamadas de lectura no pasan por EventBridge).

**Cualquier otro log group** (stream `cloudwatch_logs`). Los pasos 2 a 5 de CloudTrail, en el log group que quieras.

Por la CLI, la suscripción de un log group queda así:

```bash
aws logs put-subscription-filter \
  --log-group-name <log-group> \
  --filter-name centralops \
  --filter-pattern "" \
  --destination-arn <arn-do-stream> \
  --role-arn arn:aws:iam::<cuenta>:role/<rol-de-cloudwatch-logs>
```

**GuardDuty y Security Hub** (streams `guardduty` y `securityhub`). Los hallazgos pasan por EventBridge:

1. Abre **Amazon EventBridge → Rules** y haz clic en **Create rule**. Elige **Rule with an event pattern** (regla con un patrón de eventos).
2. En **Event pattern**, elige **Custom pattern (JSON editor)** y pega el patrón del servicio:

   ```json
   { "source": ["aws.guardduty"], "detail-type": ["GuardDuty Finding"] }
   ```

   ```json
   { "source": ["aws.securityhub"], "detail-type": ["Security Hub Findings - Imported"] }
   ```

3. En **Target**, elige **AWS service → Firehose stream** y selecciona el stream del servicio.
4. En **Execution role**, deja **Create a new role for this specific resource**. Crea la regla.

Crea la regla en cada región donde el servicio esté activado. Con la agregación entre regiones de Security Hub, basta con la región de agregación.

### 4. Verificar

- Después de 1 a 2 minutos, los eventos aparecen en **Recolecta -> Colectores**, en el stream de la integración.
- En Firehose, la pestaña **Monitoring** del stream muestra las entregas. Si algo falla, la pestaña **Destination error logs** muestra el mensaje de CentralOps (por ejemplo, token inválido o lote demasiado grande).

### Roles que usa AWS {#papéis-que-a-aws-usa}

La consola crea los roles de abajo sola. Por la CLI, créalos antes. Ninguno da acceso a CentralOps: son permisos de la propia AWS para entregar los logs en el stream.

| Quién escribe | Confía en | Permisos |
|---|---|---|
| Firehose, en el bucket de respaldo | `firehose.amazonaws.com` | `s3:AbortMultipartUpload`, `s3:GetBucketLocation`, `s3:GetObject`, `s3:ListBucket`, `s3:ListBucketMultipartUploads` y `s3:PutObject` en el bucket de respaldo |
| CloudWatch Logs, en el stream | `logs.amazonaws.com` | `firehose:PutRecord` y `firehose:PutRecordBatch` en el stream |
| EventBridge, en el stream | `events.amazonaws.com` | `firehose:PutRecord` y `firehose:PutRecordBatch` en el stream |
| WAF, en el stream | rol de servicio `AWSServiceRoleForWAFV2Logging` | creado por WAF |

Ejemplo del rol de CloudWatch Logs (cambia región, cuenta y stream):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "logs.amazonaws.com" },
      "Action": "sts:AssumeRole",
      "Condition": { "StringLike": { "aws:SourceArn": "arn:aws:logs:us-east-1:111122223333:*" } }
    }
  ]
}
```

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["firehose:PutRecord", "firehose:PutRecordBatch"],
      "Resource": "arn:aws:firehose:us-east-1:111122223333:deliverystream/centralops-cloudtrail"
    }
  ]
}
```

El primer documento es la *trust policy* (política de confianza) del rol. El segundo es el permiso.

## Entrega, retry y qué pasa ante una falla

CentralOps solo responde 200 después de que los eventos quedan escritos en el data-plane. Si no, Firehose reintenta el mismo lote hasta que se acaba el **retry duration** y entonces escribe el lote en el bucket de respaldo. No se pierde nada en silencio.

| Situación | Respuesta | Qué hace Firehose |
|---|---|---|
| Lote escrito | 200 | lo cuenta como entregado |
| Token incorrecto o revocado | 401 | reintenta y después escribe en el respaldo |
| Stream que la integración no tiene | 404 | reintenta y después escribe en el respaldo |
| Data-plane (tópico raw) caído | 503 | reintenta hasta que el data-plane vuelva |
| Lote demasiado grande (cuerpo de más de 16 MiB, más de 64 MiB descomprimido, o más de 50.000 eventos) | 500 con la instrucción de bajar el buffer | reintenta y después escribe en el respaldo |

CentralOps **nunca responde 413** a Firehose. Para Firehose, 413 es una falla permanente y el lote **no** va al respaldo. Si el mensaje "lote demasiado grande" aparece en los logs de error del stream, baja el **buffer size**.

Un registro que no se decodifica (base64 inválido, gzip corrupto, texto que no es JSON en un stream que espera JSON) no mejora con el retry. Se cuenta en `collector_ingest_malformed_total` (motivo `parse`), se avisa en el log y se salta, y el resto del lote sigue.

## Deduplicación

Firehose puede volver a entregar un lote: en el retry, o cuando el 200 se pierde en la red. Cada evento usa el id nativo de AWS como clave de deduplicación:

| Stream | Clave |
|---|---|
| `waf` | `webaclId` + `requestId` + `timestamp` |
| `cloudtrail` | `eventID` |
| `guardduty`, `securityhub` | `Id` + `UpdatedAt` (cada actualización del hallazgo es un evento) |
| `cloudwatch_logs` | `eventId` |

Sin id nativo, la clave es el hash del contenido del evento.

## Limitaciones

- **Security Hub nuevo (OCSF).** Solo entra el formato ASFF ("Security Hub Findings - Imported"). Los hallazgos que el Security Hub nuevo ya entrega en OCSF no coinciden con el mapping y van a la cuarentena.
- **Sin prueba de conexión.** La integración no tiene credencial que probar. Está funcionando cuando llegan los eventos. En Firehose, las métricas de entrega del stream y los logs de error del destino muestran cada rechazo con el mensaje de CentralOps.
- **Región de los log events de CloudWatch.** La región es la del stream de Firehose, porque el envoltorio de CloudWatch Logs no la informa.
