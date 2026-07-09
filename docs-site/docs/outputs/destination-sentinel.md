---
sidebar_position: 7
title: "Destino: Microsoft Sentinel"
description: Envie seus eventos normalizados para o Microsoft Sentinel e acompanhe a entrega pela própria interface do CentralOps.
---

# Destino: Microsoft Sentinel

O destino **Microsoft Sentinel** envia automaticamente os eventos normalizados pelo CentralOps para o seu workspace do Microsoft Sentinel (Azure Monitor). Depois de configurado, o CentralOps cuida do envio em segundo plano — você só precisa acompanhar a saúde da entrega na interface.

> Criar e editar destinos é uma tarefa de **administrador**. Analistas e engenheiros podem acompanhar a entrega nas telas de monitoramento.

## Quando usar

- **Centralizar tudo em um único SIEM**: sua equipe já investiga e cria regras no Microsoft Sentinel e quer que os eventos do Sophos, Wazuh e demais integrações cheguem lá normalizados, em um só workspace.
- **Encaminhar apenas o que importa**: você quer mandar para o Sentinel somente alertas de severidade alta, mantendo o restante apenas no CentralOps, para reduzir custo de ingestão no Azure.
- **Redundância entre tenants**: você precisa de uma cópia dos eventos críticos em um segundo workspace (de outro tenant) para retenção ou continuidade, sem depender de um único ambiente.

## O que você precisa preparar no Azure

Antes de configurar o destino no CentralOps, peça à equipe responsável pelo Azure que prepare o lado da Microsoft. São quatro itens, todos criados no **Azure Portal**:

| Item | Onde criar no Azure |
|------|---------------------|
| **Data Collection Endpoint (DCE)** | Azure Portal → Monitor → Data Collection Endpoints |
| **Data Collection Rule (DCR)** | Azure Portal → Monitor → Data Collection Rules |
| **Registro de aplicativo (App Registration)** | Azure Portal → Microsoft Entra ID → App registrations |
| **Permissão da aplicação no DCR** | No DCR criado → Access control (IAM) → atribuir a função **Monitoring Metrics Publisher** à aplicação |

Ao final dessa preparação no Azure, você terá em mãos os valores que vai informar no CentralOps:

| Valor | Onde encontrar no Azure |
|-------|--------------------------|
| **URI do Data Collection Endpoint** | DCE → propriedade de URI de ingestão |
| **Immutable ID do DCR** | DCR → Properties |
| **Stream Name** | DCR → Data sources → seu Custom Text Logs (ex.: `Custom-CentralOps_CL`) |
| **Tenant ID** | Microsoft Entra ID → Overview |
| **Client ID** | App registration → Overview |
| **Client secret** | App registration → Certificates & secrets (copie o valor no momento da criação — ele não aparece de novo) |

> O passo a passo detalhado dentro do Azure (criar DCE, DCR, app e atribuir a função) faz parte da administração do seu ambiente Microsoft. Se você não tiver acesso a esse portal, peça esses valores à equipe responsável pela nuvem Azure.

## Passo 1: Guardar a credencial no CentralOps

O **client secret** da aplicação é uma credencial sensível. O CentralOps guarda esse segredo em um cofre de credenciais e usa apenas uma referência a ele na configuração do destino — assim a senha nunca fica exposta na tela de configuração.

1. Abra o menu **Administração**.
2. Acesse a área de armazenamento de credenciais da plataforma.
3. Adicione uma nova credencial informando um **nome** (ex.: `sentinel-client-secret`) e o **valor** (o client secret copiado do Azure).
4. Salve e anote o nome da credencial — você vai selecioná-lo na configuração do destino.

> O cofre de credenciais é definido pela equipe de infraestrutura no momento do deploy. Se você não encontrar essa área na sua instalação ou não tiver permissão para usá-la, fale com o administrador da plataforma.

## Passo 2: Criar o destino no CentralOps

1. No menu **Operação → Destinos**, clique para criar um **novo destino**.
2. No tipo de destino, selecione **Microsoft Sentinel**.
3. Preencha os campos da configuração:

| Campo | O que informar |
|-------|----------------|
| **Nome** | Um nome reconhecível, ex.: `Sentinel - Produção` |
| **Data Collection Endpoint** | O URI do DCE copiado do Azure |
| **DCR Immutable ID** | O Immutable ID do DCR |
| **Stream Name** | O nome do stream (ex.: `Custom-CentralOps_CL`) |
| **Tenant ID** | O Tenant ID do Entra ID |
| **Client ID** | O Client ID do registro de aplicativo |
| **Credencial (client secret)** | Selecione a credencial criada no Passo 1 |
| **Verificar TLS** | Deixe marcado (recomendado) |

