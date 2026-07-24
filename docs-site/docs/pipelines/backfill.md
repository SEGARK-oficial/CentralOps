---
sidebar_position: 4
title: Backfill (recoleta histórica)
description: Recolha eventos antigos de uma integração para preencher lacunas ou reprocessar histórico.
---

# Backfill (recoleta histórica)

O backfill recolhe eventos antigos de uma integração e os reprocessa pela plataforma, como se tivessem chegado naquele momento. Use-o para preencher períodos em que a coleta ficou parada ou para trazer histórico de uma integração recém-adicionada.

A data e a hora originais de cada evento são preservadas — o backfill não "carimba" os eventos com a hora atual.

## Quando usar

### Caso 1: a coleta de uma integração ficou parada

A integração Sophos parou de coletar por 2 horas (por exemplo, credenciais expiradas). Depois de corrigir o acesso, você quer recuperar os eventos daquelas 2 horas que não entraram.

**O que fazer:** rode um backfill cobrindo o período em que a coleta esteve parada.

### Caso 2: integração nova, mas você quer o histórico recente

Você acabou de adicionar a integração Sophos hoje, mas precisa dos eventos dos últimos 7 dias para ter contexto nas investigações.

**O que fazer:** rode um backfill cobrindo os últimos 7 dias.

### Caso 3: trazer histórico para um período sob investigação

Durante uma apuração, você percebe que precisa de eventos de um vendor referentes a um intervalo específico do mês passado que ainda não foram coletados.

**O que fazer:** rode um backfill apenas para o intervalo de datas relevante.

## Como iniciar um backfill

1. Vá ao menu **Visão geral -> Integrações**.
2. Abra a integração da qual você quer recolher o histórico.
3. Inicie um backfill na própria tela da integração.
4. Informe o intervalo e a velocidade (veja abaixo) e confirme o início.

> O backfill é uma operação de administrador. Se você não vê a opção na tela da integração, peça a um administrador da plataforma.

### Intervalo de datas

| Campo | O que informar |
| --- | --- |
| Data/hora inicial | A partir de quando recolher os eventos. |
| Data/hora final | Até quando recolher os eventos. |

- **Limite no passado:** depende de quanto tempo o vendor guarda o histórico. A Sophos, por exemplo, costuma manter cerca de 90 dias. Não é possível recolher além do que o vendor ainda disponibiliza.
- **Futuro:** não é permitido escolher datas no futuro.

### Velocidade da recoleta

Você define a velocidade máxima da recoleta (quantos pedidos por segundo a plataforma faz ao vendor):

- **Mais devagar:** recolhe mais lentamente, mas pressiona menos a API do vendor. Boa escolha se o vendor é sensível a volume.
- **Mais rápido:** termina antes, mas aumenta o risco de o vendor recusar pedidos por excesso de chamadas.
- **Padrão:** um valor intermediário e equilibrado.

