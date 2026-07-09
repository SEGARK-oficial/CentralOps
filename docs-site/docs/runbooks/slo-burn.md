---
sidebar_position: 4
title: "Latência alta: dados chegando com atraso"
description: "O que verificar quando os dados demoram a chegar nos destinos e como reagir pela interface"
---

# Latência alta: dados chegando com atraso

Esta página ajuda você a investigar quando os eventos estão demorando para chegar nos destinos configurados — ou seja, quando o tempo entre o evento acontecer no produto de origem e ele aparecer no destino final está acima do esperado.

O CentralOps acompanha esse tempo de ponta a ponta (da coleta até a entrega). Quando ele cresce, alguns dados continuam chegando, mas atrasados — o que atrapalha investigações em tempo real e relatórios.

## Quando usar

Use este guia nestes cenários do dia a dia:

- Você recebeu um **alerta de latência de ponta a ponta acima do alvo** (por exemplo, dados levando mais de 5 minutos para chegar).
- Um destino específico aparece como **lento** na tela de **Saúde do Pipeline**, enquanto os outros seguem normais.
- Um cliente ou time interno **reportou que os dados estão chegando com atraso** em um destino (por exemplo, no SIEM ou no data lake).

## Como saber se está dentro do alvo

O tempo de ponta a ponta é o que mais importa: do momento em que o evento é criado no produto de origem até ele chegar ao destino configurado. Cada destino tem sua própria latência, e um destino lento não necessariamente significa que todos estão lentos.

Como referência de saúde:

| O que você vê | Situação |
|---|---|
| Latência de ponta a ponta em torno de 3 minutos ou menos | Dentro do alvo |
| Latência de ponta a ponta passando de 5 minutos | Em risco — investigue |

Para conferir o valor atual:

1. Abra o menu **Normalização -> Saúde do Pipeline**.
2. Veja o indicador de latência (tempo de entrega) no painel.
3. Observe se há um destino com latência muito acima dos demais.

## Passo a passo do diagnóstico

### 1. Confirme se há latência alta

Em **Normalização -> Saúde do Pipeline**, olhe o indicador de latência geral. Se estiver em torno de 3 minutos ou menos, está tudo bem. Se passar de 5 minutos, siga para o próximo passo.

### 2. Descubra qual destino está lento

Ainda em **Saúde do Pipeline**, observe os cartões/indicadores por destino. Compare a latência de cada um:

- Se apenas **um destino** está com latência alta (por exemplo, o SIEM mostra 8 minutos enquanto os outros mostram 1 a 2 minutos), o problema está concentrado nesse destino. Vá para a seção **O destino está lento**.
- Se **todos os destinos** estão lentos ao mesmo tempo, o gargalo provavelmente é a coleta ou o processamento. Continue no próximo passo.

### 3. Descubra qual etapa está lenta

A jornada de um evento tem três etapas. Identificar onde o tempo está sendo gasto direciona a solução:

| Etapa | Sintoma | Onde olhar |
|---|---|---|
| **Coleta** | A última coleta da origem demorou muito | **Visão geral -> Integrações** (veja o status e o horário da última coleta da integração) |
| **Normalização** | O evento foi coletado, mas demora para ser processado | **Normalização -> Saúde do Pipeline** e **Normalização -> Mappings** |
| **Entrega ao destino** | O evento foi processado, mas demora para chegar ao destino | **Operação -> Destinos** (somente admin) |

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

- Em **Normalização -> Saúde do Pipeline**, veja se o tempo de processamento (normalização) está alto mesmo com a coleta e a entrega normais.

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

Se a latência estava normal (por exemplo, 1 minuto) e disparou de uma vez (por exemplo, 8 minutos), siga este roteiro:

1. **Confirme o alcance:** em **Normalização -> Saúde do Pipeline**, veja se o atraso é em um destino só ou em todos.
2. **Verifique a coleta:** em **Visão geral -> Integrações**, confira se alguma integração começou a falhar ou se entrou um volume de eventos muito acima do normal (cliente novo, pico de tráfego).
3. **Verifique os destinos:** em **Operação -> Destinos** (admin), veja se algum destino ficou indisponível ou começou a limitar o recebimento — é o sinal da **proteção contra destino instável** ter sido acionada.
4. **Estabilize:** quando o destino voltar, **reprocese a fila de reenvio** na tela de Destinos para drenar o acúmulo.
5. **Escale se necessário:** se o atraso vem do volume geral ou de um gargalo de processamento, e não de um único destino instável, **abra um chamado para a equipe de infraestrutura**. Aumentar a capacidade de coleta e de processamento em segundo plano é uma ação de plataforma, feita fora da interface.

> Aumentar a quantidade de processamento em segundo plano (workers) e a capacidade da plataforma é definido pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma.

## Como prevenir

- **Acompanhe a tendência:** confira regularmente a latência por destino em **Normalização -> Saúde do Pipeline** para perceber a degradação antes de virar incidente.
- **Configure alertas:** garanta que há um alerta disparando quando a latência de ponta a ponta passa do alvo por tempo prolongado. A configuração de notificações é definida pela equipe de infraestrutura no momento do deploy. Se precisar ajustá-la, fale com o administrador da plataforma.
- **Planeje capacidade:** se o volume de eventos cresce de forma consistente, antecipe o aumento de capacidade com a equipe de infraestrutura.
- **Valide destinos novos:** antes de colocar um destino novo em produção, valide-o em ambiente de testes para entender seu comportamento sob carga.

## Próximos passos

- **Monitorar a latência:** vá a [Saúde do Pipeline](../operations/pipeline-health.md).
- **Verificar a coleta:** vá a [Collectors](../pipelines/collectors.md).
- **Ajustar um mapeamento:** vá a [Mappings](../normalization/overview.md).
- **Configurar destinos:** vá a [Destinos](../outputs/destinations.md) (somente admin).
- **Roteamento de eventos:** vá a [Roteamento](../outputs/routing.md) (somente admin).
