---
sidebar_position: 6
title: Microsoft 365
description: Colete a auditoria unificada do Microsoft 365 (Entra, Exchange, SharePoint/OneDrive, Teams e DLP) pela Office 365 Management Activity API
---

# Microsoft 365

Conecte o Microsoft 365 ao CentralOps para coletar a **auditoria unificada** do tenant (o mesmo dado da busca de auditoria do Microsoft Purview) pela **Office 365 Management Activity API**. Uma integração e uma credencial cobrem:

- **Entra ID**: logon com sucesso e falho, e a administração do diretório (criar usuário, resetar senha, dar papel de administrador);
- **Exchange Online**: regras de caixa de entrada, encaminhamento, permissão em caixa de terceiros, exclusão definitiva e, com Audit Premium, o acesso a mensagens (`MailItemsAccessed`);
- **SharePoint e OneDrive**: download, sincronização, compartilhamento (inclusive link anônimo) e exclusão de arquivo;
- **Teams, Power Platform, Defender for Office e os demais workloads** (`Audit.General`);
- **DLP**: casamentos de política de prevenção de perda de dados em todos os workloads.

## Quando usar

- **Comprometimento de conta de e-mail (BEC).** Regra de caixa criada para encaminhar ou esconder mensagens (`New-InboxRule`, `Set-InboxRule`), encaminhamento externo no `Set-Mailbox`, `Add-MailboxPermission` em caixa de executivo.
- **Exfiltração de dados.** Download em massa no OneDrive/SharePoint (`FileDownloaded`, `FileSyncDownloadedFull`), link anônimo criado (`AnonymousLinkCreated`), casamento de DLP.
- **Escalada de privilégio no diretório.** `Add member to role.` (por exemplo, *Global Administrator*), `Reset user password.`, credencial nova em service principal (`Add service principal credentials.`).
- **Ataque de senha.** Sequência de `UserLoginFailed` por usuário ou por IP.

## Como funciona

```
Microsoft 365 ── auditoria unificada ──► blobs de conteúdo (por content type)
                                            │
                    CentralOps (polling a cada 5 min, token do Entra) ──► 5 streams
```

A API não entrega eventos um a um: a Microsoft agrupa os eventos em **blobs de conteúdo**. O CentralOps lista os blobs que ficaram disponíveis numa janela de tempo, baixa cada um e entrega os eventos. Cada *content type* é um stream, com a sua própria **assinatura** do lado da Microsoft:

| Stream | Content type | O que chega | Classe OCSF |
|---|---|---|---|
| `audit_aad` | `Audit.AzureActiveDirectory` | Logon no Entra e operações de diretório | Logon → Authentication (3002); conta/papel → Account Change (3001); demais → API Activity (6003) |
| `audit_exchange` | `Audit.Exchange` | Admin do Exchange e auditoria de caixa | API Activity (6003) |
| `audit_sharepoint` | `Audit.SharePoint` | SharePoint e OneDrive | API Activity (6003) |
| `audit_general` | `Audit.General` | Teams, Power Platform, Defender for Office, Purview e demais | API Activity (6003) |
| `dlp` | `DLP.All` | Casamentos de política de DLP de todos os workloads | Detection Finding (2004) |

- **Assinatura automática.** No primeiro ciclo o CentralOps inicia a assinatura de cada content type (`subscriptions/start`). Uma assinatura que já existe é aproveitada. A Microsoft avisa que, numa assinatura nova, o **primeiro conteúdo pode levar até 12 horas** para aparecer: stream sem evento nas primeiras horas é normal.
- **A janela anda sobre a disponibilidade do blob, não sobre a hora do evento.** Por isso evento que a Microsoft publica atrasado não se perde: ele chega num blob novo, e o blob novo cai na próxima janela.

:::info[Diferença para as integrações Entra ID e Defender]

A [integração Microsoft Entra ID](./microsoft-entra-id.md) lê o Graph: sign-ins com detalhe de risco, Conditional Access e localização. O `audit_aad` daqui traz logon e auditoria de diretório **no formato da auditoria unificada**, com menos detalhe, mas sem exigir licença Entra ID P1/P2. Se você usa as duas, os logons aparecem nas duas. A [integração Microsoft Defender](./microsoft-defender.md) traz **alertas e incidentes** já correlacionados pelo Defender; esta traz os **eventos de atividade** que as suas próprias regras de detecção vão analisar.

:::

## Segurança da integração

