---
sidebar_position: 7
title: AWS Security Hub y GuardDuty
description: Recolecta los hallazgos de seguridad de AWS (Security Hub en ASFF y GuardDuty) como OCSF Detection Finding
---

# AWS Security Hub y GuardDuty

Dos integraciones para traer los **hallazgos de seguridad** de AWS a CentralOps, normalizados como OCSF 1.8 **Detection Finding (2004)**.

| Integración | Qué trae | Cuándo usarla |
|---|---|---|
| **AWS Security Hub** | Hallazgos agregados en ASFF: GuardDuty, Inspector, Macie, IAM Access Analyzer, Config y los controles del propio Security Hub (CSPM) | La cuenta usa Security Hub. Es la opción por defecto: una integración, todos los productos |
| **Amazon GuardDuty** | Hallazgos de amenaza directo de la API de GuardDuty (cuentas, EC2, S3, EKS, RDS, Lambda) | La cuenta usa GuardDuty sin Security Hub |

Con las dos activadas, los hallazgos de GuardDuty llegan dos veces (por Security Hub y directo). Elige una.

## Cómo funciona

| Ítem | Security Hub | GuardDuty |
|---|---|---|
| Stream | `findings` (tipo `aws_securityhub.finding`) | `findings` (tipo `aws_guardduty.finding`) |
| API | `GetFindings`, filtro `UpdatedAt`, 100 por página | `ListDetectors` → `ListFindings` (filtro `updatedAt`, 50 IDs) → `GetFindings` |
| Frecuencia | cada 5 minutos | cada 5 minutos |
| Regiones | lista o `*` (las habilitadas en la cuenta, vía `ec2:DescribeRegions`) | lista o `*`; una región sin GuardDuty se salta |
| Primera recolección | hallazgos actualizados en las últimas 24 horas | ídem |
| Posición de la recolección | por región: el `UpdatedAt` más reciente leído | ídem |
| Tope por ciclo | 50 páginas, sumando las regiones; la región en curso guarda la posición exacta | ídem |
| Duplicados | `Id` + `UpdatedAt` | ídem |

**Cada actualización de un hallazgo es un evento.** GuardDuty suma ocurrencias en un hallazgo existente y Security Hub cambia el workflow o lo archiva. Por eso CentralOps trata `Id` + `UpdatedAt` como identidad: la misma amenaza que se repite genera un evento nuevo, con `activity_id` **Update**.

**Un hallazgo indexado con retraso no se pierde.** Cada consulta empieza 15 minutos antes de la posición guardada, y la posición nunca pasa del final de la ventana menos 15 minutos. Lo que se vuelva a leer lo descarta el dedupe.

**Security Hub con agregación entre regiones.** Si la *cross-Region aggregation* (agregación entre regiones) está activada, la región de agregación devuelve los hallazgos de todas. Indica solo esa; listar las otras traería duplicados.

## Requisitos previos

- **El servicio activado en la cuenta y en las regiones que vas a listar.** En la consola: **Security Hub → Go to Security Hub → Enable Security Hub**, o **GuardDuty → Get Started → Enable GuardDuty**, en cada región. GuardDuty solo genera hallazgos después de activarlo; no hay historial anterior.
- Lectura por el **rol cross-account** de CentralOps. El paso a paso por la consola está en [AWS: dar acceso a CentralOps](./aws-acesso.md). La plantilla del panel **Acceso por rol de AWS** ya incluye `securityhub:GetFindings`, `guardduty:ListDetectors`, `guardduty:ListFindings`, `guardduty:GetFindings` y `ec2:DescribeRegions`. El paso a paso del rol está en [AWS CloudTrail: Opción A](./aws-cloudtrail.md#opção-a-recomendada-papel-cross-account).

Con el rol creado a mano (o con access key, legado), la policy mínima es esta. `ec2:DescribeRegions` solo se usa con regiones `*`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "LerAchados",
      "Effect": "Allow",
      "Action": [
        "securityhub:GetFindings",
        "guardduty:ListDetectors",
        "guardduty:ListFindings",
        "guardduty:GetFindings",
        "ec2:DescribeRegions"
      ],
      "Resource": "*"
    }
  ]
}
```

### Varias cuentas de una AWS Organization

El paso a paso completo, por la consola y por la CLI, está en [AWS: dar acceso a CentralOps](./aws-acesso.md#várias-contas-de-uma-aws-organization). Resumen:

La misma plantilla sirve como **StackSet**: el External ID es por organización de CentralOps, no por cuenta de AWS, así que todas las cuentas de un cliente usan el mismo rol con el mismo External ID. En la cuenta de administración (o administradora delegada de CloudFormation):

```bash
aws cloudformation create-stack-set \
  --stack-set-name centralops-readonly \
  --template-body file://centralops-readonly-role.json \
  --capabilities CAPABILITY_NAMED_IAM \
  --permission-model SERVICE_MANAGED \
  --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false

aws cloudformation create-stack-instances \
  --stack-set-name centralops-readonly \
  --deployment-targets OrganizationalUnitIds=r-abcd \
  --regions us-east-1
