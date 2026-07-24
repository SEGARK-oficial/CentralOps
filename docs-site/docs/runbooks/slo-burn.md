---
sidebar_position: 4
title: "Latência alta: dados chegando com atraso"
description: "O que verificar quando os dados demoram a chegar nos destinos, o que o cartão de latência realmente mede e como reagir pela interface"
---

# Latência alta: dados chegando com atraso

Esta página ajuda quando os dados estão demorando a aparecer no destino. Antes de investigar, é preciso separar duas coisas que costumam ser confundidas — e que **não** são o mesmo número:

| Conceito | O que é | O CentralOps mede? |
|---|---|---|
| **Ponta a ponta** | Do instante em que o evento acontece no produto de origem até ele aparecer no destino | **Não.** Continua sendo um alvo de negócio legítimo — "o plantão precisa ver o alerta em até X minutos" —, mas hoje nenhuma tela da plataforma mostra esse número |
| **Entrega do lote ao destino** | Quanto tempo o CentralOps levou para empurrar um lote já pronto até o destino | **Sim.** É o cartão **latência média (s)** em **Operação -> Destinos** |

:::warning[O cartão não é ponta a ponta]
O relógio do cartão **latência média (s)** começa no **envio** do lote e para quando o destino responde. Ele inclui a quebra do lote em pedaços e todas as retentativas. Ele **não** inclui o tempo que o evento passou parado na origem, nem a coleta, nem a normalização.

O valor está em **segundos**, não em minutos. Um cartão marcando `2` quer dizer "a entrega do lote levou 2 segundos" — não "os dados estão 2 minutos atrasados".
:::

Na prática: se o time reclama de atraso ponta a ponta, o cartão sozinho não confirma nem desmente a queixa. Ele responde uma pergunta mais estreita — "o gargalo está na saída para este destino?" — e é assim que ele deve ser usado.

## Quando usar

Use este guia nestes cenários do dia a dia:

- Um cliente ou time interno **reportou que os dados estão chegando com atraso** em um destino (por exemplo, no SIEM ou no data lake) e você precisa descobrir se o gargalo é a coleta, a normalização ou a entrega.
- Um destino específico aparece com **latência média (s)** muito acima dos demais em **Operação -> Destinos**.
- A **fila de reenvio** de um destino está crescendo, sinal de que as entregas estão falhando ou demorando.

## Como ler o cartão de latência

Cada destino tem sua própria latência de entrega, e um destino lento não significa que todos estão lentos. Como ponto de partida para ler o cartão **latência média (s)**:

| Latência média do destino | Leitura |
|---|---|
| Abaixo de **2 s** | Normal. O destino aceita os lotes sem esforço |
| **2 a 10 s** | Aceitável para lotes grandes ou destinos naturalmente mais lentos. Acompanhe a tendência |
| **10 a 30 s** | Investigue. Normalmente é lote fatiado em muitos pedaços, destino devolvendo erro e forçando retentativa, ou o destino realmente lento |
| Acima de **30 s** | Sinal forte de retentativas ou de lote muito fatiado. Não conclua "estourou o tempo limite" só por causa deste número: o limite de 30 s é **por pedaço enviado**, e o número aqui é a soma do lote inteiro — um lote em cinco pedaços rápidos passa de 30 s sem nenhum tempo limite ter sido atingido |

:::note[Estas faixas são referência inicial, não SLO de fábrica]
A plataforma não vem com nenhum limite de latência configurado, e não existe alerta automático em cima desse número. Um destino que sempre entrega em 6 segundos não está com defeito. Meça a linha de base de cada destino durante uma semana normal e trate como problema o **desvio da própria linha de base**, não o cruzamento das faixas acima.
:::

Para conferir o valor atual:

1. Abra o menu **Operação -> Destinos** e clique no destino que quer investigar.
2. Veja o cartão **latência média (s)** — é o tempo de entrega do lote para aquele destino.
3. Repita para os demais destinos e observe se há um com latência muito acima.

:::warning[Gráfico começando do zero não é destino que ficou lento]
Até a atualização em que essa medição passou a ser gravada, a série de latência nunca recebia um único ponto — o cartão ficava permanentemente vazio, para todos os destinos. O histórico anterior à atualização continua vazio e **não** será preenchido retroativamente.

Depois de atualizar, o gráfico começa do zero e só cresce com as entregas novas. Uma linha subindo do nada nos primeiros dias é a série se enchendo, não o destino degradando. Só compare com a linha de base depois de acumular alguns dias de dados.
:::

:::note[O indicador é por destino]
A latência é medida **por destino**, no momento da entrega. Não existe um número
único de "latência geral" na tela de Saúde do Pipeline — compare os destinos
entre si, que é o que aponta o gargalo.
:::

