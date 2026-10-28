---
sidebar_position: 9
title: Google Workspace
description: Colete os logs de auditoria do Google Workspace (login, 2SV, Admin console, OAuth, SAML) e os alertas do Alert Center
---

# Google Workspace

Conecte o Google Workspace ao CentralOps para coletar os **logs de auditoria** da Reports API (login, verificação em duas etapas, mudanças no Admin console, consentimento OAuth, SSO SAML, grupos, dispositivos móveis e regras de DLP) e os **alertas do Alert Center** (senha vazada, login suspeito, phishing reportado, dispositivo comprometido).

## Quando usar

- Detectar ataque a contas: força bruta, login suspeito, desafio de login falho, alerta de ataque governamental.
- Detectar persistência e escalada: papel de admin concedido, 2SV desligada, usuário criado ou reativado, senha trocada pelo admin.
- Detectar **phishing de consentimento OAuth**: um usuário autorizando um app de terceiros com escopo de e-mail ou Drive (log `token`).
- Levar para o SIEM os alertas que o próprio Google já gera, com o link da ferramenta de investigação.

## Como funciona

A integração tem dois streams, lidos por polling a cada 5 minutos:

| Stream | API | O que traz |
|---|---|---|
| `activity` | [Admin SDK Reports API](https://developers.google.com/workspace/admin/reports/reference/rest/v1/activities/list) (`activities.list`) | Um registro por evento de auditoria, de cada aplicação configurada. |
| `alerts` | [Alert Center API](https://developers.google.com/workspace/admin/alertcenter/reference/rest/v1beta1/alerts/list) (`alerts.list`) | Um registro por alerta novo (janela por `createTime`). |

As duas APIs exigem agir em nome de um usuário do domínio. Por isso a credencial é uma **service account com delegação em todo o domínio**: ela assina um token em nome do admin que você indicar, com dois escopos somente leitura.

### Aplicações lidas

Por padrão o stream `activity` lê as aplicações de segurança com volume baixo:

`login`, `saml`, `admin`, `user_accounts`, `token`, `groups`, `mobile`, `rules`

Para ler outras, preencha o campo **Aplicações** com a lista completa (separada por vírgula). As mais pedidas:

- `drive`: volume alto. A maioria dos eventos do Drive só é registrada para arquivos de usuários em edições Enterprise ou Frontline ([Drive log events](https://knowledge.workspace.google.com/admin/reports/drive-log-events)).
- `gmail`: volume alto.
- `chrome`, `context_aware_access`, `access_transparency`: dependem de edição ou licença.

Aplicação que a edição não oferece, ou que o admin não pode ler, é pulada sem derrubar as outras. O log do worker registra `HTTP 403 em <aplicação> — aplicação pulada`.

### Atraso de publicação

O Google não publica todos os logs na mesma velocidade. Cada aplicação tem cursor próprio, e a janela seguinte recomeça antes do fim da anterior para ainda achar o evento publicado com atraso. A repetição é descartada pelo CentralOps.

| Aplicação | Atraso oficial | Sobreposição usada |
|---|---|---|
| `login`, `saml`, `admin`, `rules`, `mobile`, `drive`, `gmail`, `chrome` | quase tempo real (minutos) | 20 min |
| `user_accounts` | dezenas de minutos | 1 h |
| `groups`, `calendar` | dezenas de minutos a algumas horas | 3 h |
| `token` (OAuth), `cloud_search` | até algumas horas | 4 h |

Fonte: [Data retention and lag times](https://knowledge.workspace.google.com/admin/reports/data-retention-and-lag-times). O próprio Google avisa que atrasos maiores acontecem ocasionalmente; um evento publicado depois da sobreposição não é lido.

## Pré-requisitos

- Um projeto do Google Cloud (pode ser um projeto só para isso).
- Um **super admin** do Workspace para autorizar a delegação no Admin console.
- Um usuário do domínio, ativo, para o CentralOps impersonar (veja o Passo 3).

## Passo 1: Ligar as APIs no projeto

No [console do Google Cloud](https://console.cloud.google.com/), no projeto escolhido, ative:

- **Admin SDK API**
- **Google Workspace Alert Center API**

Ou pela linha de comando:

```bash
gcloud services enable admin.googleapis.com alertcenter.googleapis.com --project=PROJETO
```

## Passo 2: Criar a service account e a chave

1. Em **IAM e administrador -> Contas de serviço**, crie uma service account (ex.: `centralops-workspace`). Ela não precisa de nenhum papel no projeto.
2. Na aba **Chaves**, crie uma chave do tipo **JSON** e guarde o arquivo.
3. Anote o **ID exclusivo** (Client ID numérico) da service account, na aba **Detalhes**.

:::note[Organizações novas bloqueiam chave de service account]

A política `iam.disableServiceAccountKeyCreation` vem ativa em organizações criadas a partir de 2024. Peça ao administrador uma exceção para este projeto. Workload Identity Federation não serve aqui: a delegação em todo o domínio exige que a service account assine o token em nome do admin, e só a chave faz isso.

:::

## Passo 3: Autorizar a delegação em todo o domínio

1. No [Admin console](https://admin.google.com/), como super admin, vá em **Segurança -> Controles de acesso e dados -> Controles de API -> Delegação em todo o domínio**.
2. Clique em **Adicionar novo** e informe:
   - **ID do cliente:** o Client ID numérico do Passo 2.
   - **Escopos OAuth** (separados por vírgula):

     ```text
     https://www.googleapis.com/auth/admin.reports.audit.readonly,https://www.googleapis.com/auth/apps.alerts
     ```

3. Autorize. A propagação costuma levar minutos, mas o Google documenta que pode levar até 24 horas ([guia de delegação](https://developers.google.com/workspace/admin/reports/v1/guides/delegation)).

### Qual usuário impersonar

O token vale só para o que esse usuário enxerga. Use um usuário dedicado com um papel de admin personalizado que tenha os privilégios **Relatórios** e **Central de alertas (visualização)**, ou um super admin. A referência da API não documenta o papel mínimo; se o teste de conexão mostrar `sem permissão` em um stream, falta o privilégio correspondente.

## Passo 4: Criar a integração no CentralOps

1. No menu lateral, vá em **Coleta -> Integrações** e adicione uma integração **Google Workspace**.
2. Preencha:

   | Campo | O que informar |
   |---|---|
   | Chave da service account (JSON) | O conteúdo inteiro do arquivo do Passo 2. |
   | E-mail do admin a impersonar | O usuário do Passo 3 (ex.: `auditoria@empresa.com`). |
   | Customer ID | Opcional. Vazio usa o cliente do admin. Em ambientes com revenda, copie de **Conta -> Configurações da conta** (ex.: `C01abc23d`). |
   | Aplicações | Opcional. Vazio usa a lista padrão acima. |

3. Clique em **testar a conexão**. O teste emite o token e faz uma leitura de um registro em cada API:
   - `activity (login): ok` e `alerts: ok`: tudo certo.
   - `sem permissão`: o usuário impersonado não tem o privilégio daquele stream, ou o escopo não foi autorizado na delegação.
   - *Delegação em todo o domínio não autorizada*: o Client ID ou os escopos do Passo 3 não batem, ou a autorização ainda não propagou.
   - *O Google recusou a impersonação*: o e-mail não é de um usuário ativo do domínio, ou a chave foi revogada.

   O teste reprova se nenhum stream estiver utilizável. A mensagem de erro do Google não é repassada para a tela.

4. Salve. Na primeira execução, cada stream lê a última hora.

## O que é coletado

Cada evento chega normalizado em OCSF 1.8, com o registro original preservado no `raw`. No stream `activity`, uma atividade do Google pode agrupar vários eventos (ex.: `login_success` e `login_verification` no mesmo instante); o CentralOps emite **um registro por evento**, com a atividade (`id`, `actor`, `ipAddress`), o evento em `event` e os parâmetros achatados em `event.params` (ex.: `event.params.login_type`).

| Evento do Google | Classe OCSF | Detalhe |
|---|---|---|
| `login_success`, `login_failure` (login e SAML) | Authentication (3002), Logon | `status_id` Success/Failure; `status_detail` = `login_failure_type`. |
| `login_challenge`, `login_verification` | Authentication, Logon | Resultado vem de `login_challenge_status`. |
| `suspicious_login*`, `gov_attack_warning` | Authentication, Logon | Severidade High. |
| `logout` | Authentication, Logoff | |
| `CREATE_USER`, `DELETE_USER`, `SUSPEND_USER`, `UNSUSPEND_USER`, `account_disabled_*` | Account Change (3001) | Create, Delete, Disable, Enable. |
| `ASSIGN_ROLE`, `GRANT_ADMIN_PRIVILEGE` / `UNASSIGN_ROLE`, `REVOKE_ADMIN_PRIVILEGE` | Account Change | Attach Policy / Detach Policy. |
| `2sv_enroll`, `2sv_disable`, `titanium_*`, `passkey_*` | Account Change | MFA Factor Enable / Disable. |
| `password_edit` / `CHANGE_PASSWORD` (admin) | Account Change | Password Change / Password Reset. |
| Demais (Drive, token, grupos, configurações) | API Activity (6003) | Verbo pelo nome do evento: Create, Read, Update, Delete ou Other. |
| Alerta do Alert Center | Detection Finding (2004) | `finding_info.title` = tipo do alerta; severidade de `metadata.severity`. |

Campos principais: `actor.user.email_addr` = quem fez; `user.email_addr` = conta afetada (`USER_EMAIL` dos eventos de admin, `affected_email_address` dos de login); `src_endpoint.ip` = IP de origem; `actor.app_name`/`actor.app_uid` = app que recebeu o consentimento OAuth; `resources` = documento do Drive.

## Limites e custos

- **Retenção:** 6 meses de logs de auditoria na Reports API. Backfill por período funciona dentro desse limite.
- **Cota da Reports API:** 2.400 consultas por minuto por usuário por projeto. O CentralOps usa só a janela de tempo, sem filtro: consultas com filtro (`eventName`, `filters`, `actorIpAddress`…) têm cota própria de 250 por minuto ([limites](https://developers.google.com/workspace/admin/reports/v1/limits)). Cota estourada volta como HTTP 503 e o ciclo é refeito depois.
- **Cota do Alert Center:** 1.000 consultas por segundo por projeto e 150 por usuário ([limites](https://developers.google.com/workspace/admin/alertcenter/limits)).
- **Preço:** as páginas da Reports API e do Alert Center não mencionam cobrança. O que varia com a edição é o conteúdo: eventos do Drive (Enterprise/Frontline), logs de dispositivos (dependem da assinatura) e a exportação para o BigQuery, que só existe em edições Enterprise, Education Standard/Plus, Frontline e Enterprise Essentials Plus ([BigQuery](https://knowledge.workspace.google.com/admin/reports/about-reporting-logs-and-bigquery)). O Alert Center está disponível para todos os clientes do Workspace, segundo o [guia da API](https://developers.google.com/workspace/admin/alertcenter/guides).

## Relação com a integração Google Cloud (Pub/Sub)

A integração [Google Cloud (Pub/Sub)](./google-cloud.md) também pode trazer o login do Workspace (`login.googleapis.com`), quando a organização compartilha os logs do Workspace com o Google Cloud e há um sink de organização. Se as duas estiverem ligadas, o mesmo login chega duas vezes, por caminhos diferentes. Escolha uma: esta integração cobre também Admin console, OAuth, SAML e o Alert Center sem precisar de sink.

## Solução de problemas

**O teste passa, mas uma aplicação nunca traz nada.** Procure no log do worker `aplicação pulada`. HTTP 403 numa aplicação só costuma ser edição sem aquele log; o cursor dela fica parado e ela não entra no cálculo de atraso.

**Eventos de `token` chegam horas depois.** É o atraso de publicação do Google para esse log, não da coleta.

**Erro de autenticação depois de semanas funcionando.** A chave foi revogada, o usuário impersonado foi suspenso ou perdeu o papel, ou a delegação foi removida. Atualize o campo que mudou; a chave é rotacionável pelo formulário.

## Limitações conhecidas

- Alertas são lidos uma vez, na criação. Mudança posterior de status no Alert Center não gera novo evento.
- Evento que o Google publica depois da sobreposição da aplicação (tabela acima) não é lido. O Google também avisa que, raramente, eventos podem não ser registrados.
- Só a nuvem pública do Google. Credencial por Workload Identity Federation não é suportada (veja o Passo 2).
