---
sidebar_position: 6
title: Microsoft Defender
description: Conecte o Microsoft Defender ao CentralOps para coletar incidentes, alertas e executar hunts com KQL
---

# Microsoft Defender

Conecte o CentralOps ao seu **Microsoft Defender** para coletar incidentes e alertas automaticamente, normalizá-los e disponibilizá-los para busca, investigação e correlação com as demais fontes de segurança. Além disso, execute buscas ao vivo (advanced hunting com KQL) diretamente pela plataforma de forma federada.

## Quando usar

- **Centralizar incidentes e alertas do EDR/XDR da Microsoft.** Você usa Microsoft Defender para proteção de endpoints e quer trazer as detecções para o CentralOps e analisá-las ao lado das outras origens de segurança.
- **Correlacionar ameaças entre fornecedores.** Cruze os incidentes do Defender com eventos de Sophos, CrowdStrike, Wazuh e outras integrações para investigar um incidente de ponta a ponta.
- **Fazer hunts ao vivo com KQL (Kusto Query Language).** Use o menu **Operação → Busca federada** para escrever queries KQL, executar advanced hunting e coletar resultados sem sair do CentralOps.
- **Encaminhar para SIEM e data lake.** Depois de coletados, os eventos do Defender seguem pelas regras de roteamento para os seus destinos (Sentinel, Splunk, S3), permitindo alertar e responder de forma centralizada.

## Quem pode fazer o quê

- **Administrador da plataforma:** cria e edita a integração (menu **Visão geral -> Integrações**).
- **Operador ou superior:** executa buscas ao vivo e triagem de detecções (menu **Operação -> Busca federada** e **Operação -> Detecções**).
- **Demais perfis:** visualizam os incidentes e alertas já coletados (tela **Operação -> Investigações**).

## Pré-requisitos

Antes de começar, garanta que você tem:

- Acesso de **administrador no Azure AD** (Entra ID) da sua organização, para registrar uma aplicação e gerar credenciais.
- Acesso de **administrador no CentralOps**, para criar a integração.
- **Permissões de API no Microsoft Defender**: a aplicação registrada precisa das permissões `SecurityAlert.Read.All` e `SecurityIncident.Read.All` (no mínimo).

## Passo 1: Registrar uma aplicação no Azure AD

Estas etapas são feitas no Azure, não no CentralOps.

1. Acesse o **Azure Portal** (`https://portal.azure.com`).
2. Navegue até **Azure Active Directory** → **App registrations** (Registos de aplicações).
3. Clique em **+ New registration** (+ Novo registo).
4. Preencha os dados:
   - **Name:** um nome descritivo, por exemplo **CentralOps Defender Integration**.
   - **Supported account types:** escolha **Accounts in this organizational directory only** (sua organização).
5. Clique em **Register** (Registar).
6. Anote o **Application (client) ID** e o **Directory (tenant) ID**. Você vai precisar deles no Passo 3.

## Passo 2: Gerar o segredo da aplicação

1. No App Registration criado, abra a seção **Certificates & secrets** (Certificados e segredos).
2. Na aba **Client secrets**, clique em **+ New client secret** (+ Novo segredo do cliente).
3. Dê uma descrição, por exemplo **CentralOps**.
4. Escolha a data de expiração (recomenda-se 1 ou 2 anos).
5. Clique em **Add** (Adicionar).

:::warning[Copie o segredo na hora]
O **Client Secret** (Value) é mostrado apenas uma vez. Copie e guarde em local seguro (gerenciador de senhas ou cofre de segredos). Se perder, será preciso gerar um novo.
:::

6. Anote o valor do segredo.

## Passo 3: Conceder permissões de API

Ainda na App Registration:

1. Abra a seção **API permissions** (Permissões de API).
2. Clique em **+ Add a permission** (+ Adicionar uma permissão).
3. Escolha **Microsoft Graph**.
4. Selecione **Application permissions** (Permissões da aplicação) — **não delegadas**.
5. Na caixa de busca, procure por e selecione as permissões necessárias:
   - `SecurityAlert.Read.All` — para ler alertas.
   - `SecurityIncident.Read.All` — para ler incidentes.
6. Clique em **Add permissions** (Adicionar permissões).
7. De volta em **API permissions**, clique em **Grant admin consent for [sua organização]** (Conceder consentimento do administrador) e confirme.

O status das permissões deve mudar para verde, indicando consentimento concedido.

## Passo 4: Criar a integração no CentralOps

1. No menu lateral, vá em **Visão geral -> Integrações**.
2. Clique em **Adicionar integração**.
3. Escolha **Microsoft Defender** na lista de plataformas e avance.
4. Preencha os campos do formulário:

   | Campo | O que informar |
   |---|---|
   | Nome | Um nome para identificar esta conexão, por exemplo "Microsoft Defender - Produção". |
   | Tenant ID | O Directory (tenant) ID copiado no Passo 1. |
   | Client ID | O Application (client) ID copiado no Passo 1. |
   | Client Secret | O valor do segredo gerado no Passo 2. |

5. Use a opção de **testar a conexão**. O CentralOps valida as credenciais e verifica se consegue alcançar o Microsoft Defender. Se aparecer um erro, confira:
   - Tenant ID, Client ID ou Client Secret digitados incorretamente.
   - Permissões de API não concedidas ou não consentidas no Azure AD.
   - A aplicação registrada está no Azure AD correto.
