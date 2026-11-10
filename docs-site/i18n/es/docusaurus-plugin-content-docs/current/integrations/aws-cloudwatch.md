---
sidebar_position: 7
title: AWS CloudWatch Logs
description: Recolecta los eventos de un log group de CloudWatch Logs (Lambda, ECS, API Gateway, RDS, aplicaciones) con FilterLogEvents
---

# AWS CloudWatch Logs

Conecta un **log group** de CloudWatch Logs a CentralOps para recolectar los logs que servicios y aplicaciones graban ahí: funciones Lambda, tareas ECS, API Gateway, logs exportados de RDS, appliances con el agente de CloudWatch. Es la fuente "para todo" (*catch-all*) de AWS. Para los eventos de API de la propia AWS (IAM, consola), usa [AWS CloudTrail](./aws-cloudtrail.md).

## Cuándo usarla

- **Logs de aplicaciones en AWS.** Errores y accesos denegados de una API que corre en Lambda o ECS.
- **Logs de servicios administrados.** API Gateway, logs de error y de auditoría exportados de RDS.
- **Fuentes sin integración propia** que ya graban en CloudWatch mediante un agente.

## Cómo funciona

```
servicio / aplicación ──► log group ──FilterLogEvents (cada 5 min)──► CentralOps
```

| Ítem | Valor |
|---|---|
| Stream | `events` (tipo de evento `aws_cloudwatch.event`) |
| API | [`FilterLogEvents`](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_FilterLogEvents.html) en `logs.<región>.amazonaws.com`, en orden ascendente de hora |
| Frecuencia | cada 5 minutos |
| Alcance | **un log group por integración**, todos sus log streams |
| Posición de la recolección | hora del evento (ms) hasta donde se leyó la ventana; cada ventana nueva retrocede 5 minutos para atrapar eventos que entraron atrasados |
| Duplicados | se descartan por el `eventId` (único por evento, garantizado por AWS) |
| Primera recolección | eventos de la última hora |
| Ventana por ciclo | como máximo 6 horas. Un atraso mayor se drena por partes, ciclo a ciclo |
| Tope por ciclo | 20 páginas de 1.000 eventos (20.000 eventos). Al llegar al tope, el ciclo guarda el `nextToken` y la ventana, y el siguiente continúa desde la misma página |

La respuesta de `FilterLogEvents` no trae log group, región, cuenta ni una hora legible. CentralOps agrega al evento crudo los campos `logGroupName`, `awsRegion`, `awsAccountId` (cuando se indica) y `eventTime` (ISO 8601, convertido del `timestamp` en milisegundos).

## Quién puede hacer qué

- **Administrador de la plataforma:** crea y edita la integración (menú **Recolecta -> Integraciones**).
- **En AWS:** crear el rol (o el usuario) y la policy requiere permiso de IAM.

## Requisitos previos

- El **nombre exacto del log group** (p. ej.: `/aws/lambda/faturamento-api`) y su **región**.
- Un **rol cross-account** (recomendado) o una **access key de usuario IAM**. Las credenciales temporales con session token escritas a mano no se aceptan.

## Paso 1: Dar acceso de lectura a CentralOps

**Recomendado: rol cross-account.** El paso a paso por la consola está en [AWS: dar acceso a CentralOps](./aws-acesso.md). Es el mismo rol de CloudTrail: el panel **Acceso por rol de AWS**, en el formulario de la integración, muestra el External ID de la organización, el principal de CentralOps y la plantilla de CloudFormation. La plantilla ya incluye `logs:DescribeLogGroups` y `logs:FilterLogEvents` en todos los log groups de la cuenta. Para limitarlo a un solo log group, crea el rol a mano ([Camino C](./aws-acesso.md#caminho-c-à-mão-no-console-do-iam)) con la policy de abajo. El paso a paso, incluida la identidad del servidor en una instalación propia, está en [AWS CloudTrail — Opción A](./aws-cloudtrail.md#opção-a-recomendada-papel-cross-account). Guarda el `RoleArn` de la stack.

**Legado: access key de usuario IAM.** El único permiso que se usa es `logs:FilterLogEvents` en el log group. Cambia región, cuenta y nombre:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "LerOLogGroup",
      "Effect": "Allow",
      "Action": "logs:FilterLogEvents",
      "Resource": "arn:aws:logs:sa-east-1:111122223333:log-group:/aws/lambda/faturamento-api:*"
    }
  ]
}
```

```bash
aws iam create-user --user-name centralops-cloudwatch
aws iam put-user-policy --user-name centralops-cloudwatch \
  --policy-name centralops-cloudwatch-leitura --policy-document file://policy.json
