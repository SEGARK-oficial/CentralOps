---
sidebar_position: 9
title: Okta
description: Colete o System Log da Okta (logins, MFA, ciclo de vida de usuários) com um token de API
---

# Okta

Conecte uma org Okta ao CentralOps para coletar o **System Log**: logins, desafios e falhas de MFA, criação e desativação de usuário, mudanças de política e ações de administrador.

## Quando usar

- **Detectar fadiga de MFA.** Muitos `user.mfa.okta_verify.deny_push` para o mesmo usuário em pouco tempo.
- **Ver ataque a senha.** `user.session.start` com `outcome.result = FAILURE` vindo de um mesmo IP.
- **Vigiar administração.** `user.account.privilege.grant`, `policy.lifecycle.update`, `system.api_token.create`.

## Como funciona

| Item | Valor |
|---|---|
| Stream | `system_log` (tipo de evento `okta.system_log`) |
| Endpoint | `GET <Org URL>/api/v1/logs`, modo *polling* (`sortOrder=ASCENDING`, sem `until`), 200 eventos por página |
| Autenticação | header `Authorization: SSWS <token>` |
| Frequência | a cada 2 minutos |
| Posição da coleta | a URL inteira do link `rel="next"` que a Okta devolve. O horário de início (`since`) só é usado na primeira coleta |
| Duplicados | descartados pelo `uuid` |
| Primeira coleta | a última hora |
| Teto por ciclo | 50 páginas (10.000 eventos). O ciclo seguinte continua do próximo link |
| Throttling | HTTP 429 espera até o `X-Rate-Limit-Reset` da Okta |

No modo polling, a Okta ordena pelo momento em que gravou o evento e sempre devolve um próximo link. Isso pega eventos gravados com atraso sem perder nenhum. Se a Okta recusar um link guardado, a coleta volta ao horário de início guardado junto e o dedupe absorve a releitura.

## Quem pode fazer o quê

- **Administrador da plataforma:** cria e edita a integração (menu **Coleta -> Integrações**).
- **Na Okta:** criar o token exige um administrador. O token tem as mesmas permissões de quem o criou.

## Pré-requisitos

- A **URL da sua org Okta** (ex.: `https://example.okta.com`).
- Um **administrador de serviço** na Okta para ser dono do token. Recomendamos uma conta dedicada com o papel **Read-Only Administrator**, que lê o System Log e não altera nada.

## Passo 1: Criar o token de API

1. Entre no Admin Console da Okta com a conta de serviço.
2. Vá em **Security → API → Tokens → Create token**.
3. Dê um nome, por exemplo `centralops`, e copie o valor do token. Ele não aparece de novo.

:::warning[O token vive e morre com o administrador]

O token tem as permissões do administrador que o criou e é revogado se essa conta for desativada. Um token criado na conta pessoal de alguém para de funcionar quando essa pessoa sai da empresa. A Okta também expira tokens que ficam 30 dias sem uso, o que não acontece enquanto a integração estiver coletando.

:::

## Passo 2: Criar a integração no CentralOps

1. No menu lateral, vá em **Coleta -> Integrações** e adicione uma integração **Okta**.
2. Preencha:

   | Campo | O que informar |
   |---|---|
   | Org URL | A URL da org, com `https://` (ex.: `https://example.okta.com`). Domínio próprio também serve. |
   | API Token (SSWS) | O token do Passo 1. Fica guardado cifrado. |

3. Clique em **testar a conexão**. O teste faz `GET /api/v1/logs?limit=1` com o token. Ele valida o token e a leitura do System Log. Respostas:
   - `Conexão OK — token válido.`
   - `Falha (HTTP 401). Verifique o API Token.` (ou 403): token errado, revogado ou sem permissão.
   - `Falha (HTTP <outro>). Verifique a Org URL.`: URL errada.
4. Salve. A coleta começa no próximo ciclo (até 2 minutos).

## O que é coletado

Cada evento do System Log vira um evento normalizado em OCSF 1.8, classe **Authentication (3002)**, atividade **Logon (1)**, com o original no `raw`.

