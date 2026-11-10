---
sidebar_position: 6
title: AWS CloudTrail
description: Recolecta los eventos de API de AWS (IAM, consola, cambios de recursos) leyendo los archivos que CloudTrail entrega en S3
---

# AWS CloudTrail

Conecta una cuenta de AWS a CentralOps para recolectar los **eventos de API registrados por CloudTrail**: quién llamó a qué API, desde dónde, con qué credencial y si la llamada falló. CentralOps lee los archivos `.json.gz` que un *trail* entrega en un bucket S3.

## Cuándo usarla

- **Vigilar IAM.** `CreateAccessKey`, `AttachUserPolicy` con `AdministratorAccess`, `CreateUser`, `UpdateAssumeRolePolicy`.
- **Ver inicios de sesión en la consola.** `ConsoleLogin` con y sin MFA, y fallos de inicio de sesión.
- **Detectar que se apagó una defensa.** `StopLogging`, `DeleteTrail`, `PutBucketPolicy` que deja un bucket público, `DisableKey`.
- **Investigar una clave filtrada.** Todas las llamadas hechas con un `accessKeyId`, y desde qué IPs.

## Cómo funciona

```
API de AWS ──► CloudTrail (trail) ──► bucket S3 ──list + get (cada 5 min)──► CentralOps
```

| Ítem | Valor |
|---|---|
| Stream | `events` (tipo de evento `aws_cloudtrail.event`) |
| Frecuencia | cada 5 minutos |
| Dónde lee | `s3://<bucket>/[prefixo/]AWSLogs/<Account ID>/CloudTrail/<región>/AAAA/MM/DD/` y, si el trail es de Organization, `…/AWSLogs/o-<org>/<Account ID>/CloudTrail/<región>/…` |
| Estructura | se descubre en cada ciclo listando el bucket: el trail de Organization y las regiones (con `*`) no necesitan configuración |
| Posición de la recolección | `LastModified` del último archivo leído; cada ciclo vuelve a leer los 15 minutos anteriores para atrapar entregas atrasadas |
| Duplicados | se descartan por el `eventID` (AWS repite el mismo `eventID` cuando entrega dos veces) |
| Primera recolección | archivos entregados en las últimas 2 horas |
| Tope por ciclo | 500 archivos. Al llegar al tope, el ciclo guarda la posición exacta (día + último archivo) y el siguiente continúa desde ahí |
| Días por ciclo | desde el día del último archivo leído (menos 1 día de margen) hasta hoy, como máximo 14 días |

CloudTrail particiona por la fecha de **entrega** del archivo, no por la hora del evento. Por eso la posición es el `LastModified` del objeto, y el atraso que se muestra en **Salud del pipeline** es el atraso de CentralOps respecto al bucket, no el atraso de entrega de la propia AWS.

