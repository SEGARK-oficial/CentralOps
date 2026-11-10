---
sidebar_position: 6
title: "AWS: dar acceso a CentralOps"
description: Paso a paso para crear el rol de servicio que deja a CentralOps leer los logs de tu cuenta de AWS, con los permisos mínimos de cada integración
---

# AWS: dar acceso a CentralOps

Esta página muestra, paso a paso, cómo dejar que CentralOps lea los logs de una cuenta de AWS. Vale para todas las integraciones de AWS de lectura: [CloudTrail](./aws-cloudtrail.md), [CloudWatch Logs](./aws-cloudwatch.md), [WAF](./aws-waf.md), [Security Hub y GuardDuty](./aws-findings.md). La integración [vía Firehose](./aws-firehose.md) funciona al revés (AWS envía los logs) y no usa este acceso.

## En pocas palabras

Creas en tu cuenta de AWS un **rol** (*IAM role*), que funciona como una cuenta de servicio de solo lectura. CentralOps "se pone" ese rol cuando va a buscar los logs y recibe una credencial que vale 1 hora. Ninguna contraseña ni clave queda guardada en CentralOps.

Dos candados protegen este acceso:

- **Solo CentralOps puede usar el rol.** El rol confía en una única identidad de AWS: la del servidor de CentralOps, llamada aquí **principal de CentralOps**.
- **Solo tu organización puede usar el rol.** CentralOps envía junto un código, el **External ID**, que es único para cada organización registrada en él. Si otra organización de CentralOps intenta usar tu rol, AWS lo rechaza, porque el código no coincide.

Para cortar el acceso en cualquier momento, borra el rol (o la stack que lo creó). La recolección se detiene en el ciclo siguiente.

## Antes de empezar

Vas a necesitar:

- **Acceso de administrador a la cuenta de AWS**, o al menos permiso para crear roles en IAM y stacks en CloudFormation.
- **Acceso de administrador en CentralOps**, para crear la integración.
- **Los nombres de lo que se va a leer:** el bucket de CloudTrail, el bucket o log group de WAF, el log group de CloudWatch. Cada página de integración dice dónde encontrar esos nombres.
- **Si los logs están cifrados con una clave KMS tuya:** el ARN de esa clave.

## Paso 1: Obtener el External ID y la plantilla en CentralOps

1. En CentralOps, ve a **Recolecta -> Integraciones** y haz clic para agregar una integración de AWS (por ejemplo, **AWS CloudTrail**).
2. Elige la **organización** dueña de esa cuenta de AWS.
3. Aparece el panel **Acceso por rol de AWS (recomendado)**. Muestra:
   - el **External ID** de la organización (ejemplo: `centralops-42`);
   - el **principal de CentralOps** (ejemplo: `arn:aws:iam::999999999999:role/centralops-server`);
   - el botón **Descargar plantilla**, un archivo de CloudFormation que ya viene con esos dos valores completados.
4. Haz clic en **Descargar plantilla** y guarda el archivo `centralops-readonly-role.json`.

Deja esa pantalla abierta: vuelves a ella en el Paso 3.

:::info[El External ID no cambia]

CentralOps crea el External ID una vez para cada organización y nunca cambia. Todas las cuentas de AWS de un mismo cliente pueden usar el mismo rol con el mismo External ID.

:::

## Paso 2: Crear el rol en la cuenta de AWS

Elige uno de los tres caminos. El resultado es el mismo: un rol llamado `CentralOpsReadOnly`.

### Camino A (el más fácil): por la consola, con la plantilla

