---
sidebar_position: 2
title: Sophos Central
description: Conecte o Sophos Central ao CentralOps para coletar alertas e cases de ameaça
---

# Sophos Central

Conecte o Sophos Central ao CentralOps para coletar automaticamente os alertas e os cases de ameaça da sua organização e analisá-los junto com as demais fontes de segurança.

## Quando usar

- **Centralizar a detecção do Sophos no SOC.** Traga as detecções de malware e comportamento do Sophos Central para o CentralOps e analise-as ao lado das outras fontes, sem precisar abrir o console do Sophos para cada alerta.
- **Correlacionar ameaças entre fornecedores.** Cruze os alertas do Sophos com eventos de outras integrações para investigar um incidente de ponta a ponta na tela de **Investigações**.
- **Encaminhar para SIEM e resposta.** Depois de coletados, os eventos do Sophos seguem pelas regras de roteamento para os seus destinos (SIEM, data lake, automação de resposta), permitindo alertar e responder de forma centralizada.
- **Acompanhar cases de XDR/MDR.** Se você usa Managed Threat Response, traga também os cases de XDR para acompanhar investigações gerenciadas dentro do CentralOps.

## Quem pode fazer o quê

- **Administrador da plataforma:** cria e edita a integração (menu **Visão geral -> Integrações**).
- **Demais perfis:** visualizam os alertas e cases já coletados (menu **Operação -> Alertas** e tela de **Investigações**).

## Pré-requisitos

Antes de começar, garanta que você tem:

- Acesso de **administrador no Sophos Central** da sua organização, para gerar as credenciais de API.
- Acesso de **administrador no CentralOps**, para criar a integração.

## Passo 1: Gerar as credenciais no Sophos Central

Estas etapas são feitas no console do Sophos, não no CentralOps.

1. Acesse o **Sophos Central** (`https://central.sophos.com` ou a URL da sua região).
2. Abra a área de **API Credentials**, em **Settings**.
3. Crie uma nova credencial de API. Dê um nome descritivo, por exemplo **CentralOps**, para reconhecê-la depois.
4. Conceda **apenas as permissões necessárias** (princípio do menor privilégio):

   | Permissão no Sophos | Para que serve |
   |---|---|
   | `alerts:read` | Obrigatória — permite coletar os alertas. |
   | `cases:read` | Opcional — necessária só se você for coletar cases de XDR/Managed Threat Response. |

5. Ao concluir, o Sophos exibe o **Client ID** e o **Client Secret**.

   :::warning[Copie o Client Secret na hora]

   O **Client Secret** é mostrado uma única vez. Copie e guarde em local seguro (um gerenciador de senhas, por exemplo). Se você perder, será preciso gerar uma nova credencial.

   :::

## Passo 2: Identificar sua região e o Tenant ID

Você vai precisar destes dois dados ao criar a integração:

- **Região:** identifique pela URL da API do seu Sophos (por exemplo, **US**, **EU**, **APAC** ou **AU**).
- **Tenant ID:** no Sophos, em **Settings -> Organization**, copie o identificador da organização (o campo **Organization ID**, um código longo).

Anote os dois para usar no próximo passo.

## Passo 3: Criar a integração no CentralOps

1. No menu lateral, vá em **Visão geral -> Integrações**.
2. Clique para adicionar uma nova integração.
3. Escolha **Sophos Central** na lista de plataformas e avance.
4. Preencha os campos do formulário:

   | Campo | O que informar |
   |---|---|
   | Nome | Um nome para identificar esta conexão, por exemplo "Sophos - Acme Corp". |
   | Client ID | O Client ID gerado no Passo 1. |
   | Client Secret | O Client Secret gerado no Passo 1. |
   | Tenant ID | O identificador da organização (Passo 2). |
   | Região | A região do seu Sophos (Passo 2). |

5. Use a opção de **testar a conexão**. O CentralOps valida as credenciais, confirma o Tenant ID e verifica se a permissão de leitura de alertas está concedida. Se aparecer um erro, confira:
   - Client ID ou Client Secret digitados incorretamente (copie novamente).
   - Tenant ID ou região errados.
   - Falta da permissão `alerts:read` no Sophos.