Cada archivo trae un `Records[]`. Cada ítem se convierte en un evento. Un archivo que **no se puede leer** (sin `s3:GetObject`, sin `kms:Decrypt`, red) **detiene el ciclo**: la posición de la recolección no avanza y el error aparece en **Salud del pipeline**, para que no se pierda ningún archivo. Solo un archivo **corrupto** (gzip o JSON inválido, que volver a leer no arregla) se salta; queda registrado en el estado de la recolección (mira [Solución de problemas](#solução-de-problemas)).

## Qué entra y qué no entra

- **Entra:** todo lo que el trail graba en el bucket: eventos de administración (*management events*) y, si el trail también registra *data events* (S3, Lambda), esos también, porque van en los mismos archivos.
- **No entra:** el **Event history** de la consola (90 días, sin trail) no se lee. CloudTrail Insights y los archivos de *digest* están en otros prefijos (`CloudTrail-Insight/`, `CloudTrail-Digest/`) y se ignoran. CloudTrail Lake no se usa.

## Quién puede hacer qué

- **Administrador de la plataforma:** crea y edita la integración (menú **Recolecta -> Integraciones**).
- **En AWS:** crear el trail requiere permiso de CloudTrail y S3 en la cuenta. Crear el usuario y la policy requiere permiso de IAM.

## Requisitos previos {#pré-requisitos}

- **Un trail que entregue en un bucket S3.** Sin trail, no hay archivo para leer.
- **Prefijo de clave S3.** Si el trail usa *S3 key prefix* (`meu-prefixo/AWSLogs/...`), indica el bucket como `bucket/meu-prefixo`.
- **El trail de Organization funciona sin configuración.** Graba en `AWSLogs/o-<org>/<cuenta>/...`; CentralOps encuentra esa ruta listando el bucket y lee solo la cuenta indicada. Un trail convertido a Organization deja archivos en la ruta antigua: se leen las dos. Para varias cuentas de una Organization, crea una integración por cuenta o usa el [modo AWS Organization](./aws-acesso.md#várias-contas-de-uma-aws-organization) (Enterprise), que crea una integración por cuenta por sí solo.
- **Regiones.** Una integración lee una región, una lista (`us-east-1, sa-east-1`) o todas las que existen en el bucket (`*`).
- **Listar el bucket.** El descubrimiento lista `AWSLogs/` con delimitador. Una policy antigua que restringe `s3:ListBucket` por prefijo niega ese listado; el colector entonces vuelve a la ruta clásica `AWSLogs/<cuenta>/CloudTrail/<región>/` (sin Organization y sin `*`).

:::info[Los eventos de IAM llegan en us-east-1]

En un trail multirregión, los eventos de servicios globales (IAM, STS global, inicio de sesión en la consola del usuario root) se entregan en el prefijo de `us-east-1`. Aunque tus recursos estén en `sa-east-1`, incluye `us-east-1` en las regiones (o usa `*`) si quieres ver cambios de IAM.

:::

## Paso 1: Tener un trail que entregue en S3

Si ya tienes un trail, anota el nombre del bucket y si usa prefijo de clave (**CloudTrail → Trails → tu trail → General details**).

Si no lo tienes, por la consola:

1. Abre **CloudTrail → Trails** y haz clic en **Create trail** (crear trail).
2. Ponle un nombre (p. ej.: `centralops`), elige **Create new S3 bucket** y anota el nombre del bucket. La consola crea el bucket ya con la policy que el trail necesita.
3. **Log file SSE-KMS encryption** (cifrado de los archivos con SSE-KMS) viene activado en la consola. Si lo dejas activado, anota el ARN de la clave (**AWS KMS alias**/**New**): CentralOps va a necesitar `kms:Decrypt` en ella, y tú indicas la clave en el parámetro `KmsKeyArn` del rol.
4. Haz clic en **Next**. En **Event type**, deja **Management events** marcado. Haz clic en **Next** y luego en **Create trail**.

Por la CLI:

```bash
aws s3api create-bucket --bucket meu-bucket-cloudtrail --region us-east-1
# El trail necesita una bucket policy que le permita escribir; la consola crea
# esa policy sola. Por la CLI, sigue la policy "Amazon S3 bucket policy for
# CloudTrail" de la documentación de AWS antes del próximo comando.
aws cloudtrail create-trail --name centralops --s3-bucket-name meu-bucket-cloudtrail \
  --is-multi-region-trail
aws cloudtrail start-logging --name centralops
```

AWS entrega el primer archivo algunos minutos después de que el trail empieza a registrar.

## Paso 2: Dar acceso de lectura a CentralOps

Hay dos formas. Prefiere el **rol**: CentralOps no guarda ninguna clave de larga duración, y el cliente puede revocar el acceso borrando el rol.

:::tip[Paso a paso por la consola]

La página [AWS: dar acceso a CentralOps](./aws-acesso.md) muestra la creación del rol clic a clic (por CloudFormation o a mano en IAM), los permisos mínimos de cada integración y qué hacer cuando el bucket está en otra cuenta. Aquí queda el resumen.

:::

### Opción A (recomendada): rol cross-account {#opção-a-recomendada-papel-cross-account}

El cliente crea, en su cuenta de AWS, un rol de solo lectura que confía en la identidad de AWS de CentralOps y exige un **External ID**. CentralOps asume ese rol en cada ciclo (`sts:AssumeRole`) y trabaja con credenciales temporales de 1 hora.

1. En CentralOps, abre **Recolecta -> Integraciones**, elige **AWS CloudTrail** y selecciona la organización. El panel **Acceso por rol de AWS** muestra:
   - el **External ID** de la organización. Lo genera CentralOps, es único por organización y no cambia;
   - el **principal de CentralOps**, el ARN en el que el rol debe confiar;
   - el botón **Descargar plantilla**, una plantilla de CloudFormation con esos dos valores ya completados.
2. En la cuenta del cliente, crea la stack con la plantilla. Indica el bucket del trail y, si los archivos están cifrados con SSE-KMS, el ARN de la clave:

   ```bash
   aws cloudformation deploy \
     --template-file centralops-readonly-role.json \
     --stack-name centralops-readonly \
     --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides CloudTrailBucketName=meu-bucket-cloudtrail \
       KmsKeyArn=arn:aws:kms:us-east-1:111122223333:key/ID-DA-CHAVE-DA-TRAIL

   aws cloudformation describe-stacks --stack-name centralops-readonly \
     --query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" --output text
   ```

   La plantilla crea el rol `CentralOpsReadOnly` con `s3:ListBucket`/`s3:GetObject` en el bucket, `kms:Decrypt` en la clave (si se indicó) y la lectura que usan las otras integraciones de AWS (CloudWatch Logs, Security Hub, GuardDuty). Ningún permiso de escritura. La lista completa está en [permisos mínimos](./aws-acesso.md#permissões-mínimas-de-cada-integração).
3. Guarda el `RoleArn` de la salida para el Paso 3.

¿Prefieres crear el rol a mano? El panel muestra la trust policy (política de confianza) lista. La condición que importa es esta, con el External ID de la organización:

```json
"Condition": { "StringEquals": { "sts:ExternalId": "centralops-<id-de-la-organización>" } }
```

:::warning[El External ID es el candado entre clientes]

Sin él, quien supiera el ARN del rol de otro cliente podría registrarlo en una integración propia. Por eso CentralOps genera el valor, no acepta un valor escrito a mano y nunca lo cambia. No quites la condición `sts:ExternalId` de la trust policy.

:::

:::info[Instalación propia (self-hosted): la identidad del servidor]

Para asumir roles, el servidor de CentralOps necesita una identidad de AWS propia, por la cadena estándar del SDK: variables `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, perfil, perfil de instancia (EC2) o IRSA (EKS). Esa identidad solo necesita `sts:AssumeRole` en los roles de los clientes. El panel descubre su ARN solo (`sts:GetCallerIdentity`). Si el rol del servidor tiene *path* (`role/caminho/Nome`), define `AWS_TRUST_PRINCIPAL_ARN` con el ARN completo, porque STS no devuelve el path. Sin identidad, el panel avisa y la prueba de conexión responde que el servidor no tiene identidad de AWS propia.

:::

### Opción B (legado): access key de usuario IAM

CentralOps también acepta una **access key de usuario IAM** (Access Key ID + Secret Access Key). Es una credencial de larga duración: úsala solo cuando el rol no sea posible. Las credenciales temporales (`ASIA...` con session token) no se aceptan.

Crea un usuario dedicado y adjúntale esta policy, cambiando el bucket y el Account ID:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListarOBucket",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::meu-bucket-cloudtrail"
    },
    {
      "Sid": "LerOsArquivosDaTrail",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::meu-bucket-cloudtrail/AWSLogs/111122223333/CloudTrail/*"
    }
  ]
}
```

Si el trail cifra los archivos con **SSE-KMS** (una clave KMS tuya), agrega:

```json
{
  "Sid": "DecifrarOsArquivosDaTrail",
  "Effect": "Allow",
  "Action": "kms:Decrypt",
  "Resource": "arn:aws:kms:us-east-1:111122223333:key/ID-DA-CHAVE-DA-TRAIL"
}
```

:::warning[No restrinjas el ListBucket por prefijo]

La prueba de conexión lista la raíz del bucket (un objeto, sin prefijo). Una condición `s3:prefix` en el `s3:ListBucket` hace que la prueba falle con acceso denegado, aunque la recolección funcione. Restringe el `s3:GetObject`, como en el ejemplo.

:::

```bash
aws iam create-user --user-name centralops-cloudtrail
aws iam put-user-policy --user-name centralops-cloudtrail \
  --policy-name centralops-cloudtrail-leitura --policy-document file://policy.json
aws iam create-access-key --user-name centralops-cloudtrail
```

Guarda el `AccessKeyId` y el `SecretAccessKey` de la salida. El secreto no se vuelve a mostrar.

Si el bucket está en **otra cuenta** (cuenta central de logs), la bucket policy de esa cuenta también tiene que liberar `s3:ListBucket` y `s3:GetObject` para el ARN del rol o del usuario.

## Paso 3: Crear la integración en CentralOps

1. En el menú lateral, ve a **Recolecta -> Integraciones** y agrega una integración **AWS CloudTrail**.
2. Completa:

   | Campo | Qué indicar |
   |---|---|
   | ARN del rol o Access Key ID | Opción A: el `RoleArn` del Paso 2 (p. ej.: `arn:aws:iam::111122223333:role/CentralOpsReadOnly`). Opción B: el `AccessKeyId` (p. ej.: `AKIAIOSFODNN7EXAMPLE`). CentralOps reconoce el modo por el formato. |
   | Secret Access Key | Solo en la Opción B: el `SecretAccessKey`. Se guarda cifrado. Con rol, déjalo vacío. |
   | Bucket S3 | El nombre del bucket (p. ej.: `meu-bucket-cloudtrail`). Con prefijo de clave: `meu-bucket-cloudtrail/meu-prefixo`. `s3://…` y `arn:aws:s3:::…` también valen. |
   | Regiones AWS | `us-east-1`, una lista (`us-east-1, sa-east-1`) o `*` para todas las regiones del bucket. La primera región de la lista es la del cliente S3 (el SDK sigue la redirección si el bucket está en otra). |
   | Account ID | Los 12 dígitos de la cuenta cuyos eventos están en el bucket (p. ej.: `111122223333`). Forma parte del prefijo que se lee. Con rol, puede quedar vacío cuando la cuenta de los logs es la misma del rol. |

3. Haz clic en **Probar conexión**. Con rol, la prueba asume el rol de verdad (detecta una trust policy o un External ID equivocados antes de guardar). Después lista un objeto del bucket y responde `Conexão OK (papel assumido)…` o `Conexão OK (chave fixa)…`, o el motivo del fallo (p. ej.: `AssumeRole: AccessDenied — confira se a trust policy do papel confia no principal do CentralOps e exige o External ID desta organização.`).

   :::note[La prueba valida el bucket, no la ruta]

   La prueba solo demuestra que la credencial lista el bucket. No revisa el Account ID, la región ni el permiso de `s3:GetObject`. Después de guardar, confirma en el Paso 4 que los eventos llegan.

   :::

4. Guarda. La recolección empieza en el próximo ciclo (hasta 5 minutos).

## Paso 4: Revisar la primera recolección

- En **Recolecta -> Colectores**, el stream `events` de la integración debe mostrar eventos después del primer ciclo.
- En **Salud del pipeline**, el atraso de los datos es el tiempo desde el último archivo leído. Mira [Salud del pipeline](../operations/pipeline-health).
- Para ver el evento crudo y el normalizado lado a lado, usa la [captura en vivo](../operations/live-capture).

## Qué se recolecta

Cada ítem del `Records[]` se convierte en un evento normalizado en OCSF 1.8, clase **API Activity (6003)**, con el registro original preservado en `raw`.

| Campo OCSF | Origen en CloudTrail |
|---|---|
| `class_uid` / `activity_id` / `type_uid` | `6003` / `0` (Unknown) / `600300` en todos los eventos |
| `time` | `eventTime` |
| `api.operation`, `message`, `metadata.event_code` | `eventName` |
| `api.service.name` | `eventSource` |
| `api.response.error` / `api.response.message` | `errorCode` / `errorMessage` |
| `status` | `errorCode`; `Success` cuando no hay error |
| `actor.user.name` | `userIdentity.userName`; sin él (roles, root), `userIdentity.arn` |
| `actor.user.uid` / `actor.user.type` | `userIdentity.principalId` / `userIdentity.type` |
| `src_endpoint.ip` | `sourceIPAddress` |
| `http_request.user_agent` | `userAgent` |
| `cloud.region` / `cloud.account.uid` | `awsRegion` / `recipientAccountId` |
| `metadata.uid` | `eventID` |
| `severity_id` | `1` (Informational) en todos los eventos |

`requestParameters`, `responseElements` y `userIdentity.accessKeyId` quedan solo en `raw`. Las reglas de detección que miran, por ejemplo, qué policy se adjuntó deben leer `raw.requestParameters.policyArn`.

### Ejemplo

Evento crudo (datos sintéticos), tal como sale del archivo del trail:

```json
{
  "eventVersion": "1.09",
  "userIdentity": {
    "type": "IAMUser",
    "principalId": "AIDAEXAMPLEPRINCIPAL",
    "arn": "arn:aws:iam::111122223333:user/ana.souza",
    "accountId": "111122223333",
    "accessKeyId": "AKIAIOSFODNN7EXAMPLE",
    "userName": "ana.souza"
  },
  "eventTime": "2026-10-20T13:42:07Z",
  "eventSource": "iam.amazonaws.com",
  "eventName": "AttachUserPolicy",
  "awsRegion": "us-east-1",
  "sourceIPAddress": "203.0.113.24",
  "userAgent": "aws-cli/2.17.0 Python/3.11.9 Linux/6.8 exe/x86_64",
  "requestParameters": {
    "userName": "deploy-bot",
    "policyArn": "arn:aws:iam::aws:policy/AdministratorAccess"
  },
  "responseElements": null,
  "requestID": "6a1f8c2e-0b7d-4c55-9f3e-1d2a3b4c5d6e",
  "eventID": "3f9b6c1a-2d4e-4f8a-9b0c-7e6d5c4b3a21",
  "readOnly": false,
  "eventType": "AwsApiCall",
  "managementEvent": true,
  "recipientAccountId": "111122223333",
  "eventCategory": "Management"
}
```

Evento normalizado:

```json
{
  "normalized": {
    "class_uid": 6003,
    "class_name": "API Activity",
    "category_uid": 6,
    "category_name": "Application Activity",
    "activity_id": 0,
    "type_uid": 600300,
    "time": 1792503727000,
    "severity_id": 1,
    "severity": "Informational",
    "message": "AttachUserPolicy",
    "api": {
      "operation": "AttachUserPolicy",
      "service": {
        "name": "iam.amazonaws.com"
      },
      "response": {
        "error": null,
        "message": null
      }
    },
    "actor": {
      "user": {
        "name": "ana.souza",
        "uid": "AIDAEXAMPLEPRINCIPAL",
        "type": "IAMUser"
      }
    },
    "src_endpoint": {
      "ip": "203.0.113.24"
    },
    "http_request": {
      "user_agent": "aws-cli/2.17.0 Python/3.11.9 Linux/6.8 exe/x86_64"
    },
    "cloud": {
      "provider": "AWS",
      "region": "us-east-1",
      "account": {
        "uid": "111122223333"
      }
    },
    "status": "Success",
    "metadata": {
      "version": "1.8.0",
      "product": {
        "name": "CloudTrail",
        "vendor_name": "AWS"
      },
      "uid": "3f9b6c1a-2d4e-4f8a-9b0c-7e6d5c4b3a21",
      "event_code": "AttachUserPolicy"
    },
    "observables": [
      {
        "name": "actor_user",
        "type": "User Name",
        "type_id": 4,
        "value": "ana.souza"
      },
      {
        "name": "src_ip",
        "type": "IP Address",
        "type_id": 2,
        "value": "203.0.113.24"
      }
    ]
  }
}
```

## Solución de problemas {#solução-de-problemas}

**La prueba pasa, pero no llega nada.**

1. Revisa si hay archivos en la ruta que lee CentralOps, con la misma cuenta, región y la fecha de hoy:

   ```bash
   aws s3 ls s3://meu-bucket-cloudtrail/AWSLogs/111122223333/CloudTrail/us-east-1/2026/10/20/ | tail -3
   ```

   ¿Vacío? El Account ID o la región del formulario no coinciden con el trail, o el trail usa un prefijo de clave que no se indicó en el campo Bucket (mira [Requisitos previos](#pré-requisitos)). Trail de Organization: la ruta es `AWSLogs/o-<org>/<cuenta>/CloudTrail/…`.
2. Mira el error del stream en **Salud del pipeline**.

**Error `cloudtrail: falha ao ler s3://… — AccessDenied`.** CentralOps lista el bucket, pero no logra leer un archivo. Con la pista `kms:Decrypt`, falta permiso en la clave KMS que cifra el trail; sin ella, falta `s3:GetObject`. La recolección queda detenida en ese punto, **sin perder nada**: una vez corregido el permiso, el próximo ciclo lee el archivo y sigue.

**`AssumeRole: AccessDenied` en la prueba o en la recolección.** La trust policy del rol no confía en el principal que muestra el panel, o no exige exactamente el External ID de la organización. Compara los dos valores con el panel. En la instalación propia, la identidad del servidor también necesita `sts:AssumeRole` en el ARN del rol.

**`o servidor do CentralOps não tem identidade AWS própria`.** Instalación propia sin credenciales de AWS en el entorno del servidor. Mira el aviso *Instalación propia* en el Paso 2.

**Archivo corrupto.** Un archivo con gzip o JSON inválido se salta (volver a leerlo no lo arregla) y se registra en `skipped_objects` en el estado de la recolección del stream, con la clave y el motivo (los 20 más recientes). El log del worker trae `cloudtrail: objeto <chave> corrompido`.

**`Falha ao acessar o bucket: AccessDenied` en la prueba.** Acceso denegado al `s3:ListBucket` (mira el aviso de la Opción B sobre la condición de prefijo) o bucket de otra cuenta sin bucket policy. `NoSuchBucket`: nombre equivocado o bucket en otra región.

**Los eventos de IAM no aparecen.** Están en el prefijo de `us-east-1`. Incluye esa región en el campo Regiones AWS (o usa `*`).

**"Backlog" en Salud del pipeline.** Cada ciclo procesó 500 archivos y quedó cola. Es normal en la primera carga y después de una parada. Si sigue encendido por mucho tiempo, el volumen del trail supera lo que un ciclo de 5 minutos alcanza a drenar.

## Limitaciones conocidas

- **Una cuenta por integración.** En un trail de Organization, cada cuenta necesita su propia integración. La tarjeta **CloudTrail — AWS Organization** (Enterprise) crea un padre que lista las cuentas y una hija por cada cuenta aprobada, todas leyendo el bucket central con el rol del padre. Ese rol necesita listar las cuentas (`organizations:ListAccounts`, con `AllowListAccounts=true` en la plantilla) y leer el bucket: en general es la cuenta de log archive registrada como administradora delegada.
- **El bucket se escribe a mano.** CentralOps descubre la estructura dentro del bucket, pero no descubre qué bucket usa el trail (`DescribeTrails`).
- **Todos los eventos salen como API Activity con `activity_id` 0 y severidad Informational.** CentralOps no clasifica `Create`/`Read`/`Update`/`Delete` por el nombre de la API, y el éxito o fallo está en `status` (texto), no en `status_id`.
- **La recolección histórica (backfill) no respeta el período pedido.** El colector empieza desde la ventana estándar (archivos de las últimas 2 horas), no desde el inicio del período elegido.
- **El archivo corrupto se salta** y solo queda registrado en el estado de la recolección (mira Solución de problemas).
