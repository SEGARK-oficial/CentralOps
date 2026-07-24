---
sidebar_position: 1
title: Collectors
description: Como o CentralOps coleta eventos dos seus fornecedores de forma contínua e o que você acompanha na interface.
---

# Collectors

Os **Collectors** são o que mantém o CentralOps puxando eventos das suas integrações de segurança (Sophos, Microsoft Defender, NinjaOne e outros) de forma contínua e automática. Você não dispara a coleta manualmente: cada integração tem o seu próprio ritmo, e a plataforma busca os eventos novos em segundo plano, no intervalo configurado para aquela origem.

Esta página explica o que os coletores fazem, quando a coleta acontece e onde você acompanha tudo pela interface.

## Quando usar

Você recorre a esta área principalmente para **acompanhar e diagnosticar** a coleta de eventos. Cenários típicos de SOC:

- **Os alertas de um fornecedor pararam de chegar.** Você percebe que não vê eventos novos de uma integração há um tempo e precisa confirmar se a coleta está rodando ou se a credencial expirou.
- **Investigação aponta para um vendor "silencioso".** Durante uma análise você nota uma lacuna nos dados de uma origem e quer verificar se houve erro de coleta naquele período.
- **Triagem diária de saúde.** No começo do turno você confere rapidamente se todas as integrações estão coletando normalmente, antes de confiar nos painéis.

## Como a coleta funciona

Em linguagem de produto, sem entrar em infraestrutura:

| Etapa | O que acontece |
|-------|----------------|
| Coleta contínua | A plataforma consulta a API de cada fornecedor em paralelo, em intervalos definidos por integração (de cerca de 1 minuto para detecções em tempo real, alertas e índices de SIEM, a alguns minutos para logs de auditoria). Exemplos: Sophos, Microsoft Defender, Wazuh Indexer, NinjaOne, Sentinel. |
| Só o que é novo | Cada coleta continua de onde a anterior parou, então a plataforma busca apenas os eventos que ainda não tinha visto, em vez de recomeçar do zero. |
| Remoção de duplicados | Se o mesmo evento chega mais de uma vez, a cópia repetida é descartada automaticamente. |
| Credenciais sempre válidas | Para integrações que usam login do fornecedor, a plataforma renova as credenciais de acesso sozinha, sem você precisar reconectar a cada vez. |

O resultado dessa etapa são os eventos brutos, que seguem para a normalização, o roteamento e, por fim, para os destinos configurados.

### Coletar menos, quando faz sentido

Por padrão a plataforma coleta **tudo** o que o fornecedor oferece naquele fluxo, e o que não interessa é descartado depois, no roteamento. Isso funciona bem até o volume da origem ficar maior do que a coleta consegue transportar — aí o coletor passa a gastar cada ciclo trazendo eventos que serão jogados fora logo em seguida, e começa a ficar para trás.

Para esse caso, algumas integrações permitem restringir a **própria consulta** feita ao fornecedor. Veja [Filtro de coleta](./collection-filters.md).

### Coleta em tempo real x coleta em lote

A plataforma trata as origens com prioridades diferentes:

- **Tempo real** — detecções e alertas (por exemplo, alertas do Sophos, incidentes do Defender). São coletados nos intervalos mais curtos para que cheguem o quanto antes ao seu fluxo de trabalho.
- **Auditoria e histórico** — atividades e logs de auditoria (por exemplo, atividades do NinjaOne). São coletados com menos frequência, já que um atraso de alguns minutos não atrapalha a análise.

## O que você acompanha na interface

### Status de cada integração

No menu **Visão geral -> Integrações** você vê a lista de origens conectadas e o estado de cada uma. Quando uma integração entra em erro (por exemplo, credencial expirada ou inválida), ela aparece sinalizada e o administrador é notificado para reconectar.

### Saúde da coleta

No menu **Normalização -> Saúde do Pipeline** você acompanha se a coleta está fluindo:

- Se há acúmulo de eventos aguardando processamento (sinal de que a entrada está mais rápida que o processamento em segundo plano).
- Alertas de erro de integração, com a origem afetada.
- Indicadores que viram vermelho quando uma integração está com problema persistente.

Use essa tela como primeira parada quando suspeitar que algum fornecedor parou de enviar dados.

### Coletores

No menu **Operação -> Collectors** você vê o panorama da coleta por integração — quais estão ativas, quando ocorreu a última coleta e quantos eventos vieram. É a visão consolidada para a triagem do dia a dia.

## O que a plataforma faz quando algo dá errado

Você não precisa intervir na maioria das falhas — a plataforma já trata os casos comuns:

- **Erro temporário** (limite de requisições do fornecedor, tempo esgotado): a coleta é tentada de novo automaticamente pouco depois, sem perder eventos.
- **Erro permanente** (credencial inválida ou expirada): a integração entra em estado de erro, o indicador de saúde fica vermelho e o administrador é alertado para reconectar.
- **Reinício da plataforma**: coletas em andamento terminam de forma ordenada e o ponto de onde parar é preservado, então não há perda de dados.

## Coleta histórica (recoleta)

Quando você precisa trazer eventos de um período passado — por exemplo, depois de conectar uma integração nova ou para preencher uma lacuna identificada numa investigação — é possível solicitar uma **recoleta histórica** daquela origem. O CentralOps aplica a mesma remoção de duplicados, então reprocessar um intervalo não gera eventos repetidos.

## Dimensionamento e desempenho

A capacidade de coleta (quantos eventos a plataforma processa em paralelo) é definida pela equipe de infraestrutura no momento do deploy. Se você notar acúmulo constante de eventos na tela **Normalização -> Saúde do Pipeline** mesmo em operação normal, fale com o administrador da plataforma para avaliar o dimensionamento. Não há ação de escalonamento na interface do usuário.

## Próximos passos

- **Acompanhar a saúde do pipeline:** menu **Normalização -> Saúde do Pipeline**.
- **Coleta chegando com horas de atraso:** veja [Eventos chegando horas depois](../runbooks/collection-lag-backlog.md).
- **Ver e gerenciar as integrações conectadas:** menu **Visão geral -> Integrações**.
- **Entender como os eventos são normalizados:** veja [Normalização](../normalization/overview.md).
- **Entender para onde os eventos vão depois da coleta:** veja [Destinos](../outputs/destinations.md) e [Roteamento](../outputs/routing.md).
