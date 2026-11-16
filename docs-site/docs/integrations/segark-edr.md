---
sidebar_position: 4
title: SEGARK EDR
description: Colete as detecções e a telemetria de endpoint do SEGARK EDR pela API de coleta para SIEM da plataforma, com conta de serviço revogável, e normalize para OCSF 1.8
---

# SEGARK EDR

Conecte a plataforma do SEGARK EDR ao CentralOps para coletar as **detecções** (com o processo, o arquivo, a técnica MITRE ATT&CK, o veredito e os campos de ML) e, se quiser, a **telemetria de endpoint**: processos executados, atividade de arquivos, conexões de rede e consultas DNS.

A fonte é a **API de coleta para SIEM** da própria plataforma (`/api/integrations/v1`), só de leitura, autenticada por uma **conta de serviço** que o administrador cria e revoga no console. A conta enxerga só a organização dela, só os escopos e feeds concedidos e, opcionalmente, só a partir do IP do CentralOps. Toda leitura fica na trilha de auditoria da plataforma.

## Como funciona

```
Console SEGARK EDR ──► GET /api/integrations/v1/alerts  ──(Bearer, polling 1 min)──► stream alerts
                   └─► GET /api/integrations/v1/events?feed=<feed> ─────────────────► stream <feed>
```

| Stream | Escopo da conta | OCSF 1.8 |
|---|---|---|
| `alerts` | `alerts:read` | Detection Finding (2004) |
| `process_events` | `events:read` + feed `process_events` | Process Activity (1007) |
| `file_events` | `events:read` + feed `file_events` | File System Activity (1001) |
| `network_connections` | `events:read` + feed `network_connections` | Network Activity (4001) |
| `dns_events` | `events:read` + feed `dns_events` | DNS Activity (4003) |

- **Janelas UTC.** A API recebe `from` (inclusivo) e `to` (exclusivo) e devolve páginas com `next_cursor`. O CentralOps lê cada janela até `next_cursor: null` e só então avança. A janela termina **60 segundos antes de agora**, para não perder linha que ainda estava sendo gravada.
- **Detecções.** A API devolve o **estado atual** de cada detecção, por `updated_at`. Quando uma detecção muda (status, caso, nova ocorrência) ela volta com `updated_at` novo e sai de novo no pipeline: é uma atualização, com o mesmo `finding_info.uid`. Cada janela recomeça **2 minutos antes** do fim da anterior, como a plataforma recomenda; o que já saiu nessa sobreposição é pulado pela chave `id:updated_at`.
- **Telemetria.** Ordenada por recebimento (`received_at`), em janelas de até 1 hora, sem sobreposição.
- **Primeira coleta.** Últimas 24 horas de detecções e última hora de telemetria.
- **Teto por ciclo.** Até 20 páginas por stream e ciclo (4.000 detecções ou 10.000 eventos). Se sobrar backlog, o cursor guarda a página seguinte e a **Saúde do Pipeline** mostra o stream em *backlog*.
- **Throttling.** Em HTTP 429 ou 503, o CentralOps espera o `Retry-After` dentro do ciclo quando ele é curto (até 30 s, 3 tentativas). Passou disso, o ciclo termina na última página lida e retoma depois; nada lido é descartado.

## Pré-requisitos

- Plataforma do SEGARK EDR com a API de coleta para SIEM (`/api/integrations/v1`). O bloco `detection` (processo, arquivo, veredito, ML) e o `device_os` dos eventos vêm em versões da plataforma que já os exportam; em versões anteriores a coleta funciona com os campos da lista e o mapping simplesmente não preenche o que não chegou.
- Um usuário **administrador** no console, para criar a conta de serviço.
- Saída HTTPS do CentralOps para a URL do console. O console precisa ser acessado por **HTTPS**: o CentralOps recusa `http://` para não trafegar o token em claro.

## Passo 1: Criar a conta de serviço no console

