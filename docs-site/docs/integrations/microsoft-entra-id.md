---
sidebar_position: 8
title: Microsoft Entra ID
description: Colete os logs de sign-in e a auditoria de diretório do Microsoft Entra ID (Azure AD) pelo Microsoft Graph
---

# Microsoft Entra ID

Conecte o Microsoft Entra ID (antigo Azure AD) ao CentralOps para coletar dois fluxos de identidade pelo Microsoft Graph: os **logs de sign-in** (quem entrou, de onde, em qual aplicação, se falhou) e a **auditoria de diretório** (quem criou usuário, mudou papel, alterou aplicação).

Esta integração coleta logs de identidade. Para usar o Entra ID como login no próprio CentralOps, veja [SSO com Entra ID](../administration/sso-entra.md).

## Quando usar

- **Detectar ataque a senha.** Rajada de falhas de login para muitos usuários a partir do mesmo IP, ou para um usuário a partir de muitos IPs.
- **Vigiar papéis privilegiados.** `Add member to role` para *Global Administrator*, criação de credencial em aplicação.
- **Correlacionar identidade com endpoint.** Juntar o login de um usuário aos alertas do Defender ou do Sophos do mesmo dispositivo.

## Como funciona

| Stream | Endpoint (Graph v1.0) | Tipo de evento | Posição da coleta | Classe OCSF |
|---|---|---|---|---|
| `signins` | `GET /auditLogs/signIns` | `entra_id.signin` | `createdDateTime` | Authentication (3002) |
| `audit` | `GET /auditLogs/directoryAudits` | `entra_id.audit` | `activityDateTime` | Account Change (3001) |

- **Frequência:** a cada 5 minutos por stream, na fila de prioridade.
- **Consulta:** `$filter=<campo> ge <posição>` com `$top=1000`, seguindo o `@odata.nextLink` até o fim.
- **Duplicados:** o filtro `ge` devolve de novo os eventos do instante de início da janela. O CentralOps lembra os `id` desse instante e os pula; o dedupe por `id` cobre o resto.
- **Primeira coleta:** a última hora.
- **Teto por ciclo:** 25 páginas (25.000 eventos) por stream. Ao bater o teto, o ciclo grava o `@odata.nextLink` e o seguinte continua dali. Se o Graph recusar um link guardado (link expirado), a coleta volta ao filtro por horário sem perder a posição.
- **Throttling:** HTTP 429 respeita o `Retry-After` do Graph.

## Quem pode fazer o quê

- **Administrador da plataforma:** cria e edita a integração (menu **Coleta -> Integrações**).
- **No Entra ID:** registrar a aplicação exige *Application Developer* ou superior. Dar o consentimento de administrador exige *Privileged Role Administrator* ou *Global Administrator*.

## Pré-requisitos

- **Licença Microsoft Entra ID P1 ou P2** no tenant para o stream `signins`. Sem ela, o Graph recusa a leitura dos logs de sign-in. A auditoria de diretório não depende dessa licença.
- Acesso de **administrador no CentralOps**.

## Passo 1: Registrar a aplicação

Se você já tem a aplicação da [integração Microsoft Defender](./microsoft-defender.md), pode reaproveitá-la: acrescente a permissão do Passo 2 e use o mesmo Client ID e segredo.

