---
sidebar_position: 11
title: Veeam Backup & Replication
description: Colete as sessões de job do Veeam Backup & Replication (backup, réplica, restore) pela REST API do servidor VBR
---

# Veeam Backup & Replication

Conecte um servidor Veeam Backup & Replication (VBR) ao CentralOps para coletar as **sessões de job**: cada execução de backup, réplica ou restore, com resultado e mensagem. Backup é a última linha de defesa contra ransomware. Uma sessão que falha, um job que para de rodar ou um restore fora de hora são sinais que o SOC precisa ver, e que normalmente ficam só no console do VBR.

## Quando usar

- **Ver backup falhando.** Sessões com resultado `Failed` ou `Warning`.
- **Perceber restore inesperado.** Sessões de restore fora de uma janela de mudança.
- **Correlacionar com incidente.** Backup falhando no mesmo servidor em que o EDR acusou criptografia de arquivos.

## Como funciona

| Item | Valor |
|---|---|
| Stream | `sessions` (tipo de evento `veeam.session`) |
| Endpoint | `GET <URL do VBR>/api/v1/sessions` com `createdAfterFilter`, `orderColumn=CreationTime`, `orderAsc=true`, `skip` e `limit=200` |
| Autenticação | `POST <URL do VBR>/api/oauth2/token` com `grant_type=password` (usuário e senha). Um token por ciclo |
| Header obrigatório | `x-api-version` em toda chamada (campo *Versão da API*) |
| Frequência | a cada 5 minutos |
| Posição da coleta | o maior `creationTime` lido, recuado 2 segundos, mais o `skip` quando o ciclo para no meio |
| Duplicados | descartados pelo `id` da sessão. O CentralOps também lembra as sessões dos 2 segundos de sobreposição e não as reenvia |
| Primeira coleta | sessões criadas na última hora |
| Teto por ciclo | 50 páginas (10.000 sessões). O ciclo seguinte continua da mesma janela, no `skip` seguinte |

Se o VBR apagar sessões antigas (retenção) entre dois ciclos e o `skip` guardado passar do total, a coleta recomeça a janela do início em vez de pular sessões.

:::warning[Cada sessão é coletada uma vez, no estado em que estava]

O filtro é pela hora de **criação** da sessão. Uma sessão é coletada no primeiro ciclo depois de criada e não volta quando termina. Como a coleta roda a cada 5 minutos, um backup que leva 40 minutos chega como `state: Working` e `result: None`, e a falha dele, minutos depois, não é coletada. Sessões curtas e as da primeira carga chegam já terminadas, com o resultado final.

:::

## Quem pode fazer o quê

- **Administrador da plataforma:** cria e edita a integração (menu **Coleta -> Integrações**).
- **No VBR:** atribuir papel a um usuário exige um *Veeam Backup Administrator*.

## Pré-requisitos

- **REST API do VBR acessível** a partir dos workers do CentralOps, por padrão na porta **9419** (`https://<servidor>:9419`).
- A **versão do VBR**, para escolher o `x-api-version`.
- Acesso de **administrador no CentralOps**.

## Passo 1: Criar o usuário de leitura no VBR

1. Crie uma conta para o CentralOps, local no servidor VBR ou de domínio.
2. No console do VBR, vá em **menu principal → Users and Roles → Add** e dê à conta o papel **Veeam Backup Viewer**. É um papel só de leitura, e a REST API libera a listagem de sessões para ele.
3. Se o MFA estiver ligado no VBR, marque a conta como **conta de serviço**. O CentralOps autentica só com usuário e senha.

## Passo 2: Escolher a versão da API

O VBR exige o header `x-api-version` em toda chamada. Escolha a revisão que a sua versão aceita:

| Versão do VBR | Valores do campo *Versão da API* |
|---|---|
| 12.x | `1.1-rev0`, `1.1-rev1`, `1.1-rev2`, `1.2-rev0`, `1.2-rev1` |
| 13.x | `1.3-rev1` (e as anteriores) |

Deixar vazio usa `1.2-rev0` (VBR 12.2 ou mais novo). Servidores novos aceitam revisões antigas; o contrário não.

## Passo 3: Criar a integração no CentralOps

1. No menu lateral, vá em **Coleta -> Integrações** e adicione uma integração **Veeam Backup & Replication**.
2. Preencha:

   | Campo | O que informar |
   |---|---|
   | URL do servidor VBR | Host e porta da REST API (ex.: `https://vbr.example.com:9419`). |
   | Usuário | A conta do Passo 1. Conta de domínio: `DOMINIO\usuario`. |
   | Senha | A senha da conta. Fica guardada cifrada. |
   | Versão da API (x-api-version) | A revisão do Passo 2. Vazio = `1.2-rev0`. |
   | Verificar SSL | Desligue só se o VBR usa o certificado autoassinado que vem de fábrica. |

3. Clique em **testar a conexão**. O teste pede um token e lista uma sessão. Respostas:
   - `Conexão OK — token válido e sessões acessíveis.`
   - `Falha na autenticação (HTTP 400/401/403). Verifique usuário/senha.`
   - `Falha na autenticação (HTTP <outro>). Verifique a URL e o x-api-version (...)`
   - `Autenticou, mas /api/v1/sessions falhou (HTTP ...). Verifique o x-api-version (...) e as permissões do usuário.`
   - `Não foi possível conectar: ...`: rede, porta ou certificado.
4. Salve. A coleta começa no próximo ciclo (até 5 minutos).

## O que é coletado

Cada sessão vira um evento normalizado em OCSF 1.8, classe **Scheduled Job Activity (1006)**, atividade **Start (6)**, com o original no `raw`.

