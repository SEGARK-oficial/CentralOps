---
sidebar_position: 5
title: Microsoft Azure
description: Colete o Activity Log, os sign-ins do Entra ID (inclusive não interativos) e os resource logs de Key Vault, AKS, Azure SQL e Storage, lendo de um Log Analytics workspace ou de Event Hubs
---

# Microsoft Azure

Conecte o Azure ao CentralOps para coletar, com uma integração e uma credencial só, três fluxos de segurança:

- o **Activity Log** de todas as assinaturas que você liberar (o plano de controle: quem mudou o quê, onde);
- os **logs do Microsoft Entra ID**, incluindo os sign-ins **não interativos**, de **service principal** e de **managed identity**;
- os **resource logs** dos recursos que guardam dado sensível: Key Vault, AKS, Azure SQL, Storage e outros.

O Azure entrega esses logs por *diagnostic settings*, e o CentralOps lê de um de dois destinos, à sua escolha:

- um **Log Analytics workspace**: se os seus diagnostic settings já vão para um workspace, os três fluxos saem dele, sem nenhuma infraestrutura nova;
- um **Event Hub**: a fila que SIEMs costumam usar para tirar log do Azure. Neste modo o Activity Log é lido direto da API do Azure Monitor.

## Quando usar

- **Vigiar privilégio no Azure.** Quem recebeu `Owner` ou `User Access Administrator`, quem criou ou apagou recurso, quem rodou `listKeys` numa storage account, quem mexeu em NSG, política ou bloqueio de recurso.
- **Fechar o ponto cego do Entra ID.** O Graph v1.0 (integração [Microsoft Entra ID](./microsoft-entra-id.md)) só entrega o sign-in **interativo**. Token roubado, *password spray* contra protocolo legado e abuso de service principal aparecem nos sign-ins **não interativos** e de **service principal**, que só saem por diagnostic setting.
- **Saber quem executou comando dentro de VM pelo Azure.** Run Command (portal, `az vm run-command`), Custom Script Extension e `az aks command invoke` rodam código como `SYSTEM`/`root` sem RDP nem SSH. Ver [Run Command: o que vem e o que não vem](#run-command-o-que-vem-e-o-que-não-vem).
- **Saber quem leu o segredo.** Leitura e listagem de segredo, chave e certificado no Key Vault, com quem leu e de onde.
- **Ver os eventos administrativos do Kubernetes (AKS).** `kubectl exec` em produção, `ClusterRoleBinding` para `cluster-admin`, leitura de secrets.
- **Monitorar o Azure SQL.** Login falho em sequência, `GRANT`, `CREATE USER`, DDL.

## Como funciona

O campo **Fonte** da integração decide de onde os streams leem:

```
Modo workspace (Fonte = Workspace ID do Log Analytics)

Assinaturas ─diagnostic setting─┐
Entra ID    ─diagnostic setting─┼─► Log Analytics workspace ──API de consulta (KQL)──► activity, entra, resource
Recursos    ─diagnostic setting─┘

Modo Event Hubs (Fonte = namespace do Event Hubs)

Assinaturas ──────── API do Azure Monitor (polling, papel de leitura) ─────────────► stream activity
Entra ID   ─diagnostic setting─► Event Hub centralops-entra    ──Kafka (OAuth)──► stream entra
Recursos   ─diagnostic setting─► Event Hub centralops-resource ──Kafka (OAuth)──► stream resource
```

| Stream | Fonte no modo workspace | Fonte no modo Event Hubs | O que chega | Classe OCSF |
|---|---|---|---|---|
| `activity` | Tabela `AzureActivity` | API REST do Azure Monitor (`eventtypes/management/values`), em **toda assinatura que o app enxerga** | Activity Log: categorias Administrative, Security (alertas do Defender for Cloud), Policy, ServiceHealth, Recommendation, Autoscale, ResourceHealth | API Activity (6003) |
| `entra` | `SigninLogs`, `AADNonInteractiveUserSignInLogs`, `AADServicePrincipalSignInLogs`, `AADManagedIdentitySignInLogs`, `AuditLogs`, `AADProvisioningLogs`, `ADFSSignInLogs` e as tabelas de risco do Identity Protection | Event Hub `centralops-entra` | Sign-ins (interativos, não interativos, de service principal e de managed identity), `AuditLogs` e as demais categorias do Entra | Sign-in → Authentication (3002); mudança de conta → Account Change (3001); resto do AuditLogs → API Activity (6003) |
| `resource` | `AzureDiagnostics`, `AZKVAuditLogs`, `AKSAudit`, `AKSAuditAdmin`, `StorageBlobLogs`, `StorageFileLogs`, `StorageQueueLogs`, `StorageTableLogs`, `SQLSecurityAuditEvents` | Event Hub `centralops-resource` | Resource logs: `AuditEvent` (Key Vault), `kube-audit`/`kube-audit-admin` (AKS), `SQLSecurityAuditEvents` (Azure SQL), `StorageRead`/`StorageWrite`/`StorageDelete` e qualquer outra categoria | Ver [O que é coletado](#o-que-é-coletado) |

- **O mapeamento é o mesmo nas duas fontes.** Cada linha do workspace é convertida para a forma do registro que o diagnostic setting publicaria no Event Hub (ou que a API do Activity Log devolveria). O mesmo evento dá o mesmo OCSF, venha de onde vier.
- **Stream sem fonte aparece com erro, não como saudável.** Sem nada no campo **Fonte**, `entra` e `resource` falham com a instrução do que configurar. No modo workspace, um stream cujas tabelas não receberam nada nos últimos 7 dias também falha, dizendo qual diagnostic setting falta. Assim que os logs começarem a chegar, ele volta ao normal sozinho.
- **No modo Event Hubs, hub que não existe = stream desligado.** Crie só os hubs dos fluxos que quiser.
- **No modo Event Hubs, o escopo do `activity` é o RBAC**: o CentralOps lê as assinaturas em que o app tem o papel do [Passo 1](#passo-1-modo-event-hubs-criar-o-app-e-dar-o-papel-de-leitura), **no escopo da assinatura**. Atribua o papel num *management group* para cobrir também as assinaturas criadas depois.

### Qual fonte escolher

| | Log Analytics workspace | Event Hubs |
|---|---|---|
| Use quando | Os diagnostic settings já vão para um workspace (o caso comum de quem usa Microsoft Sentinel ou Azure Monitor) | Você não quer pagar ingestão no Log Analytics, ou já exporta para SIEM por Event Hub |
| Infraestrutura nova | Nenhuma | Namespace Standard e dois hubs |
| Papel do app | **Log Analytics Reader** no workspace | **Monitoring Reader** na assinatura e **Azure Event Hubs Data Receiver** no namespace |
| Atraso típico | O de ingestão do Log Analytics: de segundos a alguns minutos, às vezes mais | Segundos depois do Azure publicar |
| Histórico | Primeira coleta: últimas 24 h. Backfill: o que o workspace retém | Primeira coleta: o que o hub retém (até 7 dias) |
| Custo no Azure | Ingestão e retenção do workspace (que você já paga). Consultar tabelas do plano Analytics não é cobrado | Namespace do Event Hubs |

:::info[Destinos de diagnostic setting]

Diagnostic settings exportam para três destinos: Log Analytics, Storage Account e Event Hub. O CentralOps lê dois deles. Do workspace, lê pela API de consulta, autenticando com o token do app. Do Event Hub, lê pelo **endpoint Kafka** (porta 9093), também com token do Entra ID e sem connection string. **Storage Account não é lida.** Se a sua storage account de log recebe diagnostic settings, aponte esses diagnostic settings também para o workspace ou para o Event Hub.

:::

## O que coletar: análise de segurança

Ligar tudo é caro e enterra o sinal. A tabela abaixo ordena as fontes pelo que elas revelam de ataque real, com o volume esperado. Comece pelas de prioridade **1**.

| Prioridade | Fonte | Stream | O que pega | Volume |
|---|---|---|---|---|
| 1 | **Activity Log** | `activity` | Escalada de privilégio (`roleAssignments/write`), `listKeys`/`regenerateKey` de storage e Cosmos DB, execução de comando em VM (Run Command, Custom Script Extension), criação de VM para mineração, desligamento de diagnostic setting (o atacante apagando o rastro), exclusão de recurso, alertas do Defender for Cloud | Baixo |
| 1 | **Entra `SignInLogs` + `NonInteractiveUserSignInLogs`** | `entra` | *Password spray*, MFA *fatigue*, *token replay* (sessão roubada usada de outro país), protocolo legado | Alto: o não interativo costuma ser 5 a 10 vezes o interativo |
| 1 | **Entra `AuditLogs`** | `entra` | Admin novo (`Add member to role`), credencial nova em app ou service principal (persistência clássica), consentimento de app, mudança de Conditional Access, reset de senha | Baixo |
| 1 | **Entra `ServicePrincipalSignInLogs` + `ManagedIdentitySignInLogs`** | `entra` | Segredo de app vazado sendo usado de fora; managed identity usada fora do padrão | Médio |
| 1 | **Key Vault `AuditEvent`** | `resource` | Leitura em massa de segredo (`SecretGet`/`SecretList`), acesso negado repetido, `SecretPurge`, `KeyDecrypt` por identidade nova | Médio |
| 2 | **AKS `kube-audit-admin`** | `resource` | `kubectl exec`/`attach`, RBAC do cluster, `create`/`delete` de workload. Não inclui `get`/`list` | Médio |
| 2 | **Azure SQL `SQLSecurityAuditEvents`** | `resource` | Força bruta no banco, `GRANT`, `CREATE USER`, DDL | Depende do grupo de ações auditado |
| 2 | **Entra `RiskyUsers`, `UserRiskEvents`, `RiskyServicePrincipals`** | `entra` | Detecções do Identity Protection (exige Entra ID P2) | Baixo |
| 3 | **AKS `kube-audit`** (completo) | `resource` | Tudo do `kube-audit-admin` mais leitura de secret (`get secrets`) | **Muito alto**: ligue só em cluster crítico |
| 3 | **Storage `StorageDelete`/`StorageWrite`** | `resource` | Exfiltração e destruição de dado em contas sensíveis (backup, dados de cliente) | Alto: ligue por conta, não para todas |
| 3 | **Azure Firewall** (`AZFWNetworkRule`, `AZFWApplicationRule`) | `resource` | Saída para destino incomum, C2 | Alto |

Não mande:

- **O Activity Log da assinatura para um hub** (modo Event Hubs). O stream `activity` já o lê pela API. Mandar também para um hub gera cada evento duas vezes, em formatos diferentes, e o dedupe não consegue juntar os dois. No modo workspace é o contrário: o Activity Log **precisa** ir para o workspace (Passo 4).
- **Métricas** (`AllMetrics`). São números de desempenho, não eventos de segurança.

## Segurança da integração

Esta integração entrega ao CentralOps o direito de **ler** log de segurança do seu Azure. Ela foi desenhada para que esse direito seja o menor possível e para que nada que o CentralOps guarda permita mudar o seu ambiente.

**Só leitura, e só o que é log.**

- No workspace, o papel é **Log Analytics Reader**: consultar as tabelas. A única chamada é o `POST` de consulta da API do Log Analytics, que não muda nada. **Não** dê *Log Analytics Contributor*: é papel de escrita e inclui instalar extensão em VM, que roda código nela.
- O papel do Activity Log, no modo Event Hubs, é de leitura (Monitoring Reader, ou o papel customizado mínimo do Passo 1, que só lê eventos). Nenhum caminho de código do coletor faz `PUT`, `POST` ou `DELETE` no ARM.
- No Event Hub, o papel é **Azure Event Hubs Data Receiver**: ler mensagens. Não dá para publicar, apagar ou mudar o hub.
- **Storage account key não é usada.** O CentralOps não lê Storage Account e não precisa da chave dela.

**Nenhuma chave compartilhada.** O CentralOps **não** recebe connection string do Event Hubs (`SharedAccessKey`). O mesmo app registration autentica no ARM e, por OAuth, no endpoint Kafka do Event Hubs. Revogar o acesso é tirar um papel no RBAC ou apagar o segredo do app, sem rodar chave de namespace usada por outros sistemas.

**O que o CentralOps guarda:** Tenant ID, Client ID e a fonte (Workspace ID ou nome do namespace) em texto; o **Client Secret cifrado** no cofre de credenciais da integração (o mesmo usado pelos demais vendors). O segredo nunca volta para a tela, nem no teste de conexão: quando o Entra recusa a credencial, o CentralOps mostra só o código do erro (ex.: `invalid_client (AADSTS7000215)`).

**Superfície de rede fechada.** O token do Entra vale como a identidade do app. Por isso o coletor:

- só aceita namespace terminado em `.servicebus.windows.net` (um namespace digitado errado ou malicioso não recebe o token);
- só aceita como Workspace ID um GUID, que vira o caminho de `https://api.loganalytics.io/v1/workspaces/<id>`; o que vem do cursor só entra na consulta KQL no formato exato de data e de hash que a própria API devolve;
- só segue o `nextLink` de paginação se ele apontar para `https://management.azure.com`;
- não segue redirecionamento em nenhuma chamada;
- valida Tenant ID e Client ID antes de montá-los na URL do login.

**Saída de rede necessária** a partir dos workers do CentralOps:

| Destino | Porta | Para quê |
|---|---|---|
| `login.microsoftonline.com` | 443 | Token do Entra |
| `management.azure.com` | 443 | Activity Log, lista de assinaturas e conferência do papel (modo Event Hubs) |
| `api.loganalytics.io` | 443 | Consulta ao workspace (modo workspace) |
| `<namespace>.servicebus.windows.net` | **9093** | Event Hubs via Kafka (modo Event Hubs) |

Se o namespace do Event Hubs tem firewall (regras de IP ou *private endpoint*), libere o IP de saída do CentralOps.

**Rotação do segredo.** Segredo de app registration expira (o padrão do `az ad sp create-for-rbac` é 1 ano). Antes de expirar, crie um segredo novo, atualize o campo *Client Secret* da integração e só então apague o antigo. Segredo expirado aparece como erro de autenticação na integração e em **Saúde do Pipeline**.

## Quem pode fazer o quê

- **Administrador da plataforma:** cria e edita a integração (menu **Coleta -> Integrações**).
- **No Azure:**
  - criar o app registration exige *Application Administrator* (ou *Cloud Application Administrator*) no Entra ID;
  - atribuir papel exige *Owner* ou *User Access Administrator* no escopo (assinatura ou management group);
  - configurar diagnostic setting do Entra ID exige *Security Administrator*;
  - configurar diagnostic setting de recurso exige *Monitoring Contributor* no recurso.

## Pré-requisitos

- Acesso de **administrador no CentralOps**.
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) logado (`az login`) com as permissões acima, ou o Cloud Shell do portal.
- Uma fonte para os logs:
  - **modo workspace:** um **Log Analytics workspace** que recebe (ou vai receber) os diagnostic settings;
  - **modo Event Hubs:** um **namespace do Event Hubs no tier Standard ou acima**. O tier Basic não tem endpoint Kafka.
- Para exportar os sign-ins do Entra ID, para qualquer destino: licença **Microsoft Entra ID P1 ou P2** no tenant. O portal recusa o export de sign-in sem ela. Sem a licença, o `AuditLogs` ainda pode ser exportado.

Nos comandos abaixo, troque `MG_ID` pelo ID do management group (ou use uma assinatura), `RG` pelo resource group do Event Hubs, `NS` pelo nome do namespace e `WS_RG`/`WS_NOME` pelo resource group e nome do workspace.

## Passo 1 (modo workspace): Criar o app e dar leitura no workspace

```bash
az ad sp create-for-rbac --name centralops-coletor --skip-assignment

WS_RESOURCE_ID=$(az monitor log-analytics workspace show -g "$WS_RG" -n "$WS_NOME" --query id -o tsv)
az role assignment create --assignee APP_ID --role "Log Analytics Reader" --scope "$WS_RESOURCE_ID"

# O Workspace ID que vai no campo Fonte (um GUID, não o nome do workspace):
az monitor log-analytics workspace show -g "$WS_RG" -n "$WS_NOME" --query customerId -o tsv
```

A saída do primeiro comando traz o Tenant ID (`tenant`), o Client ID (`appId`) e o Client Secret (`password`). Guarde o `password`: ele não aparece de novo. O Workspace ID também está no portal, na página **Overview** do workspace.

No modo workspace o app **não** precisa de papel na assinatura: o Activity Log é lido da tabela `AzureActivity`. Siga para o [Passo 3](#passo-3-exportar-os-logs-do-entra-id) e o [Passo 4](#passo-4-exportar-o-activity-log-e-os-resource-logs) só se esses logs ainda não vão para o workspace.

## Passo 1 (modo Event Hubs): Criar o app e dar o papel de leitura

```bash
MG_ID=meu-management-group

az ad sp create-for-rbac --name centralops-coletor \
  --role "Monitoring Reader" \
  --scopes "/providers/Microsoft.Management/managementGroups/$MG_ID"
```

A saída traz os três valores da integração:

| Saída | Campo no CentralOps |
|---|---|
| `tenant` | Tenant ID |
| `appId` | Client ID |
| `password` | Client Secret |

Guarde o `password` agora: ele não aparece de novo.

:::tip[Papel mínimo: só ler eventos]

O *Monitoring Reader* lê qualquer configuração (`*/read`), não só log. Para que o app enxergue **apenas** o Activity Log, crie um papel customizado e use-o no lugar:

```bash
cat > centralops-activity-reader.json <<EOF
{
  "Name": "CentralOps Activity Log Reader",
  "Description": "Le o Activity Log e lista as assinaturas. Nada mais.",
  "Actions": [
    "Microsoft.Insights/eventtypes/values/read",
    "Microsoft.Resources/subscriptions/read"
  ],
  "AssignableScopes": ["/providers/Microsoft.Management/managementGroups/$MG_ID"]
}
EOF
az role definition create --role-definition centralops-activity-reader.json
az role assignment create --assignee APP_ID \
  --role "CentralOps Activity Log Reader" \
  --scope "/providers/Microsoft.Management/managementGroups/$MG_ID"
```

:::

Sem management group, troque o escopo por `/subscriptions/ID_DA_ASSINATURA` e repita para cada assinatura.

## Passo 2 (modo Event Hubs): Namespace e hubs do Event Hubs

Pule este passo no modo workspace, ou se por enquanto só quer o Activity Log.

```bash
RG=rg-seguranca
NS=ns-seguranca-centralops   # 6 a 50 caracteres; letras, números e hífen
LOCAL=brazilsouth

az eventhubs namespace create -g "$RG" -n "$NS" -l "$LOCAL" --sku Standard

for HUB in centralops-entra centralops-resource; do
  az eventhubs eventhub create -g "$RG" --namespace-name "$NS" -n "$HUB" \
    --partition-count 4 --cleanup-policy Delete --retention-time-in-hours 168
done

# O CentralOps só LÊ: papel Data Receiver no namespace.
NS_ID=$(az eventhubs namespace show -g "$RG" -n "$NS" --query id -o tsv)
az role assignment create --assignee APP_ID \
  --role "Azure Event Hubs Data Receiver" --scope "$NS_ID"
```

- **Retenção:** 168 horas (7 dias) é o máximo do Standard. É o tempo que o CentralOps pode ficar parado sem perder log.
- **Partições:** 4 atende a maioria dos tenants. No Standard o número só é definido na criação; para tenant com muito volume de sign-in não interativo, use 8 a 16.
- **Use um namespace dedicado** ao CentralOps. Assim o papel do app não alcança hubs de aplicação.

:::note[A chave do namespace fica no Azure]

O diagnostic setting publica no hub usando uma regra de acesso compartilhado do namespace (`RootManageSharedAccessKey` ou uma regra com Manage, Send e Listen). Essa chave é usada **pelo Azure Monitor, dentro do Azure**, e nunca é informada ao CentralOps.

:::

## Passo 3: Exportar os logs do Entra ID

1. No [Microsoft Entra admin center](https://entra.microsoft.com), vá em **Identidade → Monitoramento e integridade → Configurações de diagnóstico** e clique em **Adicionar configuração de diagnóstico**.
2. Marque as categorias. Recomendado para começar: `AuditLogs`, `SignInLogs`, `NonInteractiveUserSignInLogs`, `ServicePrincipalSignInLogs`, `ManagedIdentitySignInLogs`. Com Entra ID P2, some `RiskyUsers` e `UserRiskEvents`.
3. Em **Detalhes do destino**:
   - **modo workspace:** marque **Enviar para o workspace do Log Analytics** e escolha o workspace da integração;
   - **modo Event Hubs:** marque **Transmitir para um hub de eventos**, escolha a assinatura, o namespace `NS`, o hub **`centralops-entra`** e a regra de acesso do namespace.
4. Salve. Os primeiros registros chegam em alguns minutos.

Se você já usa a integração [Microsoft Entra ID](./microsoft-entra-id.md) (Graph), ela passa a ser redundante para sign-in e auditoria: o stream `entra` entrega o mesmo conteúdo e mais. Desative uma das duas para não contar o mesmo login duas vezes.

## Passo 4: Exportar o Activity Log e os resource logs

**Modo workspace.** O Activity Log da assinatura e os resource logs vão para o workspace. Para o Activity Log (repita por assinatura):

```bash
az monitor diagnostic-settings subscription create --name centralops \
  --subscription ID_DA_ASSINATURA --location brazilsouth \
  --workspace "$WS_RESOURCE_ID" \
  --logs '[{"category":"Administrative","enabled":true},{"category":"Security","enabled":true},{"category":"Policy","enabled":true},{"category":"Alert","enabled":true},{"category":"ServiceHealth","enabled":true},{"category":"ResourceHealth","enabled":true},{"category":"Recommendation","enabled":true},{"category":"Autoscale","enabled":true}]'
```

Para os resource logs, use os mesmos comandos do modo Event Hubs abaixo trocando `--event-hub centralops-resource --event-hub-rule "$RULE_ID"` por `--workspace "$WS_RESOURCE_ID" --export-to-resource-specific true`. As tabelas por recurso (`AZKVAuditLogs`, `AKSAudit`, `StorageBlobLogs`) são as recomendadas; a tabela legada `AzureDiagnostics` também é lida. No Azure SQL, troque os parâmetros de Event Hub por `--log-analytics-target-state Enabled --log-analytics-workspace-resource-id "$WS_RESOURCE_ID"`.

**Modo Event Hubs.** Todos os resource logs vão para o hub **`centralops-resource`**. O Activity Log não vai para hub nenhum: o stream `activity` o lê pela API. A regra de acesso é a do namespace:

```bash
RULE_ID=$(az eventhubs namespace authorization-rule show -g "$RG" --namespace-name "$NS" \
  -n RootManageSharedAccessKey --query id -o tsv)
```

**Key Vault** (todo cofre com segredo de produção):

```bash
az monitor diagnostic-settings create --name centralops \
  --resource "$(az keyvault show -n MEU_COFRE --query id -o tsv)" \
  --event-hub centralops-resource --event-hub-rule "$RULE_ID" \
  --logs '[{"category":"AuditEvent","enabled":true}]'
```

**AKS** (`kube-audit-admin` deixa de fora `get`/`list`; troque por `kube-audit` só em cluster crítico):

```bash
az monitor diagnostic-settings create --name centralops \
  --resource "$(az aks show -g RG_DO_AKS -n MEU_AKS --query id -o tsv)" \
  --event-hub centralops-resource --event-hub-rule "$RULE_ID" \
  --logs '[{"category":"kube-audit-admin","enabled":true},{"category":"guard","enabled":true}]'
```

**Azure SQL** (auditoria no nível do servidor, que vale para todos os bancos dele):

```bash
az sql server audit-policy update -g RG_DO_SQL -n MEU_SERVIDOR \
  --state Enabled --event-hub-target-state Enabled \
  --event-hub-authorization-rule-id "$RULE_ID" --event-hub centralops-resource
```

O grupo de ações padrão da auditoria do Azure SQL inclui login com sucesso, login com falha e todos os *batches*. Num banco movimentado, *batch* é volume alto: considere restringir os grupos de ação (`--actions`) a `FAILED_DATABASE_AUTHENTICATION_GROUP`, `SUCCESSFUL_DATABASE_AUTHENTICATION_GROUP`, `DATABASE_PERMISSION_CHANGE_GROUP`, `DATABASE_PRINCIPAL_CHANGE_GROUP` e `SCHEMA_OBJECT_CHANGE_GROUP`.

**Storage** (só contas sensíveis; o recurso é o serviço de blob, não a conta):

```bash
az monitor diagnostic-settings create --name centralops \
  --resource "$(az storage account show -n MINHA_CONTA --query id -o tsv)/blobServices/default" \
  --event-hub centralops-resource --event-hub-rule "$RULE_ID" \
  --logs '[{"category":"StorageWrite","enabled":true},{"category":"StorageDelete","enabled":true}]'
```

:::tip[Em escala: Azure Policy]

Para não depender de alguém lembrar de ligar o log em cada cofre ou cluster novo, use as políticas internas do Azure Policy do tipo *Deploy Diagnostic Settings for … to Event Hub* ou *… to Log Analytics workspace* (há versões para Key Vault, AKS, SQL e outros), atribuídas no management group com efeito `DeployIfNotExists`.

:::

## Passo 5: Criar a integração no CentralOps

1. No menu lateral, vá em **Coleta -> Integrações** e adicione uma integração **Microsoft Azure**.
2. Preencha:

   | Campo | O que informar |
   |---|---|
   | Tenant ID | O `tenant` do Passo 1 (GUID do diretório). |
   | Client ID | O `appId` do Passo 1. |
   | Client Secret | O `password` do Passo 1. |
   | Fonte | **Modo workspace:** o **Workspace ID** (GUID) do Passo 1. Não é o nome do workspace, nem o de uma storage account. **Modo Event Hubs:** o `NS` do Passo 2 (aceita `ns`, `ns.servicebus.windows.net` ou `sb://ns.servicebus.windows.net/`). |

3. Clique em **testar a conexão**. O teste não lê nenhum evento e mostra o estado de cada stream:
   - `activity: ok (630 linha(s) em 7 dias: AzureActivity)` (modo workspace): quantas linhas de cada stream o workspace recebeu na última semana.
   - `entra: sem dados no workspace em 7 dias. …` (modo workspace): o diagnostic setting daquele fluxo ainda não aponta para o workspace. A mensagem diz qual criar.
   - `sem leitura no workspace` / `workspace … não encontrado`: falta o papel *Log Analytics Reader*, ou o campo Fonte não tem o Workspace ID certo.
   - `activity: ok (N assinatura(s))` (modo Event Hubs): o app enxerga N assinaturas e lê o Activity Log no escopo delas.
   - `activity: o app enxerga N assinatura(s), mas só tem papel em recursos dentro delas` (modo Event Hubs): o papel foi dado num recurso (por exemplo, no workspace), não na assinatura. A listagem mostra a assinatura, mas a API do Activity Log só devolveria os eventos desses recursos. Atribua o papel do Passo 1 na assinatura, ou use o modo workspace.
   - `activity: nenhuma assinatura visível`: falta o papel do Passo 1 (ou ele ainda está propagando, o que leva alguns minutos).
   - `entra/resource: sem fonte`: o campo Fonte está vazio.
   - `entra: ok` / `resource: ok` (modo Event Hubs): o hub existe e o app consegue conectar.
   - `não existe (stream desligado)`: o hub não foi criado. Normal se você não quer aquele fluxo.
   - `Event Hubs (...): inacessível`: tier Basic, porta 9093 bloqueada na saída, firewall do namespace ou papel *Data Receiver* ausente.
   - `O Entra recusou a credencial: …`: Tenant ID, Client ID ou segredo errados, ou segredo expirado. O código `AADSTS` diz qual (ex.: `AADSTS7000215` = segredo inválido; `AADSTS700016` = app não encontrado no tenant).

   O teste reprova se nenhum stream estiver utilizável.

4. Salve. O `activity` roda a cada 5 minutos; `entra` e `resource`, a cada 2 minutos.

## O que é coletado

Cada evento chega normalizado em OCSF 1.8, com o registro original do Azure preservado no `raw`. `cloud.provider` = `Azure`.

No modo workspace, o `raw` é a linha convertida para a forma do registro do Event Hub (ou da API do Activity Log), com o nome da tabela de origem em `logAnalyticsTable`. A conversão corrige o que o workspace altera: o `OperationNameValue` vem em MAIÚSCULAS no `AzureActivity` e volta à caixa original (regras que comparam `api.operation` continuam valendo); `Start`/`Success`/`Accept` voltam a `Started`/`Succeeded`/`Accepted`; o `TenantId` das tabelas, que é o ID do **workspace**, é trocado pelo tenant do Entra.

**Activity Log (`activity`)**

- `activity_id` pelo sufixo do `operationName`: `/write` → Update, `/delete` → Delete, `/read` → Read, `/action` (ex.: `listKeys`, `regenerateKey`) → Other. *Write* no ARM cria **ou** substitui o recurso; o OCSF não tem "upsert", então ele sai como Update.
- `status_id`: `Succeeded` → Success; `Failed`/`Canceled` → Failure; `Started`/`Accepted` → Other. Uma operação gera dois eventos (início e fim) com `eventDataId` diferentes.
- `severity_id` pelo `level`: Informational → 1, Warning → 2, Error → 3, Critical → 4.
- `actor.user.name` = `caller` (UPN ou appId do service principal); `actor.user.email_addr` = UPN; `actor.app_uid` = appId do cliente usado (portal, CLI, Terraform...); `src_endpoint.ip` = IP de quem chamou.
- `api.operation` = `operationName` (ex.: `Microsoft.Authorization/roleAssignments/write`); `resources[0].uid` = ID do recurso; `cloud.account.uid` = assinatura; `cloud.org.uid` = tenant.
- O papel atribuído e o principal que o recebeu ficam em `properties.requestbody` no `raw`.

**Entra ID (`entra`)**

- Sign-ins (interativo, não interativo, service principal, managed identity, AD FS) → **Authentication / Logon**. `status_id` = Failure quando `resultType` ≠ `0`, com `status_code` = código AADSTS (ex.: `50126` senha errada, `50074` MFA exigido). `user` = quem logou (o service principal, no caso de SP). `is_mfa` = o login exigiu MFA. `severity_id` = risco do Identity Protection no momento do login (`low` → 2, `medium` → 3, `high` → 4). `src_endpoint.location` = cidade e país; `service.name` = app acessado; `device.os.name` = sistema do dispositivo.
- `AuditLogs` de conta → **Account Change**, com `user` = **conta alterada** e `actor` = **quem alterou**: reset de senha (Password Reset), troca de senha, habilitar/desabilitar, criar/apagar usuário, entrar em papel (Attach Policy), sair de papel (Detach Policy).
- Demais `AuditLogs` (aplicativo, service principal, grupo, Conditional Access) → **API Activity**, pelo `operationType` (Add → Create, Update → Update, Delete → Delete, demais → Other). `api.operation` = `activityDisplayName` (ex.: `Add service principal credentials`).

**Resource logs (`resource`)**

- **Key Vault** (`AuditEvent`) → API Activity pelo verbo da operação: `SecretGet`/`SecretList`/`VaultGet` → Read; `SecretSet`/`KeyUpdate` → Update; `KeyCreate`/`KeyImport` → Create; `SecretDelete`/`SecretPurge` → Delete; `KeyDecrypt`/`KeySign`/`KeyWrapKey` → Other. `status_id` = Failure quando o HTTP é 4xx/5xx (ex.: 403 de acesso negado). `actor` = UPN ou appId do token.
- **AKS** (`kube-audit*`) → API Activity pelo `verb` do Kubernetes. O campo `log` vem do Azure como texto JSON; o CentralOps o decodifica para o mapping enxergar `verb`, `user`, `objectRef`. `api.operation` = verbo + recurso + sub-recurso (ex.: `create pods exec`); `http_request.url.url_string` = `requestURI`; `actor` = usuário do Kubernetes.
- **Azure SQL** (`SQLSecurityAuditEvents`) → `DATABASE AUTHENTICATION SUCCEEDED/FAILED` vira **Authentication** (com `user` = principal do banco); as demais ações viram **Datastore Activity / Query**, com `database.name` e `query_info.query_string` = comando executado.
- **Storage** (`Storage*`) → API Activity pelo verbo da operação (`GetBlob` → Read, `PutBlob` → Update, `DeleteBlob` → Delete). O IP de quem chamou vem como `ip:porta` e fica só no `raw`.
- **Qualquer outra categoria** (firewall, NSG, App Service, Front Door...) → **Base Event** (classe 0), o *fallback* oficial do OCSF, com `src_endpoint`/`dst_endpoint` quando o registro traz (Azure Firewall). Classificar tráfego de rede como API Activity seria errado; o conteúdo inteiro fica no `raw` para as regras de detecção.

## Run Command: o que vem e o que não vem

Run Command é o jeito mais curto de um invasor com papel no Azure (ou com o segredo de um service principal) executar código dentro de uma VM: o script roda como `SYSTEM` no Windows e `root` no Linux, sem RDP, sem SSH e sem senha da máquina. Por isso ele precisa de duas metades de evidência, que vêm de lugares diferentes.

**O que esta integração entrega (stream `activity`).** Toda execução passa pelo ARM e fica no Activity Log:

| Operação (`api.operation`) | O que é | Classe / atividade |
|---|---|---|
| `Microsoft.Compute/virtualMachines/runCommand/action` | Run Command clássico: portal (**Executar comando**), `az vm run-command invoke`, `Invoke-AzVMRunCommand` | API Activity / Other (99) |
| `Microsoft.Compute/virtualMachineScaleSets/virtualMachines/runCommand/action` | O mesmo, numa instância de VM Scale Set | API Activity / Other (99) |
| `Microsoft.Compute/virtualMachines/runCommands/write` | Run Command **gerenciado** (`az vm run-command create`): o comando vira um recurso da VM | API Activity / Update (3) |
| `Microsoft.HybridCompute/machines/runCommands/write` | Run Command em servidor **Azure Arc** (fora do Azure) | API Activity / Update (3) |
| `Microsoft.Compute/virtualMachines/extensions/write` | Custom Script Extension (ou qualquer extensão) instalada ou alterada | API Activity / Update (3) |
| `Microsoft.ContainerService/managedClusters/runCommand/action` | `az aks command invoke`: comando no cluster AKS sem acesso de rede a ele | API Activity / Other (99) |

De cada uma: **quem** rodou (`actor.user`, `actor.app_uid` = portal, CLI, automação), **de onde** (`src_endpoint.ip`), **em qual máquina** (`resources[0].uid`), **quando** (`time`) e **se funcionou** (`status_id`; o Activity Log registra o início e o fim da operação). Com o Defender for Cloud (plano *Servers* ou *Resource Manager*) ligado, os alertas dele sobre uso suspeito de Run Command chegam no mesmo stream, na categoria `Security`.

**O que esta integração NÃO entrega.**

- **O texto do script no Run Command clássico.** A Microsoft não documenta que o Activity Log guarde o conteúdo do script de um `runCommand/action`, e não conte com isso.
- **O texto do script no Run Command gerenciado.** Às vezes o corpo da requisição (com `properties.source.script`) aparece em `properties.requestbody`, no `raw`. Não é garantido: o Azure trunca ou omite o corpo em parte das operações. Use se estiver lá, mas não dependa dele.
- **A saída do comando.** O resultado volta só para quem chamou (ou vai para um blob, no gerenciado). Ele nunca passa pelo Activity Log.

**Onde achar o script: dentro da máquina.** O agente da Azure baixa o script para o disco antes de executar. O conteúdo e o que ele fez estão no host, e chegam ao CentralOps por outras integrações:

| Fonte no host | O que dá | Como trazer |
|---|---|---|
| **Windows:** pastas do plugin em `C:\Packages\Plugins\Microsoft.CPlat.Core.RunCommandWindows\<versão>\` (subpastas `Downloads`, com os `script*.ps1`, e `Status`) e logs em `C:\WindowsAzure\Logs\Plugins\` | Cópia do script executado e estado da execução | Monitoramento de integridade de arquivos (FIM) do [agente Wazuh](./wazuh.md) nessas pastas |
| **Windows:** PowerShell *Script Block Logging* (evento **4104** em `Microsoft-Windows-PowerShell/Operational`) | **O texto do script**, como o PowerShell o executou, mesmo ofuscado em camadas | Ligue por GPO (*Turn on PowerShell Script Block Logging*) e colete o canal pelo agente Wazuh ou pelo [Windows Event Log](./windows-event-log.md) |
| **Linux:** `/var/lib/waagent/` (pasta `run-command/download/` do Run Command clássico; `Microsoft.CPlat.Core.RunCommandHandlerLinux-<versão>/` do gerenciado) e logs em `/var/log/azure/` | Cópia do script e log do *handler* | FIM e coleta de log do agente Wazuh |
| **Linux:** `auditd` com regra de `execve` | Cada comando que o script disparou, com argumentos | Coleta do `audit.log` pelo agente Wazuh |
| **Microsoft Defender for Endpoint** | Árvore de processo (o agente da Azure como pai do `powershell.exe`/`bash`) e o conteúdo via AMSI | Integração [Microsoft Defender](./microsoft-defender.md) |

O nome das pastas muda com a versão do agente da Azure e com o tipo de Run Command: confira numa VM de teste antes de fixar o caminho no FIM.

**Juntando as duas metades.** O Activity Log responde *quem disparou, de onde e em qual VM*; o host responde *o que rodou*. Correlacione pelo nome da VM (`resources[0].uid` termina com o nome da máquina) e pela janela de tempo: o script aparece no host segundos depois do `runCommand/action`.

## Detecções sugeridas

Pontos de partida para regras sobre estes eventos:

| Sinal | Onde | Filtro |
|---|---|---|
| Papel privilegiado concedido | `activity` | `api.operation` = `Microsoft.Authorization/roleAssignments/write` e `status_id` = 1; o papel está em `properties.requestbody` |
| Chave de storage lida | `activity` | `api.operation` termina em `/listKeys/action` (Storage, Cosmos DB, Event Hubs) |
| Run Command em VM | `activity` | `api.operation` termina em `/runCommand/action` ou em `/runCommands/write` (VM, VMSS, Arc). Alerte em todo caso cujo `actor` não esteja na lista de automações conhecidas; em VM de produção, alerte sempre |
| Run Command por identidade nova | `activity` | o mesmo filtro, com `actor.user.name` que nunca rodou Run Command naquela assinatura, ou `src_endpoint.ip` fora da rede corporativa |
| Run Command em várias VMs | `activity` | o mesmo `actor` em 3 ou mais `resources[0].uid` diferentes em poucos minutos (movimento lateral, implantação de ransomware) |
| Custom Script Extension instalada | `activity` | `api.operation` = `Microsoft.Compute/virtualMachines/extensions/write`; o tipo da extensão está em `properties.requestbody` quando o Azure o registra |
| Comando no AKS sem acesso de rede | `activity` | `api.operation` = `Microsoft.ContainerService/managedClusters/runCommand/action` |
| Run Command seguido de ação no host | `activity` + host | `runCommand/action` numa VM e, em até 10 min, conta local criada, serviço instalado ou evento 4104 com download (`Invoke-WebRequest`, `curl`) na mesma máquina |
| Log de diagnóstico desligado | `activity` | `api.operation` = `Microsoft.Insights/diagnosticSettings/delete` (compare sem diferenciar maiúsculas: o Azure varia) |
| *Password spray* | `entra` | muitos `status_code` = `50126` de um `src_endpoint.ip` para usuários diferentes |
| Token reutilizado de outro país | `entra` | sign-in **não interativo** com sucesso de país diferente do interativo recente do mesmo `user` |
| Credencial nova em service principal | `entra` | `api.operation` = `Add service principal credentials` ou `Update application – Certificates and secrets management` |
| Admin novo | `entra` | `class_uid` = 3001, `activity_id` = 7 (Attach Policy) |
| Leitura em massa de segredo | `resource` | muitos `SecretGet` do mesmo `actor` em poucos minutos |
| `kubectl exec` em produção | `resource` | `api.operation` = `create pods exec` |
| Força bruta no banco | `resource` | `class_uid` = 3002 e `status_id` = 2 repetidos para o mesmo `user` |

## Operação e saúde

- **Atraso do Activity Log.** O Azure publica cada evento alguns minutos depois de ele acontecer. Por isso a janela de leitura do `activity` recomeça **20 minutos antes** do fim da anterior; o que volta repetido é pulado (memória da janela) ou descartado pelo dedupe (`eventDataId`). O indicador de atraso em **Saúde do Pipeline** fica, por construção, em torno de 20 minutos. Se o log do worker mostrar `evento publicado ... depois do eventTimestamp`, aquele tenant atrasa mais que a sobreposição e eventos com esse atraso podem não ser lidos.
- **Teto por ciclo.** O `activity` lê até 50 páginas por ciclo (somando as assinaturas) e o `entra`/`resource` até 5.000 mensagens ou 5 minutos. No teto o ciclo guarda a posição exata e o próximo continua dali; **Saúde do Pipeline** marca o stream como em *backlog*.
- **Entrega at-least-once.** A posição dos Event Hubs (offset de cada partição) é gravada no CentralOps **depois** de o lote ser entregue aos destinos. Se o ciclo falhar, a leitura recomeça do mesmo offset e o dedupe absorve a repetição. O CentralOps não usa *consumer group* do Kafka nem grava offset no Azure.
- **Primeira coleta.** No modo workspace, os três streams começam pelo que entrou no workspace nas últimas 24 horas. No modo Event Hubs, o `activity` começa pela última hora e o `entra`/`resource` leem tudo o que o hub ainda retém (até 7 dias no Standard). Para trazer mais, use o **backfill** da integração: no modo workspace ele funciona nos três streams, dentro da retenção do workspace; no modo Event Hubs, só no `activity` (até 90 dias).
- **Modo workspace: o cursor é a ingestão.** Cada stream lê pelo `ingestion_time()` das linhas, não pelo `TimeGenerated`: um evento que o Azure entrega com horas de atraso ainda é lido, porque entrou no workspace depois do último ciclo. Linhas ingeridas no mesmo instante (um lote inteiro tem o mesmo `ingestion_time()`) são desempatadas por um hash da linha, então nenhuma é pulada nem repetida entre páginas. O ciclo lê até 5 minutos antes de agora e até 10 páginas de 5.000 linhas; no teto, o próximo ciclo continua da última linha lida. O indicador de atraso em **Saúde do Pipeline** mostra o instante de ingestão da última linha.

## Solução de problemas

**`activity` traz poucos eventos, sem erro nenhum.** Quase sempre é o papel no escopo errado, no modo Event Hubs. Com o papel dado num **recurso** (por exemplo, *Log Analytics Reader* no workspace), a assinatura aparece na listagem, mas a API do Activity Log devolve só os eventos dos recursos que o app consegue ler. Num caso real, eram 2 eventos em vez de cerca de 90 por dia. O teste de conexão acusa isso (`só tem papel em recursos dentro delas`). Atribua o papel do Passo 1 na assinatura ou no management group, ou, se o Activity Log já vai para um workspace, use o modo workspace.

**`entra`/`resource` com erro `nenhuma fonte configurada`.** O campo **Fonte** está vazio. Informe o Workspace ID ou o namespace do Event Hubs. Antes desta versão, o stream saía como saudável com zero eventos.

**`o workspace … não recebeu nada em … nos últimos 7 dias`.** O workspace não tem os logs daquele stream: falta o diagnostic setting (Passo 3 para o Entra, Passo 4 para recursos e Activity Log). Para conferir no workspace: `union isfuzzy=true SigninLogs, AuditLogs | summarize count() by Type`. O erro some sozinho no primeiro ciclo depois que os logs começarem a chegar.

**`sem leitura no workspace` (HTTP 403).** O app não tem *Log Analytics Reader* no workspace, ou o papel ainda está propagando (até 10 minutos).

**`workspace … não encontrado` ou `Event Hubs (...): inacessível` com o nome do workspace.** O campo **Fonte** recebeu o **nome** do workspace (ou de uma storage account) no lugar do **Workspace ID**. O nome de um workspace também é um nome válido de namespace, então o CentralOps tenta conectar num Event Hub que não existe. Use o GUID da página **Overview** do workspace.

**`Log Analytics recusou a consulta (SEM…)`.** A API não aceitou a consulta. O código entre parênteses é o do KQL. Abra um chamado com ele.

**`activity: nenhuma assinatura visível`.** O papel do Passo 1 não está no escopo das assinaturas, ou ainda está propagando (até 10 minutos). Confira: `az role assignment list --assignee APP_ID --all -o table`.

**Uma assinatura some da coleta.** O log do worker registra `HTTP 403 ao ler /subscriptions/... — assinatura pulada`: o papel foi removido daquela assinatura. As demais continuam sendo coletadas.

**O teste passa, mas `entra`/`resource` não trazem nada.**

- Confira se o diagnostic setting aponta para o hub certo (`centralops-entra` ou `centralops-resource`, exatamente).
- No portal, abra o hub → **Métricas** → *Incoming Messages*. Se está em zero, o problema é a exportação, não o CentralOps.
- Exportar sign-in exige Entra ID P1/P2 (Passo 3).

**`Event Hubs (...): inacessível (KafkaConnectionError)` ou `TimeoutError`.** A porta 9093 está bloqueada na saída da rede do CentralOps, ou o namespace tem firewall sem o IP do CentralOps, ou o tier é Basic.

**Erro de autenticação depois de meses funcionando.** O Client Secret expirou (`AADSTS7000222`). Crie um novo no app registration e atualize o campo *Client Secret*.

**Log do worker: `perdeu N mensagem(ns) por retenção`.** O CentralOps ficou parado por mais tempo que a retenção do hub (7 dias no Standard) e as mensagens mais antigas já tinham sido apagadas pelo Azure. Não há como recuperá-las do hub; para o Activity Log, rode um backfill da janela.

**Backfill não traz nada em `entra`/`resource` (modo Event Hubs).** Fila não tem janela de tempo, então o backfill por período não se aplica a esses dois streams e é ignorado. O backfill do `activity` funciona normalmente. No modo workspace o backfill vale para os três.

## Limitações conhecidas

- **Só nuvem pública do Azure.** Azure Government e Azure China usam outros hosts de login, ARM e Event Hubs e não são aceitos.
- **Só Client Secret.** Autenticação do app por certificado ou por *workload identity federation* ainda não é suportada.
- Os nomes dos hubs são fixos (`centralops-entra` e `centralops-resource`).
- Uma fonte por integração: ou workspace, ou Event Hubs. Para ler de dois workspaces, crie duas integrações.
- Só tabelas no plano **Analytics** do Log Analytics foram validadas. Tabelas nos planos *Basic* ou *Auxiliary* têm consulta cobrada e KQL restrito, e não foram testadas.
- **Storage Account não é lida** como destino de diagnostic setting.
- O texto e a saída de um Run Command não vêm pelo Activity Log: só o fato de ele ter sido executado. O conteúdo está no host (ver [Run Command: o que vem e o que não vem](#run-command-o-que-vem-e-o-que-não-vem)).
- Um evento do Activity Log publicado mais de 20 minutos depois de acontecer pode não ser lido (ver [Operação e saúde](#operação-e-saúde)).
- Resource logs de categorias não listadas acima saem como Base Event; o detalhe fica no `raw`.
- A integração não lê NSG flow logs, que o Azure grava em Storage Account. Pelo workspace, só se você usar *Traffic Analytics* (tabelas que esta integração ainda não lê).
