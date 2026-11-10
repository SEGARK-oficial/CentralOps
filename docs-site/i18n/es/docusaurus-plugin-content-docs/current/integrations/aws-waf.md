---
sidebar_position: 7
title: AWS WAF
description: Recolecta el log de cada solicitud evaluada por las web ACLs de AWS WAF, leído desde S3 o desde CloudWatch Logs
---

# AWS WAF

Conecta AWS WAF a CentralOps para recolectar el **log de cada solicitud evaluada por una web ACL**: qué decidió WAF (permitir, bloquear, CAPTCHA, challenge), qué regla decidió, la IP y el país del cliente, la URI y el User-Agent. CentralOps lee el log donde el propio WAF lo entrega: en un bucket S3 o en un log group de CloudWatch Logs.

## Cuándo usarla

- **Ver ataques bloqueados en el borde.** SQLi, XSS, bots, reglas administradas de AWS.
- **Encontrar lo que pasó.** Una regla en modo **COUNT** coincidió y la solicitud llegó a la aplicación. Ese es el evento que pide investigación.
- **Rastrear un cliente.** Todas las solicitudes de una IP o de una huella TLS (JA4) que cambia de IP.
- **Ajustar reglas.** Qué reglas bloquean tráfico legítimo, por URI y país.

## Cómo funciona

```
Solicitud ──► web ACL (WAF) ─► logging ─► bucket S3 aws-waf-logs-*      ─┐
                                      └─► log group aws-waf-logs-*      ─┴─► CentralOps (cada 5 min)   
```

WAF no tiene una API que devuelva el log completo; el log solo existe después de activar el logging de la web ACL. Cada web ACL entrega a **un** destino, cuyo nombre tiene que empezar con `aws-waf-logs-`.

| Ítem | S3 | CloudWatch Logs |
|---|---|---|
| Stream | `events` (tipo `aws_waf.event`) | el mismo |
| Dónde lee | `AWSLogs/<cuenta>/WAFLogs/<región>/<web-acl>/AAAA/MM/dd/HH/mm/*.log.gz` | el log group indicado |
| Formato | un registro JSON por línea, archivos de 5 minutos | un registro JSON por log event |
| Web ACLs | todas las que entregan en el bucket, descubiertas solas | todas las que entregan en el log group |
| Regiones | todas, o las que listes (incluye `cloudfront`) | la del log group |
| Posición de la recolección | `LastModified` del último archivo, con 15 minutos de relectura | hora del último evento leído |
| Tope por ciclo | 500 archivos | 20.000 eventos |
| Cuándo conviene | volumen alto (más barato) | volumen bajo, o si el log ya está en CloudWatch |

Los dos caminos entregan el mismo registro. Un archivo de S3 que **no se puede leer** (sin `s3:GetObject`, sin `kms:Decrypt`) detiene el ciclo sin avanzar la posición, y el error aparece en **Salud del pipeline**. Un archivo **corrupto** se salta y queda registrado en `skipped_objects` en el estado de la recolección. Una línea inválida dentro de un archivo bueno se descarta sola.

Para recibir el log de WAF **casi en tiempo real**, con el destino **Firehose** de la web ACL, usa la integración [AWS vía Firehose](./aws-firehose.md).

## Requisitos previos

- **Logging activado en la web ACL**, hacia un bucket S3 o un log group con nombre `aws-waf-logs-…`. En la consola: **WAF & Shield → Web ACLs → tu ACL → Logging and metrics → Enable**.
- **Acceso de lectura** al destino: el rol cross-account de CentralOps (recomendado) o una access key.

## Paso 1: Activar el logging de la web ACL

Si todavía no está activado, elige el destino y actívalo. Por la CLI, para S3:

```bash
aws s3api create-bucket --bucket aws-waf-logs-minha-empresa --region us-east-1
aws wafv2 put-logging-configuration --logging-configuration \
  ResourceArn=arn:aws:wafv2:us-east-1:111122223333:regional/webacl/minha-acl/ID-DA-ACL,LogDestinationConfigs=arn:aws:s3:::aws-waf-logs-minha-empresa
```

Para ver a dónde entrega ya una web ACL:

```bash
aws wafv2 get-logging-configuration \
  --resource-arn arn:aws:wafv2:us-east-1:111122223333:regional/webacl/minha-acl/ID-DA-ACL \
  --query "LoggingConfiguration.LogDestinationConfigs"
```

El valor devuelto es exactamente lo que va en el campo **Destino de los logs de WAF** del Paso 3.

:::tip[Reduce el volumen en el origen]

WAF también registra las solicitudes permitidas, que son la mayoría. Para enviar solo lo que interesa, usa el **filtro de logging** de WAF (mantener `BLOCK` y `COUNT`, descartar el resto) y la **redacción de campos** para no guardar los headers `Authorization` y `Cookie`.

:::

## Paso 2: Dar acceso de lectura a CentralOps

Usa el mismo **rol cross-account** de CloudTrail. El paso a paso por la consola está en [AWS: dar acceso a CentralOps](./aws-acesso.md). En el formulario de la integración, el panel **Acceso por rol de AWS** muestra el External ID de la organización, el principal de CentralOps y la plantilla de CloudFormation. El paso a paso está en [AWS CloudTrail: Opción A](./aws-cloudtrail.md#opção-a-recomendada-papel-cross-account).

Al crear la stack, indica el bucket de WAF:

```bash
aws cloudformation deploy \
  --template-file centralops-readonly-role.json \
  --stack-name centralops-readonly \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides WafLogsBucketName=aws-waf-logs-minha-empresa
```

La plantilla da `s3:ListBucket`/`s3:GetObject` en el bucket de WAF y lectura de CloudWatch Logs (`logs:FilterLogEvents`). Si también se lee el bucket de CloudTrail, pasa los dos parámetros en la misma stack. Con SSE-KMS, indica también `KmsKeyArn`.

Con **access key** (legado), o con el rol creado a mano, la policy mínima es esta. Cambia el bucket (S3) o la región, la cuenta y el log group (CloudWatch), y usa solo el bloque del destino que usas:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "WafNoS3Listar",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::aws-waf-logs-minha-empresa"
    },
    {
      "Sid": "WafNoS3Ler",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::aws-waf-logs-minha-empresa/*"
    },
    {
      "Sid": "WafNoCloudWatch",
      "Effect": "Allow",
      "Action": "logs:FilterLogEvents",
      "Resource": "arn:aws:logs:us-east-1:111122223333:log-group:aws-waf-logs-api:*"
    }
  ]
}
```

## Paso 3: Crear la integración en CentralOps

1. Ve a **Recolecta -> Integraciones** y agrega una integración **AWS WAF**.
2. Completa:

   | Campo | Qué indicar |
   |---|---|
   | ARN del rol o Access Key ID | El `RoleArn` de la stack (p. ej.: `arn:aws:iam::111122223333:role/CentralOpsReadOnly`) o el `AccessKeyId`. |
   | Secret Access Key | Solo con Access Key ID. |
   | Destino de los logs de WAF | El ARN que muestra WAF: `arn:aws:s3:::aws-waf-logs-minha-empresa` (o `s3://aws-waf-logs-minha-empresa/prefixo`) para S3, o `arn:aws:logs:us-east-1:111122223333:log-group:aws-waf-logs-api` para CloudWatch. Un nombre suelto se rechaza: el bucket y el log group usan el mismo prefijo `aws-waf-logs-`. |
   | Regiones de AWS | Solo para S3. Vacío o `*` = todas las regiones del bucket, incluida `cloudfront`. O una lista: `us-east-1, sa-east-1`. |
   | Account ID | La cuenta dueña de los logs. Vacío = la cuenta del ARN del rol (S3) o del log group (CloudWatch). |

3. Haz clic en **Probar conexión**. Con rol, la prueba asume el rol. Después lista el bucket (S3) o lee 1 evento del log group (CloudWatch).
4. Guarda. La recolección empieza en el siguiente ciclo (hasta 5 minutos).

## Qué se recolecta

Cada registro se convierte en un evento OCSF 1.8 de la clase **HTTP Activity (4002)**, con el registro original en el `raw`.

| Acción de WAF | `disposition` (`disposition_id`) | `action` | Severidad | Alerta |
|---|---|---|---|---|
| `ALLOW`, sin regla coincidente | Allowed (1) | Allowed | Informational | no |
| `ALLOW` con regla en modo COUNT | Count (20) | Allowed | **Medium** | sí |
| `BLOCK` | Blocked (2) | Denied | Low | sí |
| `CAPTCHA` | Captcha (22) | Other | Low | sí |
| `CHALLENGE` | Challenge (23) | Other | Low | sí |

La solicitud permitida que coincidió con una regla en COUNT recibe la severidad más alta a propósito: llegó a la aplicación.

| Campo OCSF | Origen en el log de WAF |
|---|---|
| `time` | `timestamp` (epoch en ms) |
| `activity_id` / `type_uid` | `httpRequest.httpMethod` (GET = 3, POST = 6, …) |
| `src_endpoint.ip` / `.location.country` | `httpRequest.clientIp` / `httpRequest.country` (`-` = vacío) |
| `http_request.url.hostname`, `dst_endpoint.hostname` | header `Host` (o `:authority` en HTTP/2) |
| `http_request.url.path` / `.query_string` | `httpRequest.uri` / `httpRequest.args` |
| `http_request.user_agent` | header `User-Agent` |
| `http_request.http_method` / `.version` / `.uid` | `httpMethod` / `httpVersion` / `requestId` |
| `http_response.code` | `responseCodeSent` (respuesta personalizada de WAF) |
| `firewall_rule.uid` / `.type` | `terminatingRuleId` / `terminatingRuleType` |
| `firewall_rule.match_location` / `.match_details` | `terminatingRuleMatchDetails[0]` (SQLi/XSS) |
| `policy.uid` | `webaclId` |
| `metadata.labels` | `labels[].name` (p. ej.: `awswaf:managed:aws:bot-control:…`) |
| `ja4_fingerprint_list` | `ja4Fingerprint` (CloudFront y ALB) |
| `cloud.region` / `cloud.account.uid` | ruta en S3 o ARN del log group |
| `unmapped.aws_waf_count_rules` | `nonTerminatingMatchingRules[].ruleId` |
| `unmapped.aws_waf_group_rule` | la regla dentro del grupo administrado que decidió |
| `unmapped.aws_waf_attack_type` | `terminatingRuleMatchDetails[0].conditionType` |

Los demás campos (`ruleGroupList`, `rateBasedRuleList`, headers completos) quedan en el `raw`. Los duplicados se descartan por la combinación web ACL + `requestId` + `timestamp`.

## Solución de problemas

**`Informe o destino de logging da web ACL como ARN…`.** El campo recibió un nombre suelto. Usa el ARN del Paso 1 o `s3://bucket`.

**La prueba pasa, pero no llega nada (S3).** Revisa la ruta que usa WAF:

```bash
aws s3 ls s3://aws-waf-logs-minha-empresa/AWSLogs/111122223333/WAFLogs/ --recursive | tail -3
```

¿Vacío? El logging no está activado, o el Account ID no es la cuenta de las web ACLs. Si las carpetas son de otra región, revisa el campo **Regiones de AWS**.

**`waf: falha ao ler s3://… — AccessDenied`.** El rol lista el bucket pero no lee el archivo: falta `s3:GetObject` o, con la pista `kms:Decrypt`, permiso en la clave KMS. La recolección queda detenida en ese punto, sin perder nada, hasta que se corrija el permiso.

**Eventos en la cuarentena con `unparsedMessage`.** El log group indicado tiene eventos que no son de WAF. Revisa el ARN del log group.

## Limitaciones conocidas

- **Esta integración no lee Firehose.** Para el destino Firehose de la web ACL, usa la integración [AWS vía Firehose](./aws-firehose.md): WAF entrega directo en CentralOps, sin bucket.
- **Una cuenta por integración en S3.** Un bucket central con logs de varias cuentas necesita una integración por cuenta.
- **Sin descubrimiento por la API de WAF.** Las web ACLs se descubren por las carpetas del bucket, no por `wafv2:ListWebACLs`.
- **Volumen.** Una web ACL con mucho tráfico permitido supera el tope de 500 archivos por ciclo. Filtra en el origen (consulta el Paso 1).
