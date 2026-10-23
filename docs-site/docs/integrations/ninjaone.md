---
sidebar_position: 10
title: NinjaOne
description: Colete as atividades do NinjaOne RMM (condições disparadas, ações em dispositivos, ações de técnicos)
---

# NinjaOne

Conecte o NinjaOne RMM ao CentralOps para coletar as **atividades**: condições de monitoramento disparadas, ações executadas em dispositivos, scripts, instalação de patches e ações dos técnicos no console.

## Quando usar

- **Juntar o RMM ao SOC.** Um script rodado por um técnico num servidor às 3h é contexto para um alerta do EDR no mesmo host.
- **Ver ações administrativas.** Quem rodou o quê, em qual dispositivo.
- **Trazer condições de saúde** (disco, serviço parado) para o mesmo fluxo dos eventos de segurança.

## Como funciona

| Item | Valor |
|---|---|
| Stream | `activities` (tipo de evento `ninjaone.activity`) |
| Endpoint | `GET https://app.ninjarmm.com/v2/activities`, 200 itens por página |
| Autenticação | OAuth 2.0 `client_credentials` em `https://app.ninjarmm.com/ws/oauth/token`, escopos `monitoring management`. O token é renovado sozinho |
| Frequência | a cada 5 minutos |
| Posição da coleta | o maior `id` já lido (`after=<id>`), mais um piso de horário (`activityTimeAfter`) fixado na primeira coleta |
| Duplicados | descartados pelo `id` |
| Primeira coleta | a última hora |
| Teto por ciclo | 13 páginas (2.600 atividades). O ciclo seguinte continua do último `id` |
| Throttling | HTTP 429 respeita o `Retry-After` |

## Quem pode fazer o quê

- **Administrador da plataforma:** cria e edita a integração (menu **Coleta -> Integrações**).
- **No NinjaOne:** criar a aplicação de API exige um administrador do sistema.

## Pré-requisitos