aws iam create-access-key --user-name centralops-cloudwatch
```

Guarda el `AccessKeyId` y el `SecretAccessKey` de la salida. Para leer varios log groups, repite el `Resource` en la policy (o usa `log-group:/aws/lambda/*`) y crea una integración por log group.

## Paso 2: Crear la integración en CentralOps

1. En el menú lateral, ve a **Recolecta -> Integraciones** y agrega una integración **AWS CloudWatch Logs**.
2. Completa:

   | Campo | Qué indicar |
   |---|---|
   | ARN del rol o Access Key ID | El `RoleArn` del rol (p. ej.: `arn:aws:iam::111122223333:role/CentralOpsReadOnly`) o el `AccessKeyId`. CentralOps reconoce el modo por el formato. |
   | Secret Access Key | Solo con Access Key ID. Se guarda cifrado. Con rol, déjalo vacío. |
   | Log Group | El nombre exacto, con las barras (p. ej.: `/aws/lambda/faturamento-api`). Sin comodín. |
   | Región AWS | La región del log group (p. ej.: `sa-east-1`). |
   | Account ID | Opcional. Solo completa `cloud.account.uid` en el evento normalizado, porque la API no devuelve la cuenta. Con rol, vacío = la cuenta del ARN. |

3. Haz clic en **Probar conexión**. La prueba hace un `FilterLogEvents` de 1 evento en la última hora del log group. Valida de una vez la credencial, el permiso y que el log group exista. Con rol, antes asume el rol de verdad. Responde `Conexão OK (papel assumido)…` o `Conexão OK (chave fixa)…`, o el motivo del fallo (p. ej.: `Falha ao consultar o log group: ResourceNotFoundException — o recurso (ex.: log group) não existe nesta região.`).
4. Guarda. La recolección empieza en el próximo ciclo (hasta 5 minutos).

## Qué se recolecta

Cada evento del log group se convierte en un evento normalizado en OCSF 1.8, clase **Base Event (0)**, porque el contenido de un log group es libre. El evento original queda en `raw`.

Si el campo `message` es un JSON, CentralOps lo lee y aprovecha algunos campos comunes. Si no lo es, el evento sigue normalizado solo con el mensaje en texto.

| Campo OCSF | Origen |
|---|---|
| `time`, `metadata.original_time` | `eventTime` (viene del `timestamp` de CloudWatch) |
| `message` | `message`, completo |
| `severity_id` / `severity` | `level` o `severity` dentro del JSON: `trace`/`debug`/`info`/`notice` → 1; `warn`/`warning` → 3; `error`/`err` → 4; `critical`/`crit`/`alert` → 5; `fatal`/`emerg`/`emergency` → 6. Sin el campo: 1 |
| `metadata.uid` | `eventId` |
| `metadata.log_name`, `unmapped.log_group` | `logGroupName` |
| `metadata.correlation_uid`, `unmapped.log_stream` | `logStreamName` |
| `cloud.region` / `cloud.account.uid` | región del formulario / Account ID del formulario |
| `unmapped.operation` | `eventName`, `operation` o `action` del JSON |
| `unmapped.user_name` | `user`, `userName` o `userIdentity.userName` del JSON |
| `unmapped.src_ip` | `sourceIPAddress`, `srcAddr`, `client_ip` o `clientIp` del JSON |
| `unmapped.error_code` / `unmapped.error_message` | `errorCode` o `error` / `errorMessage` del JSON |
| `unmapped.status`, `unmapped.user_agent`, `unmapped.user_uid` | `status`, `userAgent`, `userId` o `userIdentity.principalId` del JSON |
| `observables` | usuario e IP de arriba, cuando existen |

Para una fuente específica (un formato de aplicación tuyo), crea un mapeo propio en **Normaliza -> Mapeos** que coloque los campos en la clase OCSF correcta. Mira [Normalización](../normalization/overview).

### Ejemplo

Evento crudo (datos sintéticos), ya con los campos que agrega CentralOps:

```json
{
  "eventId": "38451234567890123456789012345678901234567890123456789012",
  "timestamp": 1792503727000,
  "ingestionTime": 1792503728113,
  "logStreamName": "2026/10/20/[$LATEST]0a1b2c3d4e5f",
  "message": "{\"level\":\"ERROR\",\"operation\":\"DeleteInvoice\",\"user\":\"ana.souza@example.com\",\"clientIp\":\"198.51.100.17\",\"status\":403,\"errorMessage\":\"forbidden\"}",
  "logGroupName": "/aws/lambda/faturamento-api",
  "awsRegion": "sa-east-1",
  "awsAccountId": "111122223333",
  "eventTime": "2026-10-20T13:42:07.000Z"
}
```

Evento normalizado:

```json
{
  "normalized": {
    "class_uid": 0,
    "class_name": "Base Event",
    "category_uid": 0,
    "category_name": "Uncategorized",
    "activity_id": 0,
    "activity_name": "Unknown",
    "type_uid": 0,
    "time": 1792503727000,
    "metadata": {
      "original_time": "2026-10-20T13:42:07.000Z",
      "version": "1.8.0",
      "product": {
        "name": "CloudWatch Logs",
        "vendor_name": "AWS"
      },
      "uid": "38451234567890123456789012345678901234567890123456789012",
      "log_name": "/aws/lambda/faturamento-api",
      "log_provider": "AWS CloudWatch Logs",
      "correlation_uid": "2026/10/20/[$LATEST]0a1b2c3d4e5f"
    },
    "severity_id": 4,
    "severity": "High",
    "message": "{\"level\":\"ERROR\",\"operation\":\"DeleteInvoice\",\"user\":\"ana.souza@example.com\",\"clientIp\":\"198.51.100.17\",\"status\":403,\"errorMessage\":\"forbidden\"}",
    "cloud": {
      "provider": "AWS",
      "region": "sa-east-1",
      "account": {
        "uid": "111122223333"
      }
    },
    "unmapped": {
      "log_group": "/aws/lambda/faturamento-api",
      "log_stream": "2026/10/20/[$LATEST]0a1b2c3d4e5f",
      "operation": "DeleteInvoice",
      "error_code": null,
      "error_message": "forbidden",
      "user_name": "ana.souza@example.com",
      "user_uid": null,
      "src_ip": "198.51.100.17",
      "user_agent": null,
      "status": 403
    },
    "observables": [
      {
        "name": "actor_user",
        "type": "User Name",
        "type_id": 4,
        "value": "ana.souza@example.com"
      },
      {
        "name": "src_ip",
        "type": "IP Address",
        "type_id": 2,
        "value": "198.51.100.17"
      }
    ]
  }
}
```

## Solución de problemas

**`Falha ao consultar o log group: ResourceNotFoundException`.** El log group no existe en esa región. Revisa el nombre (con la barra inicial) y la región: `aws logs describe-log-groups --log-group-name-prefix /aws/lambda/faturamento --region sa-east-1`.

**`Falha ao consultar o log group: AccessDeniedException`.** Falta `logs:FilterLogEvents` en el ARN de ese log group, o la policy apunta a otra región o cuenta.

**`AssumeRole: AccessDenied`.** La trust policy del rol no confía en el principal que muestra el panel o no exige el External ID de la organización. Mira [AWS CloudTrail — Solución de problemas](./aws-cloudtrail.md#solução-de-problemas).

**La prueba pasa, pero no llega nada.** Puede que el log group no haya recibido eventos desde que se creó la integración. Compruébalo con `aws logs filter-log-events --log-group-name /aws/lambda/faturamento-api --start-time $(( ($(date +%s) - 3600) * 1000 )) --limit 5 --region sa-east-1`.

**"Backlog" en Salud del pipeline.** El ciclo leyó 20.000 eventos y quedó cola. Si pasa siempre, el log group genera más de unos 4.000 eventos por minuto, que es lo que la recolección drena de forma continua.

**Errores de throttling (`ThrottlingException`).** La cuota de `FilterLogEvents` es por cuenta y región, y se reparte con cualquier otra herramienta que lea logs en la misma cuenta. El ciclo que falla se repite en el siguiente, desde la posición guardada.

## Limitaciones conocidas

- **Un log group por integración**, por el nombre exacto. No hay comodín ni lista.
- **Un rol por integración, sin descubrimiento de log groups.** El nombre del log group se escribe a mano.
- **Clase genérica.** Todo evento sale como Base Event (0); los campos extraídos del JSON quedan en `unmapped`, no en atributos OCSF de clase.
- **Un volumen alto acumula atraso.** El tope de 20.000 eventos por ciclo de 5 minutos limita el caudal. Log groups como los VPC Flow Logs de una VPC con mucho tráfico lo superan.
- **La recolección histórica (backfill) no respeta el período pedido.** El colector empieza desde la ventana estándar (la última hora), no desde el inicio del período elegido.
