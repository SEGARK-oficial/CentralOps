---
sidebar_position: 11
title: "Redis cheio: dimensionar o dedupe"
description: "Por que o Redis cresce, como calcular o tamanho necessário e como reduzir o consumo ajustando o TTL de dedupe"
---

# Redis cheio: dimensionar o dedupe

O CentralOps usa o Redis para filtrar eventos duplicados. Cada evento coletado
deixa uma chave temporária. Em volumes altos, essas chaves passam a ser o maior
consumidor de memória da instância — e, quando o Redis enche, ele começa a apagar
essas chaves **antes da hora**, o que faz eventos já entregues serem entregues de
novo, sem nenhum aviso.

Esta página explica como calcular o tamanho necessário e como reduzir o consumo.

## Quando usar

- O Redis está perto do limite de memória, ou você viu o número de chaves crescer
  muito (`DBSIZE` na casa dos milhões).
- Você percebeu **eventos duplicados** chegando no destino.
- Você vai aumentar bastante o volume coletado e quer dimensionar antes.

## A fórmula

O número de chaves de dedupe **não** depende do volume acumulado. Depende só de
duas coisas: a taxa de eventos e a janela de dedupe (o TTL).

> **chaves ≈ eventos por segundo × TTL em segundos**
>
> **memória ≈ chaves × 115 bytes**

Os 115 bytes por chave incluem o texto da chave, o valor, e o overhead interno do
Redis (índice principal e índice de expiração).

### Exemplos práticos

Para uma coleta de **100 eventos por segundo** (~6.000 por minuto):

| TTL de dedupe | Chaves em regime | Memória aproximada |
|---|---|---|
| 4 horas | ~1,4 milhão | ~165 MB |
| 12 horas | ~4,3 milhões | ~500 MB |
| 24 horas | ~8,6 milhões | ~1 GB |
| 7 dias | ~60 milhões | ~7 GB |

Compare com o limite configurado (`REDIS_MAXMEMORY`, padrão **512 MB**). No
exemplo acima, um TTL de 24 horas já exige o dobro do padrão.

:::warning[O que acontece quando não cabe]
O Redis de cache/dedupe usa a política `volatile-lru`: ao atingir o limite, ele
**apaga chaves de dedupe antes do TTL** para liberar espaço. A partir daí, um
evento que já passou volta a parecer inédito e é **entregue de novo ao destino**.
Não há erro nem alerta — o sintoma aparece como duplicidade no SIEM.
:::

## Como reduzir o consumo

### 1. Diminua o TTL de dedupe (mais eficaz)

Vá em **Configurações → Coleta & Entrega** e ajuste o **TTL de dedupe (horas)**.

O TTL não precisa cobrir dias. Ele existe para cobrir a janela em que o mesmo
evento pode ser reprocessado: sobreposição entre coletas, tentativas automáticas
e reentrega após falha de um worker. Nesta arquitetura essa janela é de **1 hora**
no pior caso automático.

- **Mínimo permitido: 4 horas** — quatro vezes o pior caso, com folga.
- Reduzir de 24h para 4h corta o consumo por **6**.

A alteração vale em até 30 segundos, sem reiniciar nada. Chaves já criadas
mantêm o TTL antigo até expirarem, então a queda de memória é gradual.

### 2. Aumente o limite de memória

Se o volume exige uma janela maior, ajuste `REDIS_MAXMEMORY` no `.env` para um
valor acima do calculado pela fórmula — e confirme que o limite do container
(`deploy.resources.limits.memory`) é maior que ele.

### 3. Confira o que está ocupando

```bash
redis-cli -a "$REDIS_PASSWORD" DBSIZE
redis-cli -a "$REDIS_PASSWORD" INFO memory | grep -E "used_memory_human|maxmemory_human"
redis-cli -a "$REDIS_PASSWORD" INFO stats | grep evicted_keys
```

`evicted_keys` maior que zero e crescendo é a confirmação de que o Redis está
apagando chaves por falta de espaço — trate como incidente e reduza o TTL ou
aumente o limite.

:::note[O broker fica em outra instância]
O agendamento e a fila de tarefas ficam num Redis separado (`redis-control`), com
política que **nunca** apaga dados. A pressão de memória do dedupe não afeta o
agendador — foi uma separação feita justamente para isso.
:::

## Próximos passos

- [Latência alta](./slo-burn.md) — quando os dados chegam com atraso.
- [Fila de reenvio e entrega](./dlq-and-destination-delivery.md) — o que acontece
  com o evento que não pôde ser entregue.
