---
sidebar_position: 11
title: "Redis cheio: estado de controle"
description: "O que o Redis guarda, por que ele não cresce com o volume de eventos e o que fazer quando ele começa a evictar chaves"
---

# Redis cheio: estado de controle

O Redis do CentralOps guarda só **estado de controle**: cursores de coleta em
cache, travas, tokens OAuth, estado do disjuntor de cada destino, limites de taxa,
supressão por assinatura e contadores de observabilidade. Nenhum evento fica nele.

Até a versão anterior ao ADR-0018 Onda 5, o filtro de duplicatas gravava uma
chave por evento no Redis, e era isso que enchia a memória em volume alto. Esse
filtro agora roda em memória, dentro do processador de eventos, e o Redis deixou
de crescer com a taxa de eventos.

## Quando usar

- O alerta **CentralOpsDedupeEvicting** disparou (o nome vem da época do filtro
  antigo; hoje ele indica qualquer evicção).
- O Redis está perto do limite de memória.

## O que acontece quando não cabe

O Redis de cache usa a política `volatile-lru`: ao atingir o limite, ele apaga
chaves com prazo de validade para liberar espaço. Isso pode derrubar um token
OAuth em cache (a próxima coleta pede outro), soltar uma trava antes da hora (dois
ciclos do mesmo stream podem rodar juntos) ou esquecer o estado de um disjuntor.
Nada disso perde evento, mas gera trabalho repetido e ruído.

## O que fazer

### 1. Confira o que está ocupando

```bash
redis-cli -a "$REDIS_PASSWORD" DBSIZE
redis-cli -a "$REDIS_PASSWORD" INFO memory | grep -E "used_memory_human|maxmemory_human"
redis-cli -a "$REDIS_PASSWORD" INFO stats | grep evicted_keys
redis-cli -a "$REDIS_PASSWORD" --scan --pattern 'dedupe:*' | head
```

Chaves `dedupe:*` são resíduo da versão anterior: elas expiram sozinhas em até
24 horas depois da atualização. Se a memória cair nesse prazo, não há mais nada a
fazer.

### 2. Aumente o limite de memória

Se a evicção continua sem chaves `dedupe:*`, ajuste `REDIS_MAXMEMORY` no `.env` e
confirme que o limite do container (`deploy.resources.limits.memory`) é maior que
ele.

:::note[O broker fica em outra instância]
O agendamento e a fila de tarefas ficam num Redis separado (`redis-control`), com
política que **nunca** apaga dados. A pressão de memória do cache não afeta o
agendador.
:::

## Duplicatas no destino

O filtro de duplicatas lembra os eventos da última hora (até 200 mil por
partição do tópico `raw`; ajuste com `RAW_DEDUPE_WINDOW_SECONDS` e
`RAW_DEDUPE_WINDOW_MAX_IDS`). Um evento coletado de novo depois disso, ou relido
logo depois que um processador reiniciou, pode chegar duas vezes ao destino. A
entrega é "pelo menos uma vez"; veja as garantias em
[Arquitetura](../concepts/architecture.md).

## Próximos passos

- [Latência alta](./slo-burn.md) — quando os dados chegam com atraso.
- [Fila de reenvio e entrega](./dlq-and-destination-delivery.md) — o que acontece
  com o evento que não pôde ser entregue.
