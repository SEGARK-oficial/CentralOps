---
sidebar_position: 4
title: CrowdStrike Falcon
description: Colete alertas do CrowdStrike Falcon e rode consultas FQL ao vivo
---

# CrowdStrike Falcon

Conecte o CrowdStrike Falcon ao CentralOps para coletar automaticamente as detecções de endpoints da sua organização. Além da coleta contínua, você pode rodar consultas **FQL ao vivo** direto nas suas integrações do Falcon, sem deixar o CentralOps.

## Quando usar

- **Centralizar detecções do Falcon no SOC.** Traga os alertas de malware e comportamento suspeito do CrowdStrike para o CentralOps e analise-os ao lado das demais fontes, sem precisar abrir o console Falcon para cada detecção.
- **Rodar buscas FQL ao vivo no Falcon.** Quando você precisa investigar um indicador (hash, IP, domínio), lance uma consulta FQL diretamente do CentralOps, alcançando os alertas mais recentes do Falcon sem esperar a próxima coleta automática.
- **Correlacionar ameaças entre fornecedores.** Cruze as detecções do CrowdStrike com eventos de Sophos, Wazuh e outras integrações para investigar um incidente de ponta a ponta.
- **Encaminhar para SIEM e resposta.** Depois de coletados, os eventos do Falcon seguem pelas regras de roteamento para os seus destinos (SIEM, data lake, automação de resposta).

## Quem pode fazer o quê

- **Administrador da plataforma:** cria e edita a integração (menu **Visão geral -> Integrações**).
- **Operador ou superior:** rodam consultas FQL ao vivo (menu **Operação -> Busca Federada**).
- **Demais perfis:** visualizam os alertas já coletados (tela **Operação -> Investigações**).

## Pré-requisitos

Antes de começar, garanta que você tem:

- Acesso de **administrador no CrowdStrike Falcon** da sua organização, para gerar as credenciais de API.
- Acesso de **administrador no CentralOps**, para criar a integração.
- A **região correta** do seu tenant Falcon (US-1, US-2, EU-1 ou Government).

## Passo 1: Criar uma chave de API no Falcon

Estas etapas são feitas no console do Falcon, não no CentralOps.

1. Acesse o **CrowdStrike Falcon** (`https://falcon.crowdstrike.com` ou a URL da sua região).
2. No menu, vá para **Support & resources → API Clients & Keys** (ou similiar, dependendo da versão).
3. Clique em **Add API Client** para criar uma nova chave.
4. Preencha os dados:
   - **Client name:** um nome descritivo, por exemplo `CentralOps`.
   - **Description:** opcional, para lembrar o propósito.

5. Conceda **apenas as permissões necessárias** (princípio do menor privilégio):

   | Permissão no Falcon | Para que serve |
   |---|---|
   | `alerts:read` | Obrigatória — permite coletar detecções. |

6. Clique em **Add** para confirmar.
7. O Falcon exibe o **Client ID** e o **Client Secret**.

   :::warning[Copie o Client Secret na hora]

   O **Client Secret** é mostrado uma única vez. Copie e guarde em local seguro (um gerenciador de senhas, por exemplo). Se você perder, será preciso gerar uma nova chave.

   :::

## Passo 2: Identificar sua região

Você vai precisar da **base URL region-aware** ao criar a integração. Identifique pela URL que você usa para acessar o Falcon:

| Sua URL ou região | Base URL para o CentralOps |
|---|---|
| **https://falcon.crowdstrike.com** (padrão US) | `https://api.crowdstrike.com` |
| **https://falcon.us-2.crowdstrike.com** (US-2) | `https://api.us-2.crowdstrike.com` |
| **https://falcon.eu-1.crowdstrike.com** (EU-1) | `https://api.eu-1.crowdstrike.com` |
| **https://falcon.laggar.gcw.crowdstrike.com** (Government) | `https://api.laggar.gcw.crowdstrike.com` |

Anote a URL correta para usar no próximo passo — usar a URL errada resultará em erro de autenticação.

## Passo 3: Criar a integração no CentralOps

1. No menu lateral, vá em **Visão geral -> Integrações**.
2. Clique para adicionar uma nova integração.
3. Escolha **CrowdStrike Falcon** na lista de plataformas e avance.
4. Preencha os campos do formulário:

   | Campo | O que informar |
   |---|---|
   | Nome | Um nome para identificar esta conexão, por exemplo "CrowdStrike - Acme Corp". |
   | Client ID | O Client ID gerado no Passo 1. |
   | Client Secret | O Client Secret gerado no Passo 1. |
   | Base URL (região) | A URL region-aware do Passo 2. |

5. Use a opção de **testar a conexão**. O CentralOps valida as credenciais e confirma acesso à API. Se aparecer um erro, confira:
   - Client ID ou Client Secret digitados incorretamente (copie novamente).
   - Base URL errada para sua região.
   - Falta da permissão `alerts:read` no Falcon.

6. Salve a integração. As credenciais são armazenadas de forma criptografada e a integração entra no estado de aguardando a primeira coleta.

## Passo 4: Acompanhar a primeira coleta

