---
sidebar_position: 5
title: Google Cloud (Pub/Sub)
description: Colete Cloud Audit Logs (IAM, login), audit do GKE e auditoria do Cloud SQL MySQL via Pub/Sub
---

# Google Cloud (Pub/Sub)

Conecte o Google Cloud ao CentralOps para coletar, com uma integração só, três fluxos de segurança: os **Cloud Audit Logs** (IAM, mudanças de configuração, login do Workspace), o **audit do Kubernetes do GKE** e a **auditoria do banco Cloud SQL for MySQL**. O Cloud Logging exporta os logs para tópicos do Pub/Sub, e o CentralOps lê de lá.

## Quando usar

- **Monitorar o Cloud SQL (MySQL).** Login falho no banco, `GRANT`, `CREATE USER`, `DROP TABLE` e mudanças na instância (rede autorizada `0.0.0.0/0`, auditoria desligada).
- **Vigiar o IAM.** Quem ganhou `roles/owner`, quem criou chave de service account, quem se fez passar por uma service account (`GenerateAccessToken`).
- **Ver os eventos administrativos do Kubernetes (GKE).** `kubectl exec` em produção, `ClusterRoleBinding` para `cluster-admin`, leitura de secrets.
- **Juntar o login do Google Workspace / Cloud Identity** aos demais logs de identidade do SOC.

## Como funciona

```
Cloud Logging ──sink (filtro)──► tópico Pub/Sub ──► subscription ──pull──► CentralOps
```

Cada fluxo tem o seu tópico e a sua subscription. O nome da subscription define o stream:

| Subscription | Stream no CentralOps | O que chega | Classe OCSF |
|---|---|---|---|
| `centralops-audit` | `audit` | Cloud Audit Logs de todos os serviços, exceto GKE e banco | API Activity (6003); login do Workspace → Authentication (3002) |
| `centralops-gke-audit` | `gke_audit` | Audit do Kubernetes do GKE (`k8s.io`) | API Activity (6003) |
| `centralops-cloudsql-mysql` | `cloudsql_mysql` | Auditoria dentro do MySQL (`MysqlAuditEntry`) | Conexão → Authentication (3002); demais → Datastore Activity (6005) |

Crie só as subscriptions que quiser: **subscription que não existe = stream desligado**. O prefixo `centralops` pode ser trocado no campo *Prefixo das subscriptions*.

A entrega é *at-least-once*. O CentralOps só confirma a mensagem (ack) **depois** de entregar o evento e gravar a posição da coleta. Se o ciclo falhar, a mensagem volta para a fila e é reentregue, e o dedupe absorve a repetição.

:::info[Por que Pub/Sub e não a API do Logging]

A `entries.list` do Cloud Logging aceita 60 requisições por minuto por projeto, sem possibilidade de aumento, e não tem confirmação de leitura. Serve para investigar, não para coletar. Sink → Pub/Sub é o padrão usado por Splunk, Elastic, Sentinel e outros.

:::

## Quem pode fazer o quê

- **Administrador da plataforma:** cria e edita a integração (menu **Coleta -> Integrações**).
- **No Google Cloud:** criar os sinks da organização exige `roles/logging.configWriter` na organização. Criar tópicos e subscriptions exige `roles/pubsub.editor` no projeto escolhido.

## Pré-requisitos

