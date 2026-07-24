---
sidebar_position: 1
title: Dashboard
description: Visão geral em tempo quase real da coleta, do roteamento e da entrega de eventos aos destinos.
---

# Dashboard

O Dashboard é a primeira tela da plataforma (menu **Visão geral -> Dashboard**). Ele reúne, num só lugar, os indicadores que mostram se os eventos estão **chegando** das suas fontes, sendo **normalizados** corretamente e sendo **entregues** aos destinos configurados.

A tela acompanha duas pontas do fluxo:

- **Entrada (coleta):** saúde das integrações, taxa de ingestão de eventos e eventos que ficaram retidos para correção.
- **Saída (entrega):** destinos ativos, taxa de entrega por destino e eventos que não puderam ser entregues e aguardam reenvio.

Quem é Viewer enxerga todos os indicadores. Quem é administrador vê, além disso, os atalhos para reprocessar reenvios, ajustar roteamento e operar destinos.

## Quando usar

- **Check-in matinal do SOC:** ao começar o turno, abrir o Dashboard para responder "os eventos continuaram fluindo durante a madrugada?" e "alguma fonte parou de enviar dados?".
- **Durante um incidente:** confirmar rapidamente se um pico de eventos é real (e não falha de coleta) e se os eventos estão chegando aos destinos onde o time investiga.
- **Verificação de entrega:** depois de configurar ou alterar um destino, conferir se os eventos voltaram a fluir e se a fila de reenvio não está crescendo.

## Indicadores de entrada (coleta)

### Funil do pipeline

Visão de ponta a ponta do fluxo: quantos eventos foram **coletados**, **normalizados**, **roteados** e **entregues** na janela atual. Uma queda brusca entre duas etapas indica onde investigar — por exemplo, muitos eventos coletados mas poucos normalizados apontam para a Quarentena; muitos roteados mas poucos entregues apontam para um destino com problema.

Os cartões de indicadores resumem a mesma história:

| Indicador | O que mostra |
|---|---|
| **Ingestão (EPS)** | Eventos por segundo entrando pelas suas fontes. |
| **Cobertura de mapping** | Proporção entre os campos que o mapeamento aproveita e o total observado (aproveitados + novos detectados). Um mergulho logo após uma atualização costuma significar que o fornecedor passou a enviar campos novos, não que o mapeamento piorou. |
| **Quarentena 24h** | Taxa (ou volume) de eventos retidos por erro de normalização. |
| **Roteados (/min)** | Eventos processados pelas regras de roteamento e o percentual descartado (drop). |
| **Destinos** | Quantos destinos estão saudáveis em relação ao total. |
| **Fontes ativas** | Quantas integrações estão coletando e quantas apresentam erro. |

### Taxa de eventos

Gráfico com o volume de eventos processados por minuto nas últimas 24 horas. Picos podem indicar um ataque em andamento ou uma recoleta histórica (recoleta de dados antigos) recém-iniciada.

### Saúde da coleta

Resumo do estado das suas integrações. Clique para abrir o detalhamento por fonte em **Visão geral -> Integrações**.

| Cor | Situação |
|---|---|
| Verde | Todas as fontes ativas e coletando normalmente |
| Amarelo | Ao menos uma fonte com problema leve (ex.: limite de requisições atingido, credencial perto de expirar) |
| Vermelho | Uma fonte importante parada há vários minutos |

### Eventos em quarentena

Quantidade de eventos que ficaram retidos porque não puderam ser interpretados ou mapeados (por exemplo, depois de uma mudança na API de um fornecedor). Clique para abrir a tela **Normalização -> Quarentena** e tratar a fila.

| Cor | Faixa | Leitura |
|---|---|---|
| Verde | 0 | Nenhum erro; os mapeamentos estão funcionando |
| Amarelo | Poucos eventos | Reveja o mapeamento se o número continuar subindo |
| Vermelho | Muitos eventos | Provável mudança no formato da fonte; trate com prioridade |

Mais detalhes em [Quarentena](./quarantine.md). Esta fila é diferente da fila de reenvio de saída descrita abaixo.

### Últimos eventos normalizados

Lista dos eventos mais recentes, com horário, fonte, severidade e um título curto. Clique em um item para expandir e ver o conteúdo completo do evento (somente leitura).

## Indicadores de saída (entrega)

### Destinos e saúde agregada

Mostra quantos destinos estão ativos e o estado geral da entrega. Clique para abrir **Operação -> Destinos**, onde cada destino tem seu próprio indicador. (A tela **Destinos** aparece apenas para administradores.)

| Estado | O que significa |
|---|---|
| Saudável | Conexão OK e nenhuma falha nas últimas 24h |
| Degradado | Conexão OK, mas há eventos aguardando reenvio |
| Indisponível | Destino temporariamente bloqueado pela proteção contra destino instável, após falhas repetidas |
| Desativado | Destino inativo |