1. No console do SEGARK EDR, abra **Administração → Integrações** e crie uma conta de serviço, por exemplo `centralops`.
2. Escolha os escopos:
   - **`alerts:read`** para as detecções.
   - **`events:read`** só se for coletar telemetria. Nesse caso informe a **lista de feeds** com o nome exato que aparece em *Eventos históricos*: `process_events`, `file_events`, `network_connections`, `dns_events`. Conceda só os que for coletar.
3. Opcional, recomendado: restrinja a **allowlist de IP/CIDR** ao IP de saída do CentralOps (ex.: `192.0.2.10/32`).
4. Defina a **validade** (1 a 365 dias). Anote a data: antes de vencer, crie uma conta nova, troque o token no CentralOps e só então revogue a antiga.
5. Copie o **token** (começa com `oedrsvc_`). Ele é exibido **uma única vez**; a plataforma guarda só o hash.

## Passo 2: Criar a integração no CentralOps

1. No menu lateral, vá em **Coleta → Integrações** e adicione uma integração **SEGARK EDR**.
2. Preencha:

   | Campo | O que informar |
   |---|---|
   | URL do console | O endereço do console, **sem caminho**: `https://edr.example.com` (com a porta, se não for 443). |
   | Token da conta de serviço | O token do Passo 1. Fica no armazenamento de segredos do CentralOps, criptografado, e nunca aparece em log, erro ou teste de conexão. |
   | Fluxos coletados (opcional) | Separados por vírgula: `alerts`, `process_events`, `file_events`, `network_connections`, `dns_events`. Vazio = só `alerts`. |
   | Verificar SSL | Deixe ligado. Desligue só se o console usa um certificado próprio que o CentralOps não confia. |

3. Clique em **testar a conexão**. O teste lê uma página de 1 item, dos últimos 5 minutos, de **cada** fluxo ligado, e passa só se todos responderem. Um fluxo sem o escopo ou sem o feed na allowlist aparece com a razão que a plataforma devolveu.
4. Salve. Cada fluxo ligado roda a cada minuto; os desligados não fazem nenhuma chamada.

## O que é coletado

### Detecções → Detection Finding (2004)

| Campo da API | OCSF |
|---|---|
| `id` | `finding_info.uid` e `metadata.uid` |
| `title` | `finding_info.title` e `message` |
| `severity` (`critical`…`informational`, `unknown`) | `severity_id` 5…1, 0 |
| `status` | `status_id`: `new` 1, `in_progress` 2, `resolved` 4, `false_positive` 99 (*Other*, com `status` = `False Positive`) |
| `status`, `first_seen`, `last_seen` | `activity_id`: fechada → Close (3); nova sem repetição (`first_seen` = `last_seen`) → Create (1); o resto → Update (2) |
| `updated_at` | `time` e `finding_info.modified_time` |
| `created_at` (ou `first_seen`), `first_seen`, `last_seen` | `finding_info.created_time`, `first_seen_time`, `last_seen_time`; `start_time`, `end_time` |
| `mitre` (`T1059.004`, `TA0011`…) | `finding_info.attacks[]`: `technique.uid` + `sub_technique.uid`, ou `tactic.uid`. Só o ID: o nome não é inventado |
| `source`, `detection.threat.type`, `category` | `finding_info.types` |
| `detection.threat.rule_id` ou `policy_id` | `finding_info.analytic.uid` (`name` = `policy_id`; `type_id` 4 *Learning* para ML, 1 *Rule* para regras) |
| `device_id`, `hostname`, `device_os` | `device.uid`, `device.hostname`/`name`, `device.os` (Linux 200, Windows 100) |
| `detection.process` (`pid`, `guid`, `cmdline`, `path`, `sha256`, `user`, `uid`) | `evidences[].process` (`pid`, `uid` = process_guid, `cmd_line`, `file.path`, `file.hashes` SHA-256, `user`) |
| `detection.parent` (`pid`, `guid`, `cmdline`, `path`) | `evidences[].process.parent_process` (`uid` = parent_guid) |
| `detection.file` (`path`, `sha256`) | `evidences[].file` |
| `resolution` | `status_detail` |
| `policy_id` | `metadata.event_code` |
| `detection.ml.*`, `detection.threat.*`, `case_id`, `category`, `group_key`, `external_id`, `event_id`, `closed_at` | `unmapped.segark_edr.*` (`ml_score`, `ml_band`, `ml_model`, `ml_mode`, `ml_extractor`, `engine_verdict`, `threat_source`, `threat_type`, `threat_name`, `rule_id`, `reputation_score`, `blocked`, `detect_only`…), só quando existem |