6. Salve a integração. As credenciais são armazenadas de forma criptografada e a integração entra no estado de aguardando a primeira coleta.

## Passo 4: Acompanhar a primeira coleta

A primeira coleta acontece automaticamente, poucos minutos depois de salvar. Acompanhe o status na própria tela da integração, em **Visão geral -> Integrações**.

Quando houver alertas recentes no Sophos:

- O status da integração muda para ativo e o indicador de saúde fica verde.
- Os eventos passam a aparecer em **Operação -> Alertas**.

Se nada aparecer depois de alguns minutos, veja a seção [Solução de problemas](#solução-de-problemas) abaixo.

## Passo 5 (opcional): Coletar também os cases de XDR

Se você usa **Managed Threat Response / XDR** e quer trazer os cases para o CentralOps:

1. No Sophos, edite a credencial de API criada no Passo 1 e adicione a permissão `cases:read`.
2. Salve no Sophos.
3. Volte à integração no CentralOps (**Visão geral -> Integrações**) e teste a conexão novamente para validar a nova permissão.

A partir daí, os cases passam a ser coletados automaticamente, junto com os alertas.

## O que é coletado

Cada alerta do Sophos chega ao CentralOps com informações como:

| Informação | Exemplo |
|---|---|
| Identificador do alerta | `sophos-alert-123` |
| Nível de severidade | Crítico, alto, médio, baixo |
| Tipo de ameaça | Malware, comportamento suspeito, etc. |
| Nome da ameaça | `Trojan.X` |
| Host de origem | `workstation-01` |
| Endereço IP de origem | `192.168.1.100` |
| Data e hora | `2026-04-27T10:00:00Z` |

O CentralOps converte esses dados para o formato padronizado da plataforma, de modo que os alertas do Sophos podem ser pesquisados e correlacionados com as demais fontes. Se algum campo não puder ser convertido, o evento vai para a **Quarentena** (menu **Normalização -> Quarentena**), onde você pode revisar e reprocessar.

## Solução de problemas

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| Erro de credenciais inválidas ao testar a conexão | Client ID ou Client Secret incorretos ou expirados | Gere uma nova credencial no Sophos, apague a antiga e atualize os valores na integração do CentralOps (**Visão geral -> Integrações**). Teste a conexão de novo. |
| Nenhum evento aparecendo | O Sophos não tem alertas recentes, ou o evento foi para a quarentena | Confirme no console do Sophos se há alertas recentes. Se houver, aguarde alguns minutos. Se ainda assim nada aparecer em **Operação -> Alertas**, verifique a tela **Normalização -> Quarentena**. |
| Volume de eventos muito baixo | O Sophos está limitando as requisições de API (rate limit) | Abra **Normalização -> Saúde do Pipeline** e veja a integração do Sophos para confirmar o limite de taxa. O intervalo de coleta é definido pela equipe de infraestrutura no momento do deploy. Se precisar ajustá-lo, fale com o administrador da plataforma. Para um limite muito agressivo, considere contatar o suporte do Sophos. |
| Integração parada por erro de token | A renovação automática do acesso falhou | O CentralOps tenta renovar o acesso sozinho. Se continuar falhando, gere uma nova credencial no Sophos e atualize a integração, testando a conexão em seguida. |

## Streams disponíveis

A integração do Sophos coleta dois tipos de dados:

| Tipo de dado | Descrição | Necessário? |
|---|---|---|
| Alertas | Detecções de malware, comportamento suspeito e outras ameaças. | Obrigatório |
| Cases | Cases de XDR / Managed Threat Response. | Opcional |

A frequência de coleta é definida pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma.

## Próximos passos

- **Eventos aparecendo?** Veja o [Dashboard](../operations/dashboard.md).
- **Algo na quarentena?** Veja a [Quarentena](../operations/quarantine.md).
- **Adicionar outro fornecedor?** Veja a [visão geral de Integrações](./overview.md).