- **Instância em `app.ninjarmm.com`.** A coleta e a renovação do token usam esse endereço fixo (veja [Limitações conhecidas](#limitações-conhecidas)).
- Acesso de **administrador no CentralOps**.

## Passo 1: Criar a aplicação de API no NinjaOne

1. No console do NinjaOne, vá em **Administration → Apps → API → Client App IDs** e clique em **Add**.
2. Em **Application Platform**, escolha **API Services (machine-to-machine)**.
3. Dê um nome, por exemplo `CentralOps`.
4. Em **Scopes**, marque **Monitoring** e **Management**. O CentralOps só lê (`GET /v2/activities`), mas o pedido de token inclui os dois escopos; sem os dois, o token é recusado.
5. Em **Allowed Grant Types**, marque **Client Credentials**.
6. Salve e copie o **Client ID** e o **Client Secret**. O segredo não aparece de novo.

## Passo 2: Criar a integração no CentralOps

1. No menu lateral, vá em **Coleta -> Integrações** e adicione uma integração **NinjaOne**.
2. Preencha:

   | Campo | O que informar |
   |---|---|
   | Client ID | O Client ID do Passo 1. |
   | Client Secret | O Client Secret do Passo 1. Fica guardado cifrado. |
   | Base URL | `https://app.ninjarmm.com` |

3. Clique em **testar a conexão**. O teste pede um token em `<Base URL>/ws/oauth/token` com os escopos `monitoring management`. Ele valida o Client ID, o segredo e os escopos.
4. Salve. A coleta começa no próximo ciclo (até 5 minutos).

## O que é coletado

Cada atividade vira um evento normalizado em OCSF 1.8, classe **API Activity (6003)**, atividade **Create (1)**, com o original no `raw`.

| Campo OCSF | Origem |
|---|---|
| `time` | `activityTime` (epoch em segundos, convertido para milissegundos) |
| `message` | `subject` |
| `severity_id` | `severity`, comparado exatamente com `critical` → 5, `high` → 4, `medium` → 3, `low` → 2, `info`/`informational` → 1, `none` → 0. Outro valor → 1 |
| `severity` | `severity`, como o NinjaOne manda |
| `status_id` / `status` | `1` / `Other` em todas as atividades |
| `actor.user.name` / `actor.user.uid` | `user.name` / `user.id` |
| `device.name` / `device.hostname` / `device.uid` | `device.systemName` / `device.dnsName` / `device.id` |
| `metadata.product.feature.name` | `activityType` (ex.: `CONDITION`, `ACTION`) |
| `metadata.uid` | `id` |

O texto completo (`message`), o estado (`status`, `statusCode`) e a origem (`sourceName`) ficam no `raw`.

### Exemplo

Atividade bruta (dados sintéticos):

```json
{
  "id": 987654,
  "activityTime": 1792503727.0,
  "deviceId": 412,
  "severity": "high",
  "priority": "HIGH",
  "seriesUid": "3e2d1c0b-9a8f-7e6d-5c4b-3a2f1e0d9c8b",
  "activityType": "CONDITION",
  "statusCode": "TRIGGERED",
  "status": "TRIGGERED",
  "sourceName": "Disk space",
  "subject": "Disk C: free space below 10%",
  "message": "Free space on C: is 6.2% (12 GB)",
  "type": "Condition",
  "user": {
    "id": 77,
    "name": "ana.souza@example.com"
  },
  "device": {
    "id": 412,
    "systemName": "SRV-FILE01",
    "dnsName": "srv-file01.corp.example.com"
  }
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
    "activity_id": 1,
    "activity_name": "Create",
    "type_uid": 600301,
    "time": 1792503727000,
    "severity_id": 4,
    "severity": "high",
    "status_id": 1,
    "status": "Other",
    "message": "Disk C: free space below 10%",
    "actor": {
      "user": {
        "name": "ana.souza@example.com",
        "uid": 77
      }
    },
    "device": {
      "name": "SRV-FILE01",
      "hostname": "srv-file01.corp.example.com",
      "uid": 412
    },
    "metadata": {
      "version": "1.8.0",
      "product": {
        "name": "NinjaOne",
        "vendor_name": "NinjaOne",
        "feature": {
          "name": "CONDITION"
        }
      },
      "uid": 987654
    }
  }
}
```

## Solução de problemas

**O teste falha com erro de token.** Client ID ou segredo errados, grant type *Client Credentials* não marcado, ou um dos escopos (Monitoring, Management) faltando na aplicação.

**O teste passa, mas o stream fica em erro 401.** A Base URL aponta para uma instância regional (por exemplo, a da Europa). O teste usou a URL do formulário, mas a coleta pede o token em `app.ninjarmm.com`, onde a aplicação não existe. Hoje não há contorno: veja Limitações conhecidas.

**"Backlog" em Saúde do Pipeline.** O ciclo leu 2.600 atividades e sobrou fila. Comum na primeira carga de um ambiente grande; drena nos ciclos seguintes.

## Limitações conhecidas

- **Só instâncias em `app.ninjarmm.com`.** O campo *Base URL* vale apenas para o teste de conexão. A renovação do token e a coleta usam `app.ninjarmm.com` fixo, então instâncias regionais passam no teste e falham na coleta.
- **Severidade sensível a maiúsculas.** O mapeamento compara o texto exato, em minúsculas. Um valor fora da lista (inclusive em maiúsculas) vira `severity_id` 1. Confira com um evento real na [captura ao vivo](../operations/live-capture.md) e ajuste o mapeamento em **Normaliza -> Mapeamentos** se precisar.
- **Toda atividade sai como API Activity / Create** com `status` `Other`. Para filtrar por tipo, use `metadata.product.feature.name` (o `activityType`).
- **Sem indicador de atraso dos dados.** A posição é um `id`, sem horário. Em **Saúde do Pipeline**, este stream mostra só a última coleta e o "Backlog".
- **Recoleta histórica (backfill) não respeita o período pedido.** O coletor começa da janela padrão (última hora).