> A **proteção contra destino instável** pausa automaticamente o envio para um destino que vem falhando muito, para não acumular erros. Quando o destino volta a responder, o envio é retomado.

### Taxa de entrega por destino

Tabela com os destinos mais ativos nas últimas 24h, mostrando o nome e o tipo do destino (por exemplo, Wazuh, Splunk, Sentinel, S3), o volume entregue e o estado atual. Clique em um destino para ver os detalhes de saúde e a fila de reenvio.

### Fila de reenvio

Total de eventos que **não puderam ser entregues** a um destino e ficaram aguardando uma nova tentativa.

| Cor | Faixa | Leitura |
|---|---|---|
| Verde | 0 | Tudo sendo entregue normalmente |
| Amarelo | Poucos eventos acumulados | Verifique o destino afetado e drene a fila |
| Vermelho | Muitos eventos acumulados | Investigue com prioridade (credencial expirada, destino fora do ar, rede) |

Clique para abrir a fila e filtrar pelo destino e pelo tipo de erro (por exemplo, credencial inválida, tempo limite excedido, destino temporariamente bloqueado).

### Rotas ativas

Mostra quantas regras de roteamento estão ativas e o volume de eventos que passou por cada uma nas últimas 24h. Se não houver nenhuma regra ativa, os eventos seguem para o destino marcado como padrão da organização — e, na ausência dele, vão para a fila de reenvio em vez de serem perdidos.

Clique para abrir **Operação -> Roteamento**, onde é possível ajustar as condições e os destinos de cada rota. (As telas **Roteamento** e **Fluxo de dados** aparecem apenas para administradores.)

> A ativação ou desativação global do roteamento é definida pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma.

## Fluxos de trabalho

### Investigação rápida de entrada

1. Abra **Visão geral -> Dashboard**.
2. Olhe o **funil do pipeline** e a **taxa de eventos** e veja se há picos ou quedas inesperados.
3. Confira **Saúde da coleta** para identificar a fonte com problema.
4. Revise **Últimos eventos normalizados** para identificar a fonte e o padrão.
5. Se houver muitos eventos em quarentena, vá para [Quarentena](./quarantine.md).
6. Para investigar eventos específicos, use **Operação -> Investigações** ([busca de eventos](./search.md)).

### Verificação de entrega

1. Olhe **Destinos e saúde agregada**. Há algum destino indisponível?
2. Se sim:
   - Abra **Operação -> Destinos** e selecione o destino.
   - Use a ação de testar conexão para validar a conectividade e a credencial.
   - Se o teste falhar, rotacione a credencial ou acione o responsável pelo destino. Veja [Operar destinos](../outputs/destination-operations.md).
3. Se a fila de reenvio estiver acima de zero:
   - Abra a fila e identifique o tipo de erro predominante.
   - Credencial inválida: rotacione a credencial do destino.
   - Tempo limite ou destino bloqueado: aguarde a retomada automática e teste o destino novamente.
   - Em seguida, reprocesse a fila de reenvio na tela de Destinos. Veja [Operar destinos](../outputs/destination-operations.md).

### Monitoramento contínuo

- **Início do turno:** confira o **funil do pipeline** e a **saúde da coleta** para saber se os eventos fluíram normalmente durante a madrugada; para triagem de detecções, vá a **Operação -> Detecções**.
- **Ao longo do dia:** verifique se a taxa de eventos está dentro do normal, se todos os destinos estão saudáveis e se a fila de reenvio não está crescendo.
- **Sinal de alerta:** se a fila de reenvio começa a crescer ou destinos passam a Degradado/Indisponível, investigue imediatamente — geralmente é credencial expirada, rede ou destino fora do ar.

## Limitações

- O Dashboard mostra apenas as **últimas 24 horas**. Para outras janelas de tempo, use a tela **Operação -> Investigações** ([busca de eventos](./search.md)).
- Os dados são atualizados aproximadamente **a cada minuto**, não em tempo real instantâneo.
- As agregações são por fonte (entrada) e por destino (saída). Para recortes personalizados — por exemplo, por fornecedor ou por organização — use a busca de eventos em **Operação -> Investigações** e exporte o resultado.

## Próximos passos

- **Muitos eventos em quarentena?** Vá para [Quarentena](./quarantine.md) e depois para o [guia de solução de problemas de normalização](../normalization/troubleshooting.md).
- **Destino indisponível ou fila de reenvio crescendo?** Vá para [Operar destinos](../outputs/destination-operations.md).
- **Ajustar regras de roteamento?** Vá para [Roteamento](../outputs/routing.md).
- **Busca avançada e histórico?** Use a tela **Operação -> Investigações** ([busca de eventos](./search.md)).