Se você não sabe o melhor valor, comece pelo padrão e ajuste conforme o comportamento (veja [Ajustar a velocidade](#ajustar-a-velocidade)).

## O que você vê durante o backfill

Depois de iniciar, a recoleta roda em segundo plano. Você pode fechar a página: ela continua mesmo sem você estar olhando.

Enquanto roda, a plataforma:

1. Recolhe os eventos do vendor dentro do intervalo escolhido, respeitando a velocidade definida.
2. Normaliza cada evento com o mapeamento atual da integração. Eventos que não puderem ser normalizados vão para a **Quarentena**.
3. Entrega os eventos normalizados aos destinos, seguindo as mesmas regras da coleta normal (veja [Destinos e entrega](#destinos-e-entrega)).

Ao final, a plataforma indica que o backfill foi concluído e quantos eventos foram importados.

## Acompanhar o progresso

Acompanhe a recoleta pela tela **Normalização -> Saúde do Pipeline**. Lá você vê, para o backfill em andamento:

- Se ainda está em execução ou se já terminou.
- Quantos eventos já foram importados e o percentual concluído.
- A taxa de importação e uma estimativa de tempo para terminar.

Você não precisa de nenhuma ferramenta externa nem de acesso técnico ao ambiente: tudo o que é preciso para monitorar o backfill está nessa tela.

### Quanto tempo costuma levar

A duração depende de três fatores principais:

- **Tamanho do intervalo:** quanto maior o período, mais tempo leva (7 dias levam cerca de 7 vezes o tempo de 1 dia).
- **Velocidade escolhida:** uma velocidade mais baixa torna a recoleta proporcionalmente mais lenta.
- **O próprio vendor:** algumas APIs de vendor respondem mais devagar, o que está fora do controle da plataforma.

Como referência, recolher 7 dias de uma integração com baixo volume de eventos (poucas centenas no total) costuma levar alguns minutos na velocidade padrão.

## Pausar e retomar

Você pode pausar um backfill em andamento a qualquer momento e retomá-lo depois:

- Ao **pausar**, a recoleta para imediatamente, sem descartar o que já foi coletado.
- A plataforma guarda o ponto exato em que parou.
- Ao **retomar**, ela continua de onde havia parado, sem repetir o que já foi recolhido.

Isso é útil, por exemplo, para pausar uma recoleta grande durante o horário de pico e retomá-la mais tarde.

## Ajustar a velocidade

Se necessário, você pode aumentar ou reduzir a velocidade da recoleta enquanto ela acontece.

| Situação | O que fazer |
| --- | --- |
| O vendor está aceitando bem os pedidos e você tem pressa | Aumente a velocidade para terminar antes. |
| O vendor começou a recusar pedidos por excesso de chamadas | Reduza a velocidade para aliviar a pressão. |
| A recoleta está mais lenta do que o esperado | Aumente um pouco a velocidade; se não melhorar, o gargalo pode ser o próprio vendor. |

Quando o vendor recusa pedidos temporariamente por volume, a plataforma reduz o ritmo e tenta novamente de forma automática — você não precisa fazer nada além de, se quiser, reduzir a velocidade.

## Reprocessar histórico com um novo mapeamento

Um cenário comum é trazer histórico e, depois, querer reaplicar um mapeamento atualizado sobre esses eventos.

1. Rode o backfill do período desejado.
2. Ajuste o mapeamento da integração em **Normalização -> Mappings**. Eventos novos passam a usar o mapeamento atualizado automaticamente.
3. Se eventos do histórico ficaram na **Quarentena** (por falha de normalização), reprocesse-os em **Normalização -> Quarentena** depois de ajustar o mapeamento.

> Eventos do histórico que **já foram normalizados** com um mapeamento anterior não são reaplicados automaticamente quando você muda o mapeamento. Reprocessar em massa eventos já normalizados ainda **não está disponível na interface** — não conte com esse passo hoje. Se você precisa garantir um mapeamento específico sobre todo o histórico, ajuste o mapeamento **antes** de iniciar o backfill.

## Recolher de várias integrações ao mesmo tempo

Você pode rodar backfills de integrações diferentes em paralelo. Inicie a recoleta em cada integração separadamente, na respectiva tela em **Visão geral -> Integrações**. Elas rodam de forma independente e não competem entre si.

## O backfill honra o filtro de coleta

Se a integração tem um [filtro de coleta](./collection-filters.md) ligado, **a recoleta histórica aplica o mesmo filtro**. Um backfill não é uma porta dos fundos para trazer o que o filtro corta.

Isso é intencional: sem essa regra, a recoleta voltaria cheia de eventos que o roteamento descarta em seguida, gastando o job inteiro com o que você decidiu não coletar.

Para recuperar de propósito um período que o filtro pulou, o caminho é explícito:

1. **Desligue** o filtro de coleta da integração.
2. Rode o backfill do período desejado.
3. **Religue** o filtro.

Enquanto o filtro estiver desligado, a coleta corrente também volta a trazer tudo — planeje a janela.

## Destinos e entrega

O backfill segue exatamente as mesmas regras de entrega da coleta normal:

- Os eventos são entregues aos mesmos destinos configurados para a integração.
- Quando há regras de roteamento definidas, os eventos podem ser enviados simultaneamente a vários destinos, conforme essas regras.
- As mesmas regras de ocultação de dados sensíveis (PII) da coleta normal valem para o backfill.

Para entender como destinos e roteamento funcionam, veja [Roteamento e Destinos](../outputs/routing.md).

## Se algo der errado

| Sintoma | O que verificar / fazer |
| --- | --- |
| O backfill parou antes de concluir | Confira o status em **Normalização -> Saúde do Pipeline**. Pause e retome a recoleta. Se continuar parando, reduza a velocidade. Se persistir, fale com o suporte da plataforma. |
| A recoleta está muito lenta | Aumente um pouco a velocidade. Se não melhorar, o gargalo provavelmente é a API do vendor — aguarde ou fale com o suporte. |
| O vendor está recusando pedidos por excesso de chamadas | Reduza a velocidade da recoleta. A plataforma já tenta novamente de forma automática; reduzir o ritmo evita novas recusas. |
| Muitos eventos do backfill caíram na Quarentena | Revise o mapeamento da integração em **Normalização -> Mappings** e reprocesse os eventos em **Normalização -> Quarentena**. |

## Próximos passos

- **Acompanhar o progresso?** Vá a [Saúde do Pipeline](../operations/pipeline-health.md).
- **Eventos em quarentena?** Vá a [Quarentena](../operations/quarantine.md).
- **Ajustar o mapeamento?** Vá a [Mappings](../normalization/overview.md).