6. Salve a integração. As credenciais são armazenadas de forma criptografada e a integração entra no estado de aguardando a primeira coleta.

## Passo 5: Acompanhar a primeira coleta

A primeira coleta acontece automaticamente, poucos minutos depois de salvar. Acompanhe o status na própria tela da integração, em **Visão geral -> Integrações**.

Quando houver incidentes ou alertas recentes no Defender:

- O status da integração muda para ativo e o indicador de saúde fica verde.
- Os eventos ficam pesquisáveis em **Operação -> Investigações**.

Se nada aparecer depois de alguns minutos, veja a seção [Solução de problemas](#solução-de-problemas) abaixo.

## Passo 6 (opcional): Executar buscas ao vivo com KQL

A partir da integração criada, você pode executar **advanced hunting** (buscas ao vivo com KQL) diretamente no CentralOps, sem precisar abrir o portal do Azure.

1. Vá em **Operação -> Busca federada**.
2. Selecione **Microsoft Defender** como plataforma.
3. Escolha o dialeto **KQL**.
4. Escreva sua query KQL (por exemplo, `DeviceProcessEvents | where FileName == "cmd.exe" | limit 100`).
5. Defina a janela de tempo (até 30 dias).
6. Clique em **Executar**. O CentralOps faz a busca no Defender e mostra os resultados.

:::note[Sintaxe KQL]
A sintaxe segue exatamente o Kusto Query Language do Microsoft Defender. Para detalhes e exemplos, consulte a [documentação oficial do KQL](https://learn.microsoft.com/en-us/azure/data-explorer/kusto/query/).
:::

## O que é coletado

Cada incidente e alerta do Defender chega ao CentralOps com informações como:

| Informação | Exemplo |
|---|---|
| Identificador do incidente | `incident-123` |
| Estado | Ativo, resolvido |
| Nível de severidade | Crítico, alto, médio, baixo, informativo |
| Título | `Suspected Windows Defender Tampering` |
| Dispositivos afetados | `workstation-01`, `server-02` |
| Usuários envolvidos | `user@domain.com` |
| Data e hora | `2026-05-15T14:30:00Z` |

O CentralOps converte esses dados para o formato padronizado da plataforma, de modo que os incidentes do Defender podem ser pesquisados e correlacionados com as demais fontes. Se algum campo não puder ser convertido, o evento vai para a **Quarentena** (menu **Normalização -> Quarentena**), onde você pode revisar e reprocessar.

## Capacidades de consulta

A integração do Microsoft Defender suporta:

| Capacidade | Detalhes |
|---|---|
| **Advanced hunting (KQL)** | Dialeto: KQL (Kusto Query Language) |
| **Modo de execução** | Ao vivo (síncrono) |
| **Janela máxima de tempo** | 30 dias |
| **Taxa de limite** | ~45 requisições por minuto (limite do Graph) |
| **Especificação** | Passthrough (KQL direto) ou Sigma (regras de detecção em YAML) |

Estas capacidades ficam visíveis em **Operação -> Busca federada**, no seletor de integrações.

## Solução de problemas

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| Erro "Falha na autenticação" ao testar a conexão | Client ID ou Client Secret incorretos, ou permissões não concedidas | Verifique no Azure AD: o Client ID está correto, o segredo não expirou, e o consentimento do administrador foi concedido para as permissões. Gere um novo segredo se necessário. |
| Erro "Permissão negada" (access denied) | Permissões de API não concedidas ou insuficientes | No Azure AD, em **API permissions**, confirme que `SecurityAlert.Read.All` e `SecurityIncident.Read.All` estão presentes e com consentimento concedido (ícone verde). |
| Nenhum evento aparecendo | Não há incidentes ou alertas recentes no Defender, ou evento foi para a quarentena | Confirme no console do Defender se há incidentes/alertas recentes. Se houver, aguarde alguns minutos. Se ainda assim nada aparecer em **Operação -> Investigações**, verifique a tela **Normalização -> Quarentena**. |
| Erro de conexão "connection refused" | Host ou porta incorretos (improvável — o Graph está online) | Confirme que sua rede permite conexões de saída para `graph.microsoft.com` na porta 443 (HTTPS). Se o bloqueio for por firewall, peça exceção. |
| Busca KQL retorna erro "Query syntax error" | Sintaxe KQL inválida | Verifique a sintaxe no [Microsoft Defender Advanced Hunting](https://security.microsoft.com/advanced-hunting) antes de copiar para o CentralOps. Erros comuns: falta de `|` entre operadores, nomes de tabela errados. |
| Busca KQL fica "Pendente" por muito tempo | Taxa de limite do Graph foi atingida (45/min) | Aguarde alguns minutos e tente novamente. Se precisar executar muitas queries, escalone-as. Para aumento permanente, fale com o suporte da Microsoft. |
| Integração parada por erro de token | A renovação automática do acesso falhou | O CentralOps tenta renovar o token sozinho. Se continuar falhando, verifique a validade do segredo no Azure AD (pode estar expirado). Gere um novo segredo e atualize a integração, testando a conexão em seguida. |

## Próximos passos

- **Eventos aparecendo?** Veja o [Dashboard](../operations/dashboard.md).
- **Algo na quarentena?** Veja a [Quarentena](../operations/quarantine.md).
- **Executar buscas ao vivo?** Veja o guia de [Busca federada](../operations/search.md) (em breve).
- **Adicionar outro fornecedor?** Veja a [visão geral de Integrações](./overview.md).