| Campo OCSF | Origem |
|---|---|
| `time` | `endTime`; sem ele (sessão em andamento), `creationTime` |
| `start_time` / `end_time` | `creationTime` / `endTime` |
| `job.name` | `name`; sem ele, `jobId` |
| `job.desc`, `metadata.event_code` | `sessionType` (ex.: `BackupJob`) |
| `job.run_state` / `job.run_state_id` | `state`: `Pending`/`Idle` → 1, `Starting`/`Resuming`/`Queued` → 2, `Working`/`InProgress` → 3, `Stopping`/`Stopped` → 4, outro → 99 |
| `severity_id` / `severity` | `result.result`: `Success`/`None` → 1 Informational, `Warning` → 3 Medium, `Failed` → 4 High |
| `status_id` / `status` | `result.result`: `Success` → 1, `Failed` → 2, `Warning` → 99, `None` → 0 |
| `message`, `status_detail` | `result.message` |
| `metadata.correlation_uid` | `jobId`; sem ele, `parentSessionId` |
| `metadata.uid` | `id` |
| `unmapped.*` | `jobId`, `sessionType`, `progressPercent`, `usn`, `resourceId`, `resourceReference` |
| `observables` | `id` da sessão e `jobId` |

### Exemplo

Sessão bruta (dados sintéticos), como chega quando já tinha terminado na hora da coleta:

```json
{
  "id": "c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f",
  "name": "Backup Job - Servidores de arquivos",
  "jobId": "a0b1c2d3-e4f5-4a6b-8c7d-9e0f1a2b3c4d",
  "sessionType": "BackupJob",
  "creationTime": "2026-10-20T02:00:03.517+00:00",
  "endTime": "2026-10-20T02:41:55.204+00:00",
  "state": "Stopped",
  "progressPercent": 100,
  "result": {
    "result": "Failed",
    "message": "Processing SRV-FILE01 Error: The network path was not found.",
    "isCanceled": false
  },
  "resourceId": "b9c8d7e6-f5a4-4b3c-2d1e-0f9a8b7c6d5e",
  "resourceReference": "/api/v1/jobs/a0b1c2d3-e4f5-4a6b-8c7d-9e0f1a2b3c4d",
  "parentSessionId": null,
  "usn": 104233
}
```

Evento normalizado:

```json
{
  "normalized": {
    "class_uid": 1006,
    "class_name": "Scheduled Job Activity",
    "category_uid": 1,
    "category_name": "System Activity",
    "activity_id": 6,
    "activity_name": "Start",
    "type_uid": 100606,
    "time": 1792464115204,
    "start_time": 1792461603517,
    "end_time": 1792464115204,
    "metadata": {
      "original_time": "2026-10-20T02:00:03.517+00:00",
      "version": "1.8.0",
      "product": {
        "name": "Veeam Backup & Replication",
        "vendor_name": "Veeam"
      },
      "uid": "c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f",
      "event_code": "BackupJob",
      "correlation_uid": "a0b1c2d3-e4f5-4a6b-8c7d-9e0f1a2b3c4d"
    },
    "severity_id": 4,
    "severity": "High",
    "status_id": 2,
    "status": "Failed",
    "status_detail": "Processing SRV-FILE01 Error: The network path was not found.",
    "message": "Processing SRV-FILE01 Error: The network path was not found.",
    "job": {
      "name": "Backup Job - Servidores de arquivos",
      "desc": "BackupJob",
      "last_run_time": 1792461603517,
      "run_state_id": 4,
      "run_state": "Stopped"
    },
    "device": {
      "type_id": 1,
      "type": "Server"
    },
    "unmapped": {
      "job_id": "a0b1c2d3-e4f5-4a6b-8c7d-9e0f1a2b3c4d",
      "session_type": "BackupJob",
      "progress_percent": 100,
      "usn": 104233,
      "resource_id": "b9c8d7e6-f5a4-4b3c-2d1e-0f9a8b7c6d5e",
      "resource_reference": "/api/v1/jobs/a0b1c2d3-e4f5-4a6b-8c7d-9e0f1a2b3c4d"
    },
    "observables": [
      {
        "name": "session_uid",
        "type": "Resource UID",
        "type_id": 10,
        "value": "c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f"
      },
      {
        "name": "job_uid",
        "type": "Resource UID",
        "type_id": 10,
        "value": "a0b1c2d3-e4f5-4a6b-8c7d-9e0f1a2b3c4d"
      }
    ]
  }
}
```

## Solução de problemas

**`Não foi possível conectar` com erro de certificado.** O VBR usa o certificado autoassinado de fábrica. Instale um certificado confiável no VBR ou desligue *Verificar SSL*.

**`Não foi possível conectar` com timeout.** A porta 9419 não está acessível a partir do CentralOps (firewall do Windows no servidor VBR ou da rede).

**`Falha na autenticação (HTTP 400)`.** Usuário ou senha errados. Para conta de domínio, use `DOMINIO\usuario`. Conta com MFA também é recusada (Passo 1, item 3).

**`Autenticou, mas /api/v1/sessions falhou`.** O `x-api-version` é mais novo do que o VBR aceita, ou a conta não tem papel no VBR. Tente uma revisão anterior.

**Backups com falha não aparecem como `Failed`.** É a limitação descrita no aviso de [Como funciona](#como-funciona): a sessão foi coletada enquanto rodava.

## Limitações conhecidas

- **Sessão coletada uma vez, no estado da hora da coleta.** O resultado final de sessões longas não chega.
- **Atividade `Start` em todas as sessões**, inclusive as já terminadas. Use `job.run_state` e `status` para saber em que ponto a sessão estava.
- **Um servidor VBR por integração.** Para vários servidores, crie uma integração para cada.
- **Recoleta histórica (backfill) não respeita o período pedido.** O coletor começa da janela padrão (última hora).