No OCSF 1.8 a Detection Finding carrega o processo e o arquivo em `evidences`. O `process_guid` é o mesmo da telemetria (`process.uid` em Process Activity): no SIEM, a detecção junta com a árvore de processos por esse campo.

### Telemetria

| Feed | Classe | Atividade | Campos principais |
|---|---|---|---|
| `process_events` | Process Activity (1007) | `execve` (Linux) e `start` (Windows) → Launch; `exit` → Terminate; carga de imagem → Other | `process.pid`, `uid` (process_guid), `cmd_line`, `file.path`, `user`; `parent_process` e `actor.process` (pid e parent_guid) |
| `file_events` | File System Activity (1001) | `create` Create, `modify` Update, `delete` Delete, `rename` Rename, `setattr` Set Attributes, `open`/`append` Open, `link` Other | `file.path`, `file_result.path` (destino de rename/link), `actor.process` |
| `network_connections` | Network Activity (4001) | Traffic (a linha resume conexões iguais com bytes somados) | `connection_info` (direção, protocolo), `src_endpoint`/`dst_endpoint` pela direção, `traffic.bytes_in/out`, `tls.sni` |
| `dns_events` | DNS Activity (4003) | Query | `query.hostname`, `query.type`, `dst_endpoint.ip` (resolvedor) |

Em todos: `time` = `observed_at` (quando o agente viu), `metadata.logged_time` = `received_at` (quando a plataforma gravou), `metadata.uid` = `event_id`, `count` = eventos iguais fundidos pelo agente, `severity_id` 1 (*Informational*). `uid` 4294967295 (desconhecido no agente) não é exportado.

Outros feeds da plataforma (contas, políticas de segurança, contêineres, syslog, FIM) não têm mapping nesta versão e não podem ser ligados.

## Segurança da integração

- **Token só no header** `Authorization: Bearer`, nunca na URL, no log, na mensagem de erro ou no resultado do teste de conexão.
- **Só HTTPS, sem redirect.** A URL passa pela política de URL do CentralOps (sem caminho, sem credencial embutida, allowlist de hosts quando configurada) e redirecionamento é recusado.
- **Só leitura.** A conta de serviço não abre sessão no console nem acessa casos, evidências, usuários ou ações remotas.
- **Fuso explícito.** Todo instante da API vem em UTC com `Z`. Se o campo que posiciona o cursor chegar sem fuso, a coleta para com um erro que nomeia o campo, em vez de supor UTC.

## Solução de problemas

| Mensagem | O que fazer |
|---|---|
| `token inválido, expirado ou revogado` | A conta venceu ou foi revogada. Crie outra no console e troque o token. |
| `acesso negado (service account scope denied)` | Falta o escopo do fluxo: `alerts:read` para `alerts`, `events:read` para telemetria. |
| `acesso negado (feed is outside the service account allowlist)` | O feed do fluxo não está na lista de feeds da conta de serviço. |
| `acesso negado (service account source network denied)` | O IP de saída do CentralOps não está na allowlist da conta. |
| `API de integração não encontrada` | Informe a URL do console sem `/api` e confira se a versão da plataforma tem a API de coleta. |
| `use https://` | O console precisa ser acessado por HTTPS. |
| `fluxo desconhecido` | Use só os nomes da tabela acima, separados por vírgula. |
| Stream em *backlog* na Saúde do Pipeline | Mais de 20 páginas por minuto: o CentralOps está alcançando. Se persistir, colete menos feeds ou reduza o que o agente exporta. |