4. Use o botão de **testar destino**. O CentralOps verifica se consegue se conectar ao Azure, autenticar a aplicação e se ela tem a permissão necessária. Um teste bem-sucedido confirma que a credencial e os identificadores estão corretos.
5. **Salve** o destino.

## Passo 3: Criar a regra que envia os eventos

Criar o destino não envia nada sozinho — você precisa de uma regra de roteamento que diga **quais** eventos vão para ele.

1. No menu **Operação → Roteamento**, crie uma **nova regra**.
2. Configure:

| Campo | O que informar |
|-------|----------------|
| **Nome** | Ex.: `Sentinel - Todos os eventos` |
| **Condição** | Deixe em branco para enviar tudo, ou defina um filtro (ex.: somente severidade alta) |
| **Destino** | Selecione o destino Sentinel criado no Passo 2 |
| **Prioridade** | Deixe o valor padrão se não tiver motivo para alterar |
| **Ativa** | Deixe marcada |

3. Salve a regra. A partir daí, os eventos que atendem à condição passam a ser enviados ao Sentinel automaticamente.

## Passo 4: Conferir se os eventos estão chegando

### Pela interface do CentralOps

1. Vá em **Normalização → Saúde do Pipeline**.
2. Localize o destino **Sentinel - Produção**.
3. O status deve aparecer como **ativo** (verde). Se aparecer **inativo/parado**, geralmente a credencial não foi resolvida corretamente — veja a seção [Problemas comuns](#problemas-comuns).

### No próprio Microsoft Sentinel

Abra o seu **Log Analytics Workspace** no Azure e consulte a tabela personalizada criada pelo DCR (ex.: `Custom_CentralOps_CL`). Os eventos enviados pelo CentralOps devem aparecer ali em poucos segundos.

## Como funciona a entrega

- **Entrega garantida (com possível duplicação)**: o CentralOps garante que todo evento seja entregue ao menos uma vez. Em caso de erro temporário no Azure, ele **reenvia automaticamente** — você não precisa fazer nada. Por isso, em situações raras, um mesmo evento pode ser gravado mais de uma vez no Sentinel.
- **Remoção de duplicados**: se precisar tratar duplicatas, cada evento carrega um identificador único nos seus metadados internos, que pode ser usado nas consultas do próprio Sentinel para agrupar registros repetidos.
- **Formato dos dados**: os eventos são enviados já normalizados pelo CentralOps. O mapeamento de colunas e tipos do lado do Azure é responsabilidade do DCR (configurado no Azure Portal).
- **Proteção contra instabilidade**: se o destino ficar instável (lentidão ou erros repetidos), o CentralOps reduz e reorganiza o envio sozinho e volta ao normal quando o Azure se recupera. Nenhuma ação manual é necessária.

## Problemas comuns

Em todos os casos abaixo, comece pela tela **Normalização → Saúde do Pipeline** para ver o status e a mensagem mais recente do destino.

| Sintoma | O que verificar |
|---------|-----------------|
| **Destino aparece inativo/parado** | A credencial (client secret) foi selecionada na configuração do destino? Ela ainda existe no cofre de credenciais com o mesmo nome? Reabra o destino em **Operação → Destinos** e confirme a credencial. |
| **Eventos rejeitados por falha de autenticação** | Tenant ID e Client ID estão corretos (copie diretamente do Azure)? O client secret expirou no Azure? A aplicação tem a função **Monitoring Metrics Publisher** no DCR? Se o secret expirou, gere um novo no Azure e atualize a credencial no cofre. |
| **Eventos rejeitados por formato/esquema** | O **Stream Name** e o **DCR Immutable ID** informados no destino correspondem exatamente aos do Azure? Corrija o valor no destino. Se o status na Saúde do Pipeline continuar com erro, acione o suporte. |
| **Falha de conexão / tempo esgotado** | Pode ser instabilidade temporária de rede — o CentralOps tenta reenviar sozinho. Se persistir, confirme com a equipe de infraestrutura se o endpoint do Azure ainda existe e está acessível a partir do ambiente. |
| **Limite de taxa (muitos eventos)** | O Azure pode estar limitando o volume. O CentralOps ajusta o ritmo de envio automaticamente. Se o aviso persistir, considere restringir a regra de roteamento para enviar menos eventos, ou verifique os limites de cota do seu workspace no Azure. |

> Se a credencial estiver errada e não houver opção de editá-la diretamente no destino, a forma garantida de corrigir é atualizar a credencial no cofre (ou recriar o destino com a credencial certa). Em caso de dúvida, acione o administrador da plataforma.

## Próximos passos

- **Os dados estão chegando?** Acompanhe o volume no menu **Visão geral → Dashboard**.
- **Eventos parando antes de sair?** Confira o menu **Normalização → Quarentena**.
- **Quer adicionar outros destinos?** Veja a [visão geral de destinos](./overview.md).