- **Só leitura.** A permissão `ActivityFeed.Read` lê a auditoria e não altera nada no tenant.
- **O token não sai da Microsoft.** Os links que a API devolve (`NextPageUri` e o endereço de cada blob) só são seguidos em `https://manage.office.com`; o CentralOps não segue redirecionamento. Um link apontando para outro host é recusado e registrado no log.
- **Valores sensíveis do DLP não saem do coletor.** Com a permissão de DLP, a API devolve o **trecho** que casou a política (número de cartão, CPF). O CentralOps remove esses valores (`DetectedValues`) antes de qualquer processamento e guarda só a contagem (`DetectedValuesRedacted`). O tipo de informação sensível, a confiança e a quantidade continuam no evento.
- **Mensagens de erro sem segredo.** O teste de conexão e o log mostram só o código do erro (`AADSTS…` do Entra, `AF…` da API), nunca o corpo da resposta.

## Pré-requisitos

- **Auditoria unificada ligada** no Microsoft Purview (*Auditoria* → *Iniciar gravação da atividade do usuário e do administrador*). Ela vem ligada por padrão nos tenants atuais, mas confira: com ela desligada não há conteúdo.
- **Licença com auditoria.** Audit (Standard) está nos planos Microsoft 365/Office 365 E3, E5, Business Basic/Standard/Premium e equivalentes de governo e educação, e já inclui o acesso pela Management Activity API. Ver a seção [Licença e custo](#licença-e-custo).
- **Um app registration no Microsoft Entra ID**, com permissão de aplicativo e consentimento de um administrador (Passo 1).

## Passo 1: Criar o app e dar a permissão de leitura

1. No [portal do Entra](https://entra.microsoft.com), vá em **Aplicativos → Registros de aplicativo → Novo registro**. Dê um nome (por exemplo, `centralops-m365`) e crie.
2. Anote o **Application (client) ID** e o **Directory (tenant) ID** da página de visão geral.
3. Em **Permissões de API → Adicionar uma permissão → Office 365 Management APIs → Permissões de aplicativo**, marque:
   - `ActivityFeed.Read`: obrigatória, cobre os quatro streams de auditoria;
   - `ActivityFeed.ReadDlp`: opcional, só para o stream `dlp`.
4. Clique em **Conceder consentimento do administrador**.
5. Em **Certificados e segredos → Novo segredo do cliente**, crie um segredo e copie o **Valor** (ele só aparece uma vez).

:::tip[Sem a permissão de DLP]

Sem `ActivityFeed.ReadDlp`, o stream `dlp` fica desligado: o worker registra `stream desligado` a cada ciclo e os outros quatro streams seguem normalmente. Conceda a permissão depois, se quiser o DLP; não é preciso recriar a integração.

:::

## Passo 2: Criar a integração no CentralOps

1. No menu lateral, vá em **Coleta -> Integrações** e adicione uma integração **Microsoft 365**.
2. Preencha:

   | Campo | O que informar |
   |---|---|
   | Tenant ID | O *Directory (tenant) ID* do Passo 1. Precisa ser o GUID; o domínio (`contoso.onmicrosoft.com`) não é aceito. |
   | Client ID | O *Application (client) ID* do Passo 1. |
   | Client Secret | O valor do segredo do Passo 1. |

3. Clique em **testar a conexão**. O teste obtém um token e lista as assinaturas do tenant, sem baixar nenhum evento. Para cada stream ele mostra:
   - `assinatura ativa`: já existe uma assinatura (de uma ferramenta anterior ou de um ciclo do CentralOps);
   - `sem assinatura (iniciada no 1º ciclo)`: normal numa integração nova;
   - `assinatura disabled (será reiniciada no 1º ciclo)`: alguém parou a assinatura; o CentralOps a reinicia.

   Mensagens de falha:
   - `O Entra recusou a credencial: …`: Tenant ID, Client ID ou segredo errados, ou segredo expirado. O código `AADSTS` diz qual (ex.: `AADSTS7000215` = segredo inválido).
   - `A Management API recusou o app (HTTP 401/403 …)`: falta `ActivityFeed.Read` ou o consentimento do administrador.
   - `Falha ao listar as assinaturas … auditoria unificada`: confira se a auditoria está ligada.

4. Salve. Os cinco streams rodam a cada 5 minutos.

## O que é coletado

Cada evento chega normalizado em OCSF 1.8, com o registro original preservado no `raw`. `cloud.provider` = `Microsoft`; `cloud.org.uid` = tenant; `metadata.product.feature.name` = workload (`Exchange`, `SharePoint`, `OneDrive`, `MicrosoftTeams`…); `metadata.uid` = `Id` do evento, que também é a chave do dedupe.

**Auditoria (`audit_*`)**

- `UserLoggedIn` → **Authentication / Logon**, sucesso. `UserLoginFailed` → **Authentication / Logon**, **falha**, mesmo quando o `ResultStatus` diz *Success* (o serviço de token marca assim; a falha é a própria operação). `status_detail` = `LogonError` (ex.: `InvalidUserNameOrPassword`). `user` = quem tentou logar.
- Operações de conta e papel do diretório → **Account Change**, com `user` = **conta alterada** (`ObjectId`) e `actor` = **quem alterou**: `Add user.` (Create), `Delete user.` (Delete), `Reset user password.` (Password Reset), `Change user password.` (Password Change), `Disable account.`/`Enable account.`, `Add member to role.` (Attach Policy), `Remove member from role.` (Detach Policy), `Update user.` (Other).
- Todo o resto → **API Activity**, pelo verbo da operação: `FileDownloaded`, `FileAccessed`, `MailItemsAccessed` → Read; `New-InboxRule`, `Add-MailboxPermission`, `AnonymousLinkCreated` → Create; `Set-Mailbox`, `Set-InboxRule`, `FileModified` → Update; `HardDelete`, `FileDeleted`, `Remove-*` → Delete; demais (ex.: `Send`, `UserLoggedOut`) → Other. `api.operation` = `Operation`; `api.service.name` = workload; `resources[0]` = `ObjectId` (o arquivo, a caixa, o objeto).
- `status_id` pelo `ResultStatus`: `Succeeded`/`Success`/`True` → Success; `Failed`/`False` → Failure; `PartiallySucceeded` → Other.
- `actor.user.name`/`email_addr` = `UserId`; `actor.user.uid` = `UserKey`; `actor.user.type` pelo `UserType` (Regular → User, Admin/DcAdmin → Admin, System → System, Application/ServicePrincipal → Other, Guest → User); `actor.app_uid` = app usado.
- `src_endpoint.ip` = `ClientIP`. A API manda o IP às vezes com porta (`203.0.113.5:51234`, `[2001:db8::1]:443`); o coletor acrescenta `ClientIPNormalized` com o IP limpo, e é esse campo que o mapping usa.
- `severity_id` = Low para logon falho e para reset de senha, papel, exclusão e desabilitação de conta; Informational para o resto.

**DLP (`dlp`)**

- `DlpRuleMatch` → **Detection Finding / Create**; `DlpRuleUndo` (a regra deixou de valer: política mudou, override ou falso positivo reportado) → **Close**; `DlpInfo` → Other.
- `finding_info.title` = nome da política; `finding_info.desc` e `analytic.name` = regra que casou; `severity_id` = a maior severidade entre as regras (`Low` → 2, `Medium` → 3, `High` → 4).
- `resources[0]` = o arquivo (`FilePathUrl`, SharePoint/OneDrive) ou a mensagem (`MessageID`, Exchange). `metadata.correlation_uid` = `IncidentId`.
- Os valores detectados nunca chegam aqui (ver [Segurança da integração](#segurança-da-integração)).

## Limites da API

Fontes: [referência da Management Activity API](https://learn.microsoft.com/office/office-365-management-api/office-365-management-activity-api-reference), [busca de auditoria](https://learn.microsoft.com/purview/audit-search), [soluções de auditoria](https://learn.microsoft.com/purview/audit-solutions-overview).

| Limite | Valor | O que o CentralOps faz |
|---|---|---|
| Janela da listagem | até 24 h por chamada | Divide o atraso em janelas de 24 h e lê várias no mesmo ciclo |
| Disponibilidade do conteúdo na API | **7 dias** | Cursor mais antigo que isso é trazido para o limite, com aviso no log do quanto se perdeu |
| Primeiro conteúdo de assinatura nova | até 12 h | Nada; é só esperar |
| Latência entre o evento e o blob | típica de 60 a 90 min nos serviços principais, sem garantia (SLA) | Nada a fazer do lado de cá; a Saúde do Pipeline mede a posição da leitura, não essa latência |
| Ordem | blobs e eventos não vêm em ordem | Dedupe por `Id` |
| Throttling | ~2.000 requisições/min por tenant (base), proporcional ao número de licenças; Audit Premium tem cerca do dobro | Toda chamada leva `PublisherIdentifier` = tenant (cota dedicada); `429`/`AF429` respeitam o `Retry-After` |
| Regra de reinício | `subscriptions/start` só de 15 em 15 min | O CentralOps guarda que a assinatura existe e não chama `start` a cada ciclo |

## Licença e custo

- **A API não tem cobrança própria.** A documentação da Microsoft não cita preço; o custo é a licença que dá direito à auditoria.
- **Audit (Standard)**: incluído em Microsoft 365/Office 365 E3, E5, Business Basic/Standard/Premium, F1/F3 e equivalentes. Retenção de **180 dias** no Purview e acesso pela Management Activity API.
- **Audit (Premium)**: E5/A5/G5 ou complemento de conformidade. Retenção de **1 ano** para Entra, Exchange, SharePoint e OneDrive (até 10 anos com complemento), eventos de alto valor como `MailItemsAccessed` e `Send`, e cerca do **dobro de banda** na API.
- **Retenção no Purview ≠ alcance da API.** O registro fica 180 dias (ou 1 ano) no Purview, mas a API só entrega conteúdo dos **últimos 7 dias**. Um CentralOps parado por mais de uma semana perde o intervalo na coleta contínua.

:::note[Para o que passou de 7 dias: a API de busca do Graph]

A Microsoft publicou em v1.0 a busca de auditoria pelo Graph (`POST /security/auditLog/queries`, permissão `AuditLogsQuery.Read.All`). Ela alcança toda a retenção do Purview, mas é assíncrona (cria a busca, espera, lê o resultado), tem limite diário de buscas por tenant e só existe na nuvem global. Serve para recuperar um intervalo, não para coleta contínua; ainda não é usada por esta integração.

:::

## Operação e saúde

- **Primeira coleta.** Começa pela última hora. Para trazer até 7 dias, use o **backfill** da integração; ele lê a janela pedida e não mexe na posição da coleta contínua.
- **Sobreposição.** Cada janela recomeça 10 minutos antes do fim da anterior. Os blobs já lidos nessa faixa não são baixados de novo, e o que escapar disso é descartado pelo dedupe (`Id`). O indicador de atraso em **Saúde do Pipeline** fica, por construção, em torno de 10 minutos.
- **Teto por ciclo.** Até 300 requisições por ciclo (páginas da listagem + blobs). No teto o ciclo guarda a janela, a página e os blobs dela já lidos, e o próximo continua dali; **Saúde do Pipeline** marca o stream como em *backlog*.
- **Blob expirado.** Um blob que venceu antes de ser lido (`AF20051`) é pulado com aviso no log; os demais seguem.

## Solução de problemas

**Stream sem evento nas primeiras horas.** Assinatura nova leva até 12 h para produzir conteúdo. Confira se a auditoria unificada está ligada.

**`dlp`: `stream desligado` no log do worker.** Falta `ActivityFeed.ReadDlp` ou o consentimento do administrador para ela.

**Erro de autenticação depois de meses funcionando.** O segredo expirou (`AADSTS7000222`). Crie um novo e atualize o campo *Client Secret*.

**Log: `cursor ... além dos 7 dias da API — ... perdido`.** O CentralOps ficou parado por mais de 7 dias; o conteúdo mais antigo não existe mais na API. Para recuperá-lo, use a busca de auditoria do Purview (portal ou Graph).

**Muitos `AF429`.** O tenant está no limite de requisições. Outra ferramenta usando a mesma API sem `PublisherIdentifier` divide a cota base; o CentralOps já usa a cota dedicada.

## Limitações conhecidas

- **Só nuvem comercial.** GCC, GCC High e DoD usam outros hosts (`manage-gcc.office.com`, `manage.office365.us`, `manage.protection.apps.mil`) e ainda não são aceitos.
- **Só Client Secret.** A documentação da Microsoft mostra o fluxo de token v1 com certificado; o CentralOps usa o client credentials v2 com segredo e escopo `https://manage.office.com/.default`, o caminho usado pelos conectores de SIEM do mercado. Certificado ainda não é suportado.
- A latência da Microsoft (o evento só vira blob dezenas de minutos depois) não aparece no indicador de atraso da Saúde do Pipeline.
- Intervalos com mais de 7 dias de idade não são recuperáveis por esta integração (ver [Licença e custo](#licença-e-custo)).
- Workloads do `Audit.General` sem regra específica saem como API Activity com verbo `Other`; o detalhe fica no `raw`.