- Um **projeto GCP para os tópicos e subscriptions**. Pode ser um projeto central de logging, diferente dos projetos monitorados.
- Acesso de **administrador no CentralOps**.
- Para o login do Workspace: o [compartilhamento de dados do Workspace com o Google Cloud](https://docs.cloud.google.com/logging/docs/audit/gsuite-audit-logging) ativado, e um sink **da organização**.

Nos comandos abaixo, troque `PROJETO` pelo projeto dos tópicos e `ORG_ID` pelo ID da organização.

## Passo 1: Ligar os logs que não vêm ligados

O **Admin Activity** já vem ligado e não tem custo. Ele cobre `SetIamPolicy`, `CreateServiceAccountKey`, mudanças de instância do Cloud SQL e `create`/`update`/`delete`/`exec` no Kubernetes. O resto é **Data Access**, que vem desligado e é cobrado por volume:

| O que você quer ver | Onde ligar Data Access |
|---|---|
| Impersonation de service account (`GenerateAccessToken`, `SignJwt`) | **Identity and Access Management (IAM) API** (`iam.googleapis.com`): `ADMIN_READ` e `DATA_READ`. A API de credenciais não é configurável sozinha. |
| Auditoria dentro do MySQL e `cloudsql.users.*` | **Cloud SQL Admin API** (`cloudsql.googleapis.com`): `DATA_READ` e `DATA_WRITE` |
| Leitura de secret e outros `get`/`list` no Kubernetes | **Kubernetes Engine API**: `ADMIN_READ` e `DATA_READ`. Alto volume: ligue só se for usar. |

Ligue pelo console em **IAM e administrador → Registros de auditoria**, marcando os tipos acima em cada serviço.

### Auditoria do MySQL no Cloud SQL

1. Ligue o plugin de auditoria:

   ```bash
   gcloud sql instances patch INSTANCIA --database-flags=cloudsql_mysql_audit=ON
   ```

   :::warning[Este comando substitui TODAS as flags e reinicia a instância]

   `--database-flags` sobrescreve a lista inteira: inclua no mesmo comando as flags que a instância já tem (`gcloud sql instances describe INSTANCIA --format='value(settings.databaseFlags)'`), senão elas voltam ao padrão. A mudança reinicia o servidor; agende uma janela.

   :::

2. Crie as regras de auditoria conectado como um usuário com `EXECUTE` nos procedimentos de auditoria. Comece estreito: logins com falha, mudanças de permissão e de esquema.

   ```sql
   -- conexões com falha (login errado, usuário inexistente)
   CALL mysql.cloudsql_create_audit_rule('*','*','*','connect','U',1,@outval,@outmsg);
   -- GRANT/REVOKE e DDL (CREATE/ALTER/DROP, inclusive de usuários), com e sem sucesso
   CALL mysql.cloudsql_create_audit_rule('*','*','*','dcl,ddl','B',1,@outval,@outmsg);
   SELECT @outval, @outmsg;  -- 0 = regra criada

   -- conferir as regras ativas
   CALL mysql.cloudsql_list_audit_rule('*',@outval,@outmsg);
   SELECT @outval, @outmsg;
   ```

   :::warning[Auditar tudo pode encher o disco da instância]

   O log de auditoria é gravado no disco local antes de ir para o Cloud Logging. Uma regra `'*','*','*','*','B'` num banco movimentado gera volume alto. Prefira regras por operação e por usuário privilegiado, e acompanhe o uso de disco depois de ligar.

   :::

## Passo 2: Criar tópicos, subscriptions e sinks

Rode no Cloud Shell. O bloco cria os três fluxos; apague o que não for usar.

```bash
PROJETO=meu-projeto-de-logs
ORG_ID=123456789012
gcloud services enable pubsub.googleapis.com logging.googleapis.com --project="$PROJETO"

for S in audit gke-audit cloudsql-mysql; do
  gcloud pubsub topics create "centralops-$S" --project="$PROJETO"
  gcloud pubsub subscriptions create "centralops-$S" --topic="centralops-$S" \
    --ack-deadline=600 --message-retention-duration=7d --project="$PROJETO"
done

# Sinks da ORGANIZAÇÃO (cobrem todos os projetos, inclusive os futuros).
gcloud logging sinks create centralops-audit \
  "pubsub.googleapis.com/projects/$PROJETO/topics/centralops-audit" \
  --organization="$ORG_ID" --include-children \
  --log-filter='logName:"cloudaudit.googleapis.com" AND NOT protoPayload.serviceName="k8s.io" AND NOT protoPayload.request.@type="type.googleapis.com/google.cloud.sql.audit.v1.MysqlAuditEntry"'

gcloud logging sinks create centralops-gke-audit \
  "pubsub.googleapis.com/projects/$PROJETO/topics/centralops-gke-audit" \
  --organization="$ORG_ID" --include-children \
  --log-filter='logName:"cloudaudit.googleapis.com" AND protoPayload.serviceName="k8s.io"'

gcloud logging sinks create centralops-cloudsql-mysql \
  "pubsub.googleapis.com/projects/$PROJETO/topics/centralops-cloudsql-mysql" \
  --organization="$ORG_ID" --include-children \
  --log-filter='resource.type="cloudsql_database" AND protoPayload.request.@type="type.googleapis.com/google.cloud.sql.audit.v1.MysqlAuditEntry"'

# Cada sink publica com uma identidade própria: ela precisa poder publicar no tópico.
for S in audit gke-audit cloudsql-mysql; do
  WRITER=$(gcloud logging sinks describe "centralops-$S" --organization="$ORG_ID" --format='value(writerIdentity)')
  gcloud pubsub topics add-iam-policy-binding "centralops-$S" \
    --member="$WRITER" --role=roles/pubsub.publisher --project="$PROJETO"
done
```

Os filtros mantêm os três fluxos separados: um evento não chega em dois tópicos.

Sem acesso à organização, crie os sinks por projeto: troque `--organization="$ORG_ID" --include-children` por `--project=PROJETO_MONITORADO`, um sink por projeto. O login do Workspace só existe no nível da organização.

## Passo 3: Credencial para o CentralOps ler as subscriptions

A permissão mínima é **Pub/Sub Subscriber** (`roles/pubsub.subscriber`) **em cada subscription**, e não no projeto inteiro.

### Opção A: chave de service account

```bash
gcloud iam service-accounts create centralops-coletor --project="$PROJETO"
SA="centralops-coletor@$PROJETO.iam.gserviceaccount.com"
for S in audit gke-audit cloudsql-mysql; do
  gcloud pubsub subscriptions add-iam-policy-binding "centralops-$S" \
    --member="serviceAccount:$SA" --role=roles/pubsub.subscriber --project="$PROJETO"
done
gcloud iam service-accounts keys create centralops-coletor.json --iam-account="$SA"
```

:::note[Organizações novas bloqueiam chave de service account]

Organizações criadas a partir de maio de 2024 vêm com a política `iam.managed.disableServiceAccountKeyCreation`. Se o último comando falhar por isso, use a opção B ou peça uma exceção da política para o projeto de logs.

:::

### Opção B: Workload Identity Federation (sem chave)

Use quando o CentralOps roda na AWS ou na Azure com uma identidade de máquina. Crie o pool e o provider de WIF para essa nuvem, dê à identidade federada o papel `roles/iam.workloadIdentityUser` na service account acima e gere o arquivo de configuração:

```bash
gcloud iam workload-identity-pools create-cred-config \
  projects/NUMERO_DO_PROJETO/locations/global/workloadIdentityPools/POOL/providers/PROVIDER \
  --service-account="$SA" --aws --output-file=centralops-wif.json   # ou --azure
```

O arquivo gerado (`type: external_account`) não contém segredo. O CentralOps aceita só configurações que falam com os endpoints de token do Google e com o endpoint de metadata da nuvem. Configurações com `credential_source` de **arquivo** ou de **executável** são recusadas, porque fariam o servidor do CentralOps ler arquivo ou rodar comando local.

## Passo 4: Criar a integração no CentralOps

1. No menu lateral, vá em **Coleta -> Integrações** e adicione uma integração **Google Cloud (Pub/Sub)**.
2. Preencha:

   | Campo | O que informar |
   |---|---|
   | Project ID | O `PROJETO` onde ficam as subscriptions. |
   | Prefixo das subscriptions | Deixe vazio para usar `centralops`. |
   | Credencial (JSON) | O conteúdo inteiro do arquivo da opção A ou B. |

3. Clique em **testar a conexão**. O teste mostra o estado de cada stream sem consumir mensagens:
   - `ok`: a credencial pode consumir a subscription.
   - `não existe (stream desligado)`: a subscription não foi criada. Normal se você não quer aquele fluxo.
   - `sem permissão` ou `sem pubsub.subscriptions.consume`: falta o papel do Passo 3 nessa subscription.

   O teste reprova se nenhuma subscription estiver utilizável.

4. Salve. A coleta roda a cada 2 minutos por stream.

## O que é coletado

Cada evento chega normalizado em OCSF 1.8, com o `LogEntry` original preservado no `raw`.

- **Audit:** `api.operation` = `methodName` (ex.: `SetIamPolicy`); `activity_id` pelo verbo do método (Create, Read, Update, Delete); `status_id` = falha quando o `status.code` do Google é diferente de 0 (inclusive Policy Denied); `actor.user` = principal; `actor.session.credential_uid` = chave de service account usada; `src_endpoint.ip` = `callerIp` (vazio quando o Google informa `private`). O diff de IAM (`serviceData.policyDelta`) fica no `raw` para as regras de detecção.
- **GKE:** igual ao audit, com `resources` = `core/v1/namespaces/<ns>/<recurso>/<nome>`. O nome do cluster fica no `raw` (`resource.labels.cluster_name`).
- **Cloud SQL MySQL:** `connect`/`disconnect` viram Logon/Logoff com `status_id` 2 quando `status = unsuccessful` e `status_code` = código do MySQL (ex.: `1045`). Demais comandos viram Datastore Activity com `database`, `table` e `query_info.query_string`.

## Solução de problemas

**O teste passa, mas nada chega.**

- Confira se o sink tem permissão de publicar: `gcloud pubsub topics get-iam-policy centralops-audit --project=PROJETO` deve listar a `writerIdentity` do sink com `roles/pubsub.publisher`.
- Data Access desligado: o stream `cloudsql_mysql` e a leitura de secret no GKE dependem do Passo 1.
- Veja se há mensagens paradas: `gcloud pubsub subscriptions pull centralops-audit --limit=1 --project=PROJETO` (sem `--auto-ack`, a mensagem continua na fila).

**Um stream aparece em Coletores com 0 eventos, sempre.** A subscription dele não existe, o que é normal quando o fluxo não é usado. O log do worker registra `subscription ... não existe — stream desligado`.

**Erro de autenticação depois de semanas funcionando.** A chave de service account foi revogada ou a política da organização passou a bloquear chaves. Gere uma nova (opção A) ou migre para a opção B, e atualize o campo *Credencial (JSON)*.

**Backfill não traz nada.** Fila não tem janela de tempo, então o backfill por período não se aplica a esta integração e é ignorado. Para reprocessar, use [seek](https://docs.cloud.google.com/pubsub/docs/replay-overview) na subscription; isso exige retenção de mensagens confirmadas.

## Limitações conhecidas

- A ordem de entrega não é garantida (Pub/Sub). Cada evento carrega o próprio `time`.
- O CentralOps ficar parado por mais tempo que a retenção da subscription (7 dias no comando acima) **perde** as mensagens mais antigas. O atraso aparece em **Saúde do Pipeline**.
- O valor de `cmd` para conexões (`connect`/`disconnect`) segue o vocabulário de operações do plugin. Confira com um log real ao ligar a regra `connect`.