## Passo a passo do diagnóstico

### 1. Confirme se a entrega está lenta

Em **Operação -> Destinos**, abra cada destino e olhe a **latência média (s)**, sempre comparando com a linha de base daquele destino. Alguns segundos é o esperado; dezenas de segundos indicam retentativas. Se o número estiver dentro do normal para todos os destinos, a entrega **não** é o gargalo — o atraso relatado está antes dela, na coleta ou na normalização, e você deve ir direto ao passo 3.

### 2. Descubra qual destino está lento

Compare a **latência média (s)** de cada destino:

- Se apenas **um destino** destoa (por exemplo, o SIEM em 25 s enquanto os outros ficam em 1 a 2 s), o problema está concentrado nesse destino. Vá para a seção **O destino está lento**.
- Se **todos os destinos** estão lentos ao mesmo tempo, o gargalo provavelmente não é nenhum deles. Continue no próximo passo.

### 3. Descubra qual etapa está lenta

A jornada de um evento tem três etapas, e só a última é cronometrada. Nas duas primeiras você trabalha com sinais indiretos:

| Etapa | Sinal disponível | Onde olhar |
|---|---|---|
| **Coleta** | Horário da última coleta bem-sucedida e o atraso desde então; erros recorrentes | **Visão geral -> Integrações** e **Normalização -> Saúde do Pipeline** |
| **Normalização** | Eventos retidos em quarentena, campos novos não mapeados, erro na última execução | **Normalização -> Saúde do Pipeline** e **Normalização -> Mappings** |
| **Entrega ao destino** | Tempo cronometrado do lote, em segundos | **Operação -> Destinos** (somente admin) |

:::note[Não existe um cronômetro de normalização]
A tela de **Saúde do Pipeline** não mostra "quanto tempo a normalização levou". Ela mostra quando foi a última coleta bem-sucedida, o atraso acumulado desde então, o erro mais recente, os campos novos detectados e quantos eventos foram para a quarentena nas últimas 24 horas. Use esses sinais para inferir onde o tempo está indo — não procure um número de duração que a plataforma não coleta.
:::

## Causas e o que fazer

### Causa 1: a coleta da origem está lenta

A coleta fica lenta quando o produto de origem responde devagar, devolve um volume muito grande de eventos de uma vez, ou está limitando a quantidade de requisições.

Como identificar:

- Em **Visão geral -> Integrações**, abra a integração suspeita e confira o status e o horário da última coleta. Coletas demoradas ou com erros recorrentes indicam problema na origem.

O que você pode fazer pela interface:

- **Origem respondendo devagar:** aumente o intervalo de coleta da integração, se essa opção estiver disponível na tela da integração. Isso reduz a pressão sobre a origem. Lembre-se do efeito colateral: os dados passam a chegar com um espaçamento maior entre as coletas.
- **Volume muito alto de eventos:** alinhe com o cliente/origem se o volume é esperado. Quando possível, reduza o que é enviado já no produto de origem (filtros do lado do vendor).

Quando escalar:

- Se a origem está aplicando limite de requisições e você não consegue resolver ajustando o intervalo de coleta, **abra um chamado para a equipe de infraestrutura**. O controle fino de ritmo de coleta é definido pela plataforma e não fica exposto na interface.

### Causa 2: a normalização está lenta

A normalização fica lenta quando o mapeamento de um produto tem muitas regras condicionais encadeadas ou transformações pesadas, fazendo cada evento demorar mais para ser convertido.

Como identificar:

- Em **Normalização -> Saúde do Pipeline**, procure a combinação: coleta em dia (última coleta recente, sem atraso acumulado) e entrega normal no cartão de **latência média (s)**, mas o destino recebendo menos do que o esperado. Quarentena subindo na mesma janela reforça a hipótese de que o problema está na conversão, e não no transporte.

O que você pode fazer pela interface:

1. Abra o menu **Normalização -> Mappings** e selecione o mapeamento do produto afetado.
2. No editor de mapeamento, **simplifique as regras**: consolide condições encadeadas em regras mais diretas e remova transformações que não são essenciais.
3. Para campos que não precisam de transformação, use um valor padrão em vez de uma cadeia longa de condições.
4. Antes de publicar, use a opção de **testar o mapeamento com um evento de exemplo** na própria tela de Mappings para confirmar que o resultado continua correto.

> Cache de transformações repetidas para acelerar a normalização está no roadmap. Ainda não está disponível — não conte com esse recurso hoje.

### Causa 3: o destino está lento