1. No [portal do Entra](https://entra.microsoft.com), vá em **Identidade → Aplicativos → Registros de aplicativo → Novo registro**.
2. Nome: por exemplo `CentralOps Entra ID`. Tipos de conta: **somente este diretório organizacional**. Clique em **Registrar**.
3. Anote o **ID do aplicativo (cliente)** e o **ID do diretório (locatário)**.
4. Em **Certificados e segredos → Segredos do cliente → Novo segredo do cliente**, crie um segredo e copie o **Valor** na hora (ele não aparece de novo). Anote a data de expiração.

## Passo 2: Dar a permissão AuditLog.Read.All

1. Na aplicação, abra **Permissões de API → Adicionar uma permissão → Microsoft Graph → Permissões de aplicativo** (não delegadas).
2. Selecione **`AuditLog.Read.All`**. Ela cobre os dois streams e é a menor permissão que o Graph aceita para eles.
3. Clique em **Conceder consentimento do administrador para &lt;sua organização&gt;** e confirme. O status deve ficar verde.

## Passo 3: Criar a integração no CentralOps

1. No menu lateral, vá em **Coleta -> Integrações** e adicione uma integração **Microsoft Entra ID**.
2. Preencha:

   | Campo | O que informar |
   |---|---|
   | Tenant ID | O ID do diretório (locatário). |
   | Client ID | O ID do aplicativo (cliente). |
   | Client Secret | O Valor do segredo. Fica guardado cifrado. |

3. Clique em **testar a conexão**. O teste pede um token ao Entra (`client_credentials`, escopo `https://graph.microsoft.com/.default`).

   :::note[O teste valida o segredo, não a permissão]

   O token sai mesmo sem `AuditLog.Read.All` ou sem consentimento. Uma permissão faltando só aparece no primeiro ciclo de coleta, como erro 403. Confira o Passo 4.

   :::

4. Salve. A coleta começa no próximo ciclo (até 5 minutos).

## Passo 4: Conferir a primeira coleta

Em **Coleta -> Coletores**, os streams `signins` e `audit` devem mostrar eventos. Um stream em erro com 403 indica permissão sem consentimento ou, só no `signins`, falta de licença P1/P2.

## O que é coletado

### Sign-ins (`signins`)

Classe **Authentication (3002)**, atividade **Logon (1)**.

| Campo OCSF | Origem |
|---|---|
| `time` | `createdDateTime` |
| `user.name` / `user.uid` / `user.full_name` | `userPrincipalName` / `userId` / `userDisplayName` |
| `src_endpoint.ip` | `ipAddress` |
| `device.os.name` | `deviceDetail.operatingSystem` |
| `message` | `appDisplayName` (a aplicação acessada) |
| `status` | `status.failureReason` (texto da Microsoft) |
| `metadata.uid` | `id` |
| `severity_id` | `1` em todos os eventos |

O sucesso ou a falha do login não é traduzido para `status_id`. Use `raw.status.errorCode`: `0` é sucesso, qualquer outro valor é o código `AADSTS` da falha (ex.: `50126`, senha inválida). Risco, acesso condicional, localização e aplicação cliente ficam no `raw` (`riskLevelDuringSignIn`, `conditionalAccessStatus`, `location`, `clientAppUsed`).

#### Exemplo

Evento bruto (dados sintéticos):

```json
{
  "id": "5e3f9a1c-7b2d-4e8f-a6c0-1d2e3f4a5b6c",
  "createdDateTime": "2026-10-20T13:42:07Z",
  "userDisplayName": "Ana Souza",
  "userPrincipalName": "ana.souza@example.com",
  "userId": "8c7d6e5f-4a3b-2c1d-0e9f-8a7b6c5d4e3f",
  "appId": "00000002-0000-0ff1-ce00-000000000000",
  "appDisplayName": "Office 365 Exchange Online",
  "ipAddress": "203.0.113.24",
  "clientAppUsed": "Browser",
  "isInteractive": true,
  "conditionalAccessStatus": "failure",
  "status": {
    "errorCode": 50126,
    "failureReason": "Error validating credentials due to invalid username or password.",
    "additionalDetails": null
  },
  "deviceDetail": {
    "deviceId": "",
    "operatingSystem": "Windows10",
    "browser": "Edge 130.0.0"
  },
  "location": {
    "city": "Sao Paulo",
    "state": "Sao Paulo",
    "countryOrRegion": "BR"
  }
}
```

Evento normalizado:

```json
{
  "normalized": {
    "class_uid": 3002,
    "class_name": "Authentication",
    "category_uid": 3,
    "category_name": "Identity & Access Management",
    "activity_id": 1,
    "activity_name": "Logon",
    "type_uid": 300201,
    "time": 1792503727000,
    "severity_id": 1,
    "severity": "Informational",
    "status": "Error validating credentials due to invalid username or password.",
    "message": "Office 365 Exchange Online",
    "user": {
      "name": "ana.souza@example.com",
      "uid": "8c7d6e5f-4a3b-2c1d-0e9f-8a7b6c5d4e3f",
      "full_name": "Ana Souza"
    },
    "src_endpoint": {
      "ip": "203.0.113.24"
    },
    "device": {
      "os": {
        "name": "Windows10"
      }
    },
    "metadata": {
      "version": "1.8.0",
      "product": {
        "name": "Microsoft Entra ID",
        "vendor_name": "Microsoft"
      },
      "uid": "5e3f9a1c-7b2d-4e8f-a6c0-1d2e3f4a5b6c"
    },
    "observables": [
      {
        "name": "user",
        "type": "User Name",
        "type_id": 4,
        "value": "ana.souza@example.com"
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

### Auditoria de diretório (`audit`)

Classe **Account Change (3001)**, atividade `0` em todos os eventos.

| Campo OCSF | Origem |
|---|---|
| `time` | `activityDateTime` |
| `message` | `activityDisplayName` (ex.: `Add member to role`) |
| `status` / `status_detail` | `result` / `resultReason` |
| `actor.user.name` / `actor.user.uid` | `initiatedBy.user.userPrincipalName` / `initiatedBy.user.id` |
| `src_endpoint.ip` | `initiatedBy.user.ipAddress` |
| `user.name` | o **primeiro** alvo: `targetResources[0].userPrincipalName` ou `displayName` |
| `metadata.log_name` | `category` (ex.: `RoleManagement`, `UserManagement`) |
| `metadata.uid` | `id` |

O que mudou (`targetResources[].modifiedProperties`) fica no `raw`.

#### Exemplo

Evento bruto (dados sintéticos):

```json
{
  "id": "Directory_2b6c1e7a-9f3d-4a8b-b5c2-0d1e2f3a4b5c_ABCDE_12345678",
  "category": "RoleManagement",
  "correlationId": "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d",
  "result": "success",
  "resultReason": "",
  "activityDisplayName": "Add member to role",
  "activityDateTime": "2026-10-20T13:45:11Z",
  "loggedByService": "Core Directory",
  "operationType": "Assign",
  "initiatedBy": {
    "user": {
      "id": "8c7d6e5f-4a3b-2c1d-0e9f-8a7b6c5d4e3f",
      "userPrincipalName": "ana.souza@example.com",
      "ipAddress": "203.0.113.24"
    },
    "app": null
  },
  "targetResources": [
    {
      "id": "4d3c2b1a-0f9e-8d7c-6b5a-4f3e2d1c0b9a",
      "displayName": null,
      "type": "User",
      "userPrincipalName": "bruno.lima@example.com",
      "modifiedProperties": [
        {
          "displayName": "Role.DisplayName",
          "oldValue": null,
          "newValue": "\"Global Administrator\""
        }
      ]
    }
  ]
}
```

Evento normalizado:

```json
{
  "normalized": {
    "class_uid": 3001,
    "class_name": "Account Change",
    "category_uid": 3,
    "category_name": "Identity & Access Management",
    "activity_id": 0,
    "type_uid": 300100,
    "time": 1792503911000,
    "severity_id": 1,
    "severity": "Informational",
    "status": "success",
    "status_detail": "",
    "message": "Add member to role",
    "actor": {
      "user": {
        "name": "ana.souza@example.com",
        "uid": "8c7d6e5f-4a3b-2c1d-0e9f-8a7b6c5d4e3f"
      }
    },
    "src_endpoint": {
      "ip": "203.0.113.24"
    },
    "user": {
      "name": "bruno.lima@example.com"
    },
    "metadata": {
      "version": "1.8.0",
      "product": {
        "name": "Microsoft Entra ID",
        "vendor_name": "Microsoft"
      },
      "uid": "Directory_2b6c1e7a-9f3d-4a8b-b5c2-0d1e2f3a4b5c_ABCDE_12345678",
      "log_name": "RoleManagement"
    },
    "observables": [
      {
        "name": "actor",
        "type": "User Name",
        "type_id": 4,
        "value": "ana.souza@example.com"
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

## Solução de problemas

**O teste falha com `AADSTS7000215` ou `AADSTS7000222`.** O segredo está errado ou expirou. Gere outro (Passo 1, item 4) e atualize o campo *Client Secret*.

**O teste passa, mas os streams ficam em erro 403.** Falta `AuditLog.Read.All` como permissão **de aplicativo** ou falta o consentimento de administrador (Passo 2).

**Só o `signins` falha com 403, e o `audit` funciona.** O tenant não tem licença P1/P2. A resposta do Graph cita `Authentication_RequestFromNonPremiumTenantOrB2CTenant`.

**Faltam logins que aparecem no portal.** O endpoint v1.0 devolve os sign-ins interativos de usuário e os federados com sucesso. Os sign-ins **não interativos**, de **service principal** e de **identidade gerenciada**, que o portal mostra em abas separadas, não chegam por esta integração.

**"Backlog" no stream `signins`.** O ciclo leu 25.000 eventos e sobrou fila, comum em tenants grandes na primeira carga e em horário de pico. Ele drena nos ciclos seguintes.

## Limitações conhecidas

- **Só sign-ins interativos** (veja Solução de problemas).
- **Sucesso ou falha do login fica em texto** (`status`), sem `status_id`. Regras devem usar `raw.status.errorCode`.
- **Auditoria iniciada por aplicação** (`initiatedBy.app`) chega sem `actor.user`; a aplicação fica no `raw`.
- **Só o primeiro alvo** de uma auditoria vai para `user`. Os demais ficam no `raw`.
- **Recoleta histórica (backfill) não respeita o período pedido.** O coletor começa da janela padrão (última hora). A retenção dos logs no próprio Entra também limita o que existe para buscar.