1. Entra en la consola de AWS, en la cuenta de donde vienen los logs.
2. Abre el servicio **CloudFormation**. La región no importa, porque el rol vale para toda la cuenta.
3. Haz clic en **Create stack** (crear stack) y luego en **With new resources (standard)**.
4. En **Prerequisite - Prepare template**, deja **Choose an existing template**. En **Specify template**, elige **Upload a template file** (subir un archivo de plantilla), selecciona `centralops-readonly-role.json` y haz clic en **Next**.
5. En **Stack name**, escribe `centralops-readonly`.
6. En **Parameters**, completa solo lo que vas a usar y deja el resto como está:

   | Parámetro | Qué poner |
   |---|---|
   | `CloudTrailBucketName` | El nombre del bucket de CloudTrail (solo el nombre, sin `s3://`). Vacío si no vas a recolectar CloudTrail. |
   | `WafLogsBucketName` | El nombre del bucket `aws-waf-logs-…` de WAF. Vacío si WAF no escribe en S3. |
   | `KmsKeyArn` | El ARN de la clave KMS que cifra los logs, si la hay. Vacío si no hay clave propia. |
   | `AllowListAccounts` | `true` solo en la cuenta que usa el [modo AWS Organization](#várias-contas-de-uma-aws-organization). En las demás, `false`. |
   | `ExternalId`, `TrustedPrincipalArn`, `RoleName` | Ya vienen completados. **No los cambies.** |

7. Haz clic en **Next** dos veces.
8. Al final de la página, marca **I acknowledge that AWS CloudFormation might create IAM resources with custom names** (confirmas que CloudFormation puede crear recursos de IAM con nombres propios) y haz clic en **Submit**.
9. Espera a que el estado quede en **CREATE_COMPLETE**, lo que tarda cerca de 1 minuto.
10. Abre la pestaña **Outputs** (salidas) y copia el valor de **RoleArn** (ejemplo: `arn:aws:iam::111122223333:role/CentralOpsReadOnly`).

### Camino B: por la AWS CLI, con la plantilla

El panel de CentralOps tiene el botón **Copiar comando de AWS CLI** con el comando listo. Es así:

```bash
aws cloudformation deploy \
  --template-file centralops-readonly-role.json \
  --stack-name centralops-readonly \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides CloudTrailBucketName=meu-bucket-cloudtrail

aws cloudformation describe-stacks --stack-name centralops-readonly \
  --query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" --output text
```

El segundo comando imprime el `RoleArn`. Para cambiar un parámetro después (por ejemplo, agregar el bucket de WAF), vuelve a ejecutar el `deploy` con todos los parámetros que usas.

### Camino C: a mano, en la consola de IAM {#caminho-c-à-mão-no-console-do-iam}

Usa este camino si tu empresa no permite CloudFormation.

1. Abre **IAM -> Roles** y haz clic en **Create role** (crear rol).
2. En **Trusted entity type** (tipo de entidad de confianza), elige **AWS account** y luego **Another AWS account**.
3. En **Account ID**, escribe los 12 dígitos de la cuenta que aparece en el **principal de CentralOps** (en el ejemplo, `999999999999`).
4. Marca **Require external ID** y pega el External ID del panel. Haz clic en **Next**.
5. En la pantalla de permisos, no marques nada y haz clic en **Next**. Los permisos entran en el paso 8.
6. En **Role name**, escribe `CentralOpsReadOnly` y haz clic en **Create role**.
7. Abre el rol creado, ve a la pestaña **Trust relationships** (relaciones de confianza), haz clic en **Edit trust policy** y reemplaza el contenido por este. Así solo la identidad exacta de CentralOps puede usar el rol, y no la cuenta entera:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Principal": { "AWS": "arn:aws:iam::999999999999:role/centralops-server" },
         "Action": "sts:AssumeRole",
         "Condition": { "StringEquals": { "sts:ExternalId": "centralops-42" } }
       }
     ]
   }
   ```

   Cambia el ARN por el **principal de CentralOps** y `centralops-42` por tu External ID.

8. En la pestaña **Permissions**, haz clic en **Add permissions -> Create inline policy** (crear una policy en línea), elige **JSON** y pega la policy de las integraciones que vas a usar, armada a partir de la [tabla de permisos mínimos](#permissões-mínimas-de-cada-integração).
9. En **Maximum session duration** (duración máxima de la sesión), deja **1 hour**.
10. Copia el **ARN** del rol, en la parte de arriba de la página.

:::warning[No quites la condición del External ID]

Sin la condición `sts:ExternalId`, quien descubra el ARN de tu rol puede registrarlo en otra organización de CentralOps y leer tus logs. CentralOps no acepta un External ID escrito a mano: siempre usa el de la organización.

:::

## Paso 3: Pegar el ARN y probar

1. Vuelve a la pantalla de la integración en CentralOps.
2. Pega el `RoleArn` en el campo **ARN del rol o Access Key ID**. Deja **Secret Access Key** vacío: con rol, no se usa.
3. Completa los otros campos de la integración (bucket, regiones y los demás). La página de cada integración explica cada campo.
4. Haz clic en **Probar conexión**. CentralOps usa el rol de verdad e intenta leer una muestra. La respuesta esperada empieza con `Conexão OK (papel assumido)`.
5. Haz clic en **Guardar**. La recolección empieza en el próximo ciclo, en hasta 5 minutos.

Si la prueba falla, mira los [problemas comunes](#problemas-comuns).

## Permisos mínimos de cada integración {#permissões-mínimas-de-cada-integração}

El rol solo necesita lectura. La tabla muestra exactamente lo que cada integración llama en AWS:

| Integración | Permisos | En qué recurso |
|---|---|---|
| CloudTrail | `s3:ListBucket` | el bucket del trail |
| | `s3:GetObject` | los archivos del bucket (`arn:aws:s3:::bucket/*`) |
| CloudWatch Logs | `logs:FilterLogEvents` | el log group (`arn:aws:logs:región:cuenta:log-group:nombre:*`) |
| WAF (logs en S3) | `s3:ListBucket` y `s3:GetObject` | el bucket `aws-waf-logs-…` |
| WAF (logs en CloudWatch) | `logs:FilterLogEvents` | el log group `aws-waf-logs-…` |
| Security Hub | `securityhub:GetFindings` | `*` |
| GuardDuty | `guardduty:ListDetectors`, `guardduty:ListFindings` y `guardduty:GetFindings` | `*` |
| Security Hub o GuardDuty con regiones `*` | `ec2:DescribeRegions` (descubre las regiones habilitadas en la cuenta) | `*` |
| Logs cifrados con KMS | `kms:Decrypt` | la clave KMS |
| Modo AWS Organization (padre) | `organizations:ListAccounts` | `*` |

La plantilla de CentralOps concede estos permisos y dos más, también solo de lectura de metadatos: `s3:GetBucketLocation` (en qué región está el bucket) y `logs:DescribeLogGroups` (lista los nombres de los log groups). No se concede ningún permiso de escritura.

Ejemplo de policy para quien recolecta CloudTrail (con KMS) y GuardDuty. Cambia los nombres por los tuyos:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListarOBucketDaTrail",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::meu-bucket-cloudtrail"
    },
    {
      "Sid": "LerOsArquivosDaTrail",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::meu-bucket-cloudtrail/*"
    },
    {
      "Sid": "DecifrarOsArquivos",
      "Effect": "Allow",
      "Action": "kms:Decrypt",
      "Resource": "arn:aws:kms:us-east-1:111122223333:key/ID-DA-CHAVE"
    },
    {
      "Sid": "LerAchadosDoGuardDuty",
      "Effect": "Allow",
      "Action": ["guardduty:ListDetectors", "guardduty:ListFindings", "guardduty:GetFindings"],
      "Resource": "*"
    }
  ]
}
```

### Cuando el bucket o la clave están en otra cuenta

Es común guardar los logs en una cuenta central (*log archive*). En ese caso, el permiso del rol no basta, y el dueño del bucket también tiene que liberar el acceso:

- **Bucket en otra cuenta:** agrega esta regla en la *bucket policy* (S3 -> el bucket -> **Permissions** -> **Bucket policy**):

  ```json
  {
    "Sid": "CentralOpsLeitura",
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::111122223333:role/CentralOpsReadOnly" },
    "Action": ["s3:ListBucket", "s3:GetObject"],
    "Resource": ["arn:aws:s3:::meu-bucket-cloudtrail", "arn:aws:s3:::meu-bucket-cloudtrail/*"]
  }
  ```

- **Clave KMS en otra cuenta:** la *key policy* de la clave (KMS -> la clave -> **Key policy**) tiene que permitir `kms:Decrypt` para el mismo ARN del rol.

Más simple: crea el rol en la propia cuenta central, donde están el bucket y la clave.

## Varias cuentas de una AWS Organization {#várias-contas-de-uma-aws-organization}

Si el cliente tiene muchas cuentas de AWS, no crees el rol cuenta por cuenta. Usa un **StackSet**, que crea el mismo rol en todas las cuentas de una vez y también en las cuentas que se creen después.

1. Entra en la **cuenta de administración** (*management account*) de la Organization (o en una administradora delegada de CloudFormation).
2. Abre **CloudFormation -> StackSets -> Create StackSet**.
3. En **Permissions**, elige **Service-managed permissions** (permisos administrados por el servicio).
4. Sube el mismo `centralops-readonly-role.json` y completa los parámetros como en el Camino A.
5. En **Deployment options**, elige las unidades organizativas (o la raíz `r-…` para todas las cuentas), activa **Automatic deployment** y elige **una** región. El rol vale para toda la cuenta.
6. Termina. La cuenta de administración no recibe el rol por StackSet: si vas a recolectar de ella, crea una stack común en ella (Camino A).

Por la CLI:

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

Con el rol en todas las cuentas, el **modo AWS Organization** (edición Enterprise) crea las integraciones por ti. Registras solo un "padre", y CentralOps lista las cuentas y crea una integración "hija" para cada cuenta que apruebes:

1. En la cuenta de administración, o en una administradora delegada de AWS Organizations, crea la stack con `AllowListAccounts=true`. Este rol es el único que necesita listar las cuentas.
2. En CentralOps, agrega la integración **AWS CloudTrail — AWS Organization**, **Amazon GuardDuty — AWS Organization** o **AWS Security Hub — AWS Organization**, y pega el ARN de ese rol.
3. **Probar conexión** confirma que el rol lista las cuentas.
4. Guarda. En la página de la integración padre, el panel de tenants muestra las cuentas encontradas. Aprueba las que quieres recolectar, o activa la aprobación automática.
5. Cada cuenta aprobada se convierte en una integración propia, que recolecta de todas las regiones (`*`). Para restringir, edita las regiones en la integración hija.

Cómo accede cada hija a su cuenta:

- **GuardDuty y Security Hub:** la hija usa el rol con el **mismo nombre y ruta** en la cuenta miembro, el que creó el StackSet.
- **CloudTrail:** la hija usa el **rol del padre** y lee la parte de su cuenta en el bucket del trail de la Organization (`AWSLogs/o-…/<cuenta>/`). Por eso, el rol del padre también tiene que leer ese bucket: indica `CloudTrailBucketName` (y `KmsKeyArn`, si lo hay) en la stack de la cuenta donde está el bucket, en general la de *log archive*.

Una cuenta suspendida, o que sale de la Organization, tiene su hija desactivada en la sincronización siguiente. En la edición Community, el padre se crea pero no se importa ninguna cuenta.

## Instalación propia: la identidad del servidor {#instalação-própria-a-identidade-do-servidor}

En una instalación propia (*self-hosted*), el servidor de CentralOps necesita una identidad de AWS para usar los roles de los clientes. Viene de la cadena estándar de AWS: variables `AWS_ACCESS_KEY_ID` y `AWS_SECRET_ACCESS_KEY`, perfil de instancia (EC2) o IRSA (EKS).

- Esa identidad solo necesita el permiso `sts:AssumeRole` en los roles de los clientes, por ejemplo `arn:aws:iam::*:role/CentralOpsReadOnly`.
- El panel descubre su ARN solo.
- Si el rol del servidor tiene ruta (`role/caminho/Nome`), define `AWS_TRUST_PRINCIPAL_ARN` con el ARN completo.
- Sin identidad, el panel avisa **No se encontró la identidad de AWS de CentralOps**.

## Access key (legado)

CentralOps todavía acepta un **Access Key ID** y una **Secret Access Key** de un usuario IAM. Es una credencial que no vence y queda guardada (cifrada) en CentralOps. Úsala solo si el rol no es posible.

1. En IAM, crea un usuario solo para CentralOps, sin acceso a la consola.
2. Adjunta una policy con los [permisos mínimos](#permissões-mínimas-de-cada-integração) de la integración.
3. Crea una access key (**Security credentials -> Create access key -> Third-party service**).
4. En CentralOps, pega el Access Key ID en el campo **ARN del rol o Access Key ID** y la clave secreta en **Secret Access Key**.

Las credenciales temporales (que empiezan con `ASIA` y tienen session token) no se aceptan. El modo AWS Organization exige rol.

## Problemas comunes {#problemas-comuns}

| El mensaje dice | Qué hacer |
|---|---|
| `AssumeRole: AccessDenied` | La trust policy del rol no confía en el principal de CentralOps, o el External ID es distinto. Compara los dos con el panel **Acceso por rol de AWS**. |
| `No se encontró la identidad de AWS de CentralOps` | Solo en instalación propia: el servidor no tiene credencial de AWS. Mira [Instalación propia](#instalação-própria-a-identidade-do-servidor). |
| `AccessDenied` al listar o leer el bucket | Falta `s3:ListBucket` o `s3:GetObject` en el rol, o la bucket policy de otra cuenta no libera el rol. |
| `KMS` o `kms:Decrypt` en el error | Los archivos están cifrados. Indica `KmsKeyArn` en la stack y, si la clave es de otra cuenta, libera el rol en la key policy. |
| `ResourceNotFoundException` | El log group no existe en la región indicada. Revisa el nombre exacto (con las barras) y la región. |
| `InvalidAccessException` (Security Hub) | Security Hub no está habilitado en esa región. |
| `AWSOrganizationsNotInUse` o `AccessDenied` en `ListAccounts` | El rol del modo Organization no está en la cuenta de administración (ni en una administradora delegada), o la stack se creó con `AllowListAccounts=false`. |

## Cómo cortar el acceso

- **Por AWS:** borra la stack `centralops-readonly` (o el rol `CentralOpsReadOnly`). El acceso termina al instante; la credencial ya emitida vale como máximo 1 hora.
- **Por CentralOps:** desactiva o elimina la integración.