```

El rol es global (IAM), así que basta con una región. Con `auto-deployment`, las cuentas nuevas de la Organization reciben el rol solas. La cuenta de administración no recibe instancia de un StackSet *service-managed*; crea la stack en ella por separado, si vas a recolectar de ella.

Dos formas de cubrir las cuentas:

- **Cuenta administradora delegada del servicio.** Con Security Hub o GuardDuty configurados con administradora delegada, una sola integración en esa cuenta recibe los hallazgos de todas las cuentas miembro.
- **Modo AWS Organization (Enterprise).** Las tarjetas **Security Hub — AWS Organization** y **GuardDuty — AWS Organization** crean un *padre* que lista las cuentas (`organizations:ListAccounts`) y, para cada cuenta aprobada, una integración hija en esta misma organización, que asume el rol de **mismo nombre y path** en la cuenta miembro (el que creó el StackSet de arriba). Las cuentas suspendidas o que salen de la Organization tienen la hija desactivada en la siguiente sincronización. El rol del padre queda en la cuenta de administración o en una administradora delegada de AWS Organizations; crea la stack en ella con `AllowListAccounts=true`. En Community el padre se crea, pero no se importa ninguna cuenta. El modo Organization exige rol; no se acepta una clave fija.

## Crear la integración

1. Ve a **Recolecta -> Integraciones** y agrega **AWS Security Hub** o **Amazon GuardDuty**.
2. Completa:

   | Campo | Qué indicar |
   |---|---|
   | ARN del rol o Access Key ID | El `RoleArn` de la stack (p. ej.: `arn:aws:iam::111122223333:role/CentralOpsReadOnly`). |
   | Secret Access Key | Solo con Access Key ID. |
   | Regiones de AWS | Security Hub con agregación: solo la región de agregación. Sin agregación, o en GuardDuty: lista (`us-east-1, sa-east-1`) o `*`. |

3. **Probar conexión.** Security Hub: lee 1 hallazgo de la primera región (`InvalidAccessException` = Security Hub no está habilitado ahí). GuardDuty: busca el detector de la primera región.
4. Guarda.

## Qué se recolecta

### Security Hub (ASFF)

| Campo OCSF | Origen |
|---|---|
| `time`, `finding_info.modified_time` | `UpdatedAt` |
| `finding_info.created_time` / `first_seen_time` / `last_seen_time` | `CreatedAt` / `FirstObservedAt` / `LastObservedAt` |
| `finding_info.uid` / `.title` / `.desc` / `.types` | `Id` / `Title` / `Description` / `Types` |
| `finding_info.product.name`, `metadata.product.name` | `ProductName` (GuardDuty, Inspector, Security Hub…) |
| `finding_info.analytic.uid` | `GeneratorId` (la regla o el control) |
| `severity_id` | `Severity.Label`: INFORMATIONAL 1, LOW 2, MEDIUM 3, HIGH 4, CRITICAL 5 |
| `status_id` | `Workflow.Status`: NEW 1, NOTIFIED 2, SUPPRESSED 3, RESOLVED 4; `RecordState` ARCHIVED 5 |
| `activity_id` | Create (nunca actualizado), Update, Close (ARCHIVED) |
| `resources[]` | `Resources[]` (`Type`, `Id`, `Region`) |
| `remediation.desc` / `.references` | `Remediation.Recommendation.Text` / `.Url` |
| `cloud.account.uid` / `cloud.region` | `AwsAccountId` / `Region` |
| `unmapped.aws_compliance_status` | `Compliance.Status` (PASSED, FAILED…) |

Los hallazgos **INFORMATIONAL** entran con `is_alert=false`: los controles CSPM generan muchos hallazgos `PASSED`. Descártalos o muestréalos en la ruta, si quieres.

### GuardDuty

| Campo OCSF | Origen |
|---|---|
| `severity_id` | `Severity`: 1.0–3.9 Low, 4.0–6.9 Medium, 7.0–8.9 High, 9.0+ Critical |
| `finding_info.types` | `Type` (p. ej.: `UnauthorizedAccess:IAMUser/TorIPCaller`) |
| `finding_info.first_seen_time` / `last_seen_time`, `count` | `Service.EventFirstSeen` / `EventLastSeen`, `Service.Count` |
| `status_id`, `activity_id`, `is_alert` | `Service.Archived`: archivado = Archived, Close, sin alerta |
| `src_endpoint.ip` / `.location.country` | IP remota de la acción (llamada de API, conexión, port probe, login RDS, Kubernetes) |
| `actor.user.name` / `.type` | `Resource.AccessKeyDetails.UserName` / `UserType` |
| `resources[]` | `Resource.ResourceType` + ID de la instancia, clave, bucket, clúster EKS, base de datos RDS o función Lambda |
| `unmapped.aws_guardduty_api` | `Service.Action.AwsApiCallAction.Api` |

El hallazgo completo queda en el `raw`.

## Solución de problemas

**`InvalidAccessException` en Security Hub.** El servicio no está habilitado en la región consultada, o la cuenta no es la administradora.

**GuardDuty: `Credencial OK, mas o GuardDuty não está habilitado em <región>`.** No hay detector en esa región. En la recolección, la región se salta sin error.

**Hallazgos duplicados.** Security Hub y GuardDuty están los dos activados para la misma cuenta, o Security Hub tiene agregación y listaste varias regiones.

## Limitaciones conocidas

- **Crear una integración por cuenta automáticamente es Enterprise** (modo AWS Organization). En Community, usa la administradora delegada del servicio o una integración por cuenta.
- **Security Hub en ASFF.** El formato OCSF nativo del nuevo Security Hub (`GetFindingsV2`) no se usa.
- **Sin actualización de vuelta.** CentralOps no cambia el workflow ni archiva hallazgos en AWS.