Esta é a causa mais comum quando apenas um destino aparece atrasado. Acontece quando o destino está limitando o recebimento (rejeitando lotes por excesso), está sobrecarregado, ou não responde. Quando a fila de envio para esse destino cresce, os eventos vão se acumulando.

O CentralOps tem proteções automáticas para isso:

- Uma **proteção contra destino instável**, que pausa temporariamente o envio quando o destino para de responder, evitando que a fila de um destino problemático contamine os demais.
- Uma **fila de reenvio**, para onde vão os eventos que não puderam ser entregues, de modo que possam ser reenviados quando o destino voltar ao normal.

Como identificar (somente admin):

- Em **Operação -> Destinos**, localize o destino lento e verifique o indicador de saúde/atraso e o tamanho da fila de envio. Um destino com fila crescendo e entregas atrasadas é o causador.

O que você pode fazer pela interface (somente admin):

1. Em **Operação -> Destinos**, confirme se a **proteção contra destino instável** está ativa para aquele destino — isso indica que o CentralOps já detectou instabilidade e está segurando o envio.
2. Quando o destino voltar a responder, **reprocesse a fila de reenvio** na tela de Destinos para reenviar os eventos que ficaram acumulados.
3. Acione o responsável pelo destino (por exemplo, o administrador do SIEM ou do data lake) para verificar limite de recebimento e capacidade. Muitas vezes a solução está do lado do destino: aumentar a capacidade de ingestão ou liberar o limite.

Quando escalar:

- Ajustar o ritmo de envio para um destino, dedicar capacidade de processamento a um destino específico (isolamento) ou aumentar a capacidade de processamento em segundo plano são **ações da equipe de infraestrutura**. Se a fila de um destino não baixa mesmo com o destino respondendo, **abra um chamado para a infraestrutura/SRE** descrevendo qual destino está afetado e desde quando.

## Quando a latência sobe de repente

Se a **latência média (s)** de um destino estava estável (por exemplo, em torno de 1 segundo) e disparou de uma vez (por exemplo, para 25 segundos), siga este roteiro:

1. **Confirme o alcance:** em **Operação -> Destinos**, veja se o atraso é em um destino só ou em todos.
2. **Verifique a coleta:** em **Visão geral -> Integrações**, confira se alguma integração começou a falhar ou se entrou um volume de eventos muito acima do normal (cliente novo, pico de tráfego).
3. **Verifique os destinos:** em **Operação -> Destinos** (admin), veja se algum destino ficou indisponível ou começou a limitar o recebimento — é o sinal da **proteção contra destino instável** ter sido acionada.
4. **Estabilize:** quando o destino voltar, **reprocese a fila de reenvio** na tela de Destinos para drenar o acúmulo.
5. **Escale se necessário:** se o atraso vem do volume geral ou de um gargalo de processamento, e não de um único destino instável, **abra um chamado para a equipe de infraestrutura**. Aumentar a capacidade de coleta e de processamento em segundo plano é uma ação de plataforma, feita fora da interface.

> Aumentar a quantidade de processamento em segundo plano (workers) e a capacidade da plataforma é definido pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma.

## Como prevenir

- **Anote a linha de base:** registre a **latência média (s)** típica de cada destino em uma semana normal. É contra esse número que faz sentido comparar depois — as faixas desta página são só um ponto de partida.
- **Acompanhe a tendência:** confira regularmente a **latência média (s)** por destino em **Operação -> Destinos** para perceber a degradação antes de virar incidente. Não existe alerta automático de latência na plataforma; a conferência é manual, ou feita fora dela pela equipe de infraestrutura, em cima das métricas exportadas.
- **Não prometa um alvo ponta a ponta que ninguém mede:** se o contrato ou o acordo interno fala em "evento visível no SIEM em até X minutos", combine desde já como esse número será verificado — hoje ele não sai de nenhuma tela do CentralOps.
- **Planeje capacidade:** se o volume de eventos cresce de forma consistente, antecipe o aumento de capacidade com a equipe de infraestrutura.
- **Valide destinos novos:** antes de colocar um destino novo em produção, valide-o em ambiente de testes para entender seu comportamento sob carga.

## Próximos passos

- **Monitorar a latência por destino:** vá a **Operação -> Destinos**. Para a saúde geral do processamento, veja [Saúde do Pipeline](../operations/pipeline-health.md).
- **Verificar a coleta:** vá a [Collectors](../pipelines/collectors.md).
- **Ajustar um mapeamento:** vá a [Mappings](../normalization/overview.md).
- **Configurar destinos:** vá a [Destinos](../outputs/destinations.md) (somente admin).
- **Roteamento de eventos:** vá a [Roteamento](../outputs/routing.md) (somente admin).