| Campo OCSF | Origem |
|---|---|
| `time` | `published` |
| `metadata.event_code` | `eventType` (ex.: `user.session.start`) |
| `message` | `displayMessage` |
| `severity_id` | `severity`: `DEBUG`/`INFO` → 1, `WARN` → 3, `ERROR` → 5 |
| `severity` | `severity`, como a Okta manda (ex.: `WARN`) |
| `status_id` | `outcome.result`: `SUCCESS`/`ALLOW`/`CHALLENGE` → 1 (sucesso), `FAILURE`/`DENY` → 2 (falha), outros (`SKIPPED`, `UNKNOWN`) → 0 |
| `status` / `status_detail` | `outcome.result` / `outcome.reason` |
| `user.name` / `user.uid` / `user.full_name` | `actor.alternateId` / `actor.id` / `actor.displayName` |
| `src_endpoint.ip` | `client.ipAddress` |
| `metadata.uid` | `uuid` |
| `observables` | usuário e IP |

Os alvos da ação (`target[]`), o agente do usuário, a geolocalização e o contexto de segurança (`securityContext`) ficam no `raw`.

### Exemplo

Evento bruto (dados sintéticos):

```json
{
  "uuid": "9a8b7c6d-5e4f-11ef-a1b2-0123456789ab",
  "published": "2026-10-20T13:42:07.312Z",
  "eventType": "user.session.start",
  "version": "0",
  "severity": "WARN",
  "displayMessage": "User login to Okta",
  "actor": {
    "id": "00u1a2b3c4d5e6f7g8h9",
    "type": "User",
    "alternateId": "ana.souza@example.com",
    "displayName": "Ana Souza"
  },
  "client": {
    "ipAddress": "198.51.100.17",
    "userAgent": {
      "rawUserAgent": "Mozilla/5.0",
      "os": "Windows 10",
      "browser": "CHROME"
    },
    "zone": "null"
  },
  "outcome": {
    "result": "FAILURE",
    "reason": "VERIFICATION_ERROR"
  },
  "authenticationContext": {
    "authenticationStep": 0,
    "externalSessionId": "unknown"
  },
  "securityContext": {
    "asNumber": 64496,
    "isProxy": false
  },
  "target": null
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
    "time": 1792503727312,
    "severity_id": 3,
    "severity": "WARN",
    "status_id": 2,
    "status": "FAILURE",
    "status_detail": "VERIFICATION_ERROR",
    "message": "User login to Okta",
    "user": {
      "name": "ana.souza@example.com",
      "uid": "00u1a2b3c4d5e6f7g8h9",
      "full_name": "Ana Souza"
    },
    "src_endpoint": {
      "ip": "198.51.100.17"
    },
    "metadata": {
      "version": "1.8.0",
      "product": {
        "name": "Okta",
        "vendor_name": "Okta"
      },
      "uid": "9a8b7c6d-5e4f-11ef-a1b2-0123456789ab",
      "event_code": "user.session.start"
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
        "value": "198.51.100.17"
      }
    ]
  }
}
```

## Solução de problemas

**O teste falha com HTTP 401.** Token copiado errado, revogado, ou o administrador dono dele foi desativado. Crie outro (Passo 1) e atualize o campo *API Token*.

**O teste falha com HTTP 403.** O administrador dono do token não tem permissão para ler o System Log. Dê a ele o papel Read-Only Administrator e crie o token de novo.

**Eventos chegam com minutos de atraso em horário de pico.** A coleta lê 10.000 eventos por ciclo de 2 minutos. Acima disso aparece "Backlog" em **Saúde do Pipeline**, e o atraso drena nos ciclos seguintes.

**Erros 429 frequentes.** O limite de taxa do `/api/v1/logs` é da org inteira, dividido com qualquer outra ferramenta que leia o System Log (outro SIEM, scripts). A coleta espera o tempo que a Okta indica e retoma.

## Limitações conhecidas

- **Todo evento sai como Authentication / Logon**, inclusive mudanças de usuário e de política. Para filtrar por tipo, use `metadata.event_code` (o `eventType`).
- **`user` é quem fez a ação** (`actor`), não quem sofreu. Numa ação de administrador sobre outro usuário, o alvo está em `raw.target`.
- **Só token SSWS.** Aplicação OAuth com escopo `okta.logs.read` não é suportada.
- **Sem indicador de atraso dos dados.** A posição da coleta é um link opaco, sem horário. Em **Saúde do Pipeline**, este stream mostra só a última coleta e o "Backlog".
- **Recoleta histórica (backfill) não respeita o período pedido.** O coletor começa da janela padrão (última hora).