A primeira coleta acontece automaticamente, poucos minutos depois de salvar. Acompanhe o status na própria tela da integração, em **Visão geral -> Integrações**.

Quando houver detecções recentes no Falcon:

- O status da integração muda para ativo e o indicador de saúde fica verde.
- Os eventos ficam pesquisáveis em **Operação -> Investigações**.

Se nada aparecer depois de alguns minutos, veja a seção [Solução de problemas](#solução-de-problemas) abaixo.

## Passo 5 (opcional): Rodar consultas FQL ao vivo

Se você precisa investigar um indicador específico ou procurar eventos mais recentes sem esperar a próxima coleta automática:

1. No menu, vá em **Operação -> Busca Federada** (disponível para Operador ou superior).
2. Selecione **CrowdStrike Falcon** como a integração.
3. Escolha o intervalo de tempo (até 7 dias).
4. Escreva sua consulta em **FQL** (Falcon Query Language). Exemplos:
   - `event_type:ProcessRollup2` — todos os eventos de processo.
   - `event_type:ProcessRollup2 AND process_name:cmd.exe` — eventos cmd.exe.
   - `event_type:NetworkListenIP4 AND local_port:4444` — conexões na porta 4444.

5. Clique em **Buscar** e acompanhe o progresso. Os resultados chegam em tempo real.

:::info[Ajuda com FQL]
Para aprender a sintaxe completa de FQL, consulte a [documentação oficial do CrowdStrike](https://developer.crowdstrike.com/falcon/documentation/#tag/query-events-get-events).
:::

## O que é coletado

Cada alerta do CrowdStrike chega ao CentralOps com informações como:

| Informação | Exemplo |
|---|---|
| Identificador do alerta | `crowdstrike-alert-123` |
| Nível de severidade | Alto, Médio, Baixo |
| Tipo de detecção | Prevenção de malware, Exploit, Comportamento suspeito |
| Processo/serviço envolvido | `svchost.exe`, `powershell.exe` |
| Host de origem | `workstation-01` |
| Endereço IP de origem | `192.168.1.100` |
| Timestamp | `2026-04-27T10:00:00Z` |

O CentralOps converte esses dados para o formato padronizado da plataforma, de modo que os alertas do Falcon podem ser pesquisados e correlacionados com as demais fontes. Se algum campo não puder ser convertido, o evento vai para a **Quarentena** (menu **Normalização -> Quarentena**), onde você pode revisar e reprocessar.

## Capacidades de consulta

A integração CrowdStrike suporta:

| Capacidade | Suporte |
|---|---|
| **Dialeto FQL** | Sim — consultas ao vivo direto na API do Falcon. |
| **Janela máxima** | 7 dias — o Falcon não retorna alertas mais antigos que isso. |
| **Modo de execução** | Síncrono — a resposta chega em poucos segundos. |

Para rodar uma consulta, acesse **Operação -> Busca Federada** e selecione CrowdStrike como integração.

## Solução de problemas

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| Erro de credenciais inválidas ao testar a conexão | Client ID ou Client Secret incorretos, ou expirados | Gere uma nova chave no Falcon, apague a antiga e atualize os valores na integração do CentralOps (**Visão geral -> Integrações**). Teste a conexão de novo. |
| Erro de autenticação ao testar (HTTP 401 ou 403) | Base URL errada para a sua região | Confira em qual URL você acessa o Falcon (falcon.crowdstrike.com, falcon.us-2, falcon.eu-1, etc.) e coloque a base URL correspondente no Passo 2. |
| Nenhum evento aparecendo | O Falcon não tem detecções recentes, ou o evento foi para a quarentena | Confirme no console Falcon se há detecções recentes. Se houver, aguarde alguns minutos. Se ainda assim nada aparecer em **Operação -> Investigações**, verifique a tela **Normalização -> Quarentena**. |
| Volume de eventos muito baixo | O Falcon está limitando as requisições de API (rate limit) | Abra **Normalização -> Saúde do Pipeline** e veja a integração do CrowdStrike para confirmar o limite de taxa. O intervalo de coleta é definido pela equipe de infraestrutura no momento do deploy. Se precisar ajustá-lo, fale com o administrador da plataforma. |
| Consulta FQL retorna erro "Invalid filter" | Sintaxe FQL inválida | Confira a [documentação oficial de FQL](https://developer.crowdstrike.com/falcon/documentation/#tag/query-events-get-events) ou reduza a complexidade (tente `event_type:ProcessRollup2` antes de filtros mais complexos). |

## Streams disponíveis

A integração do CrowdStrike coleta um tipo de dado:

| Tipo de dado | Descrição | Necessário? |
|---|---|---|
| Detecções | Alertas de malware, exploits, comportamento suspeito e outras ameaças. | Obrigatório |

A frequência de coleta é definida pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma.

## Próximos passos

- **Eventos aparecendo?** Veja o [Dashboard](../operations/dashboard.md).
- **Algo na quarentena?** Veja a [Quarentena](../operations/quarantine.md).
- **Quer rodar consultas FQL ao vivo?** Veja [Busca Federada](../operations/federated-search.md).
- **Adicionar outro fornecedor?** Veja a [visão geral de Integrações](./overview.md).
