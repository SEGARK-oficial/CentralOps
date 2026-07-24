---
sidebar_position: 3
title: "Eventos chegando horas depois"
description: "A coleta roda sem erro e mesmo assim os eventos chegam ao destino com horas de atraso — como confirmar o acúmulo, medir o quanto dele é descartável e escolher entre filtrar na origem, esperar ou pular o backlog."
---

# Eventos chegando horas depois

Este runbook trata de um sintoma específico: **a coleta está funcionando, sem erro, e mesmo assim os eventos chegam ao destino com horas de atraso**.

É diferente de "a integração parou de coletar" (veja [Integração ativa não está coletando](./scheduler-stuck.md)) e diferente de "o destino está lento" (veja [Latência alta](./slo-burn.md)). Aqui a coleta anda — só que mais devagar que o relógio.

## Quando usar

- Um analista percebe que os alertas no destino têm horário de **horas atrás**, embora continuem chegando.
- O card da integração em **Saúde do Pipeline** mostra **Última coleta em segundos** e **Atraso dos dados em horas**.
- O card ficou amarelo com **Backlog** marcado.
- A métrica `collector_cycles_skipped_locked_total` está subindo de forma sustentada.

## A assinatura do problema

Cada ciclo de coleta tem um **teto de eventos**. Ele existe para que um pico não trave o coletor num ciclo infinito. Quando o volume da fonte é maior que esse teto, ciclo após ciclo, o coletor avança — mas devagar demais.

O incidente que originou este runbook (julho de 2026, integração Wazuh):

| Sinal | Valor |
|---|---|
| Eventos acumulados aguardando coleta | **2.906.255** |
| Desses, descartados logo depois pelo roteamento | **97,6%** |
| Posição do cursor de coleta | **15 horas** atrás |
| Velocidade de avanço | **0,47×** o relógio — o atraso crescia ~32 min/hora |
| Status na tela | **Saudável**, atraso 0 s |

A coleta gastava o orçamento de cada ciclo transportando eventos que a regra de roteamento jogava fora em seguida. O teto estava do lado errado do funil.

:::danger[Um atraso que cresce nunca se resolve sozinho]
A 0,47× o relógio, o coletor **perde 32 minutos a cada hora**. Não adianta esperar: sem intervenção, o atraso de 15 horas seria de 24 horas no dia seguinte. Se o passo 2 mostrar velocidade abaixo de 1×, trate como incidente, não como fila que drena.
:::

## Passo 1: confirme que é atraso de dado, não de coleta

Abra **Normalização → Saúde do Pipeline** e olhe os dois números do card:

| Última coleta | Atraso dos dados | Leitura |
|---|---|---|
| Segundos | Segundos ou minutos | Tudo em dia. O atraso relatado está em outro lugar — veja [Latência alta](./slo-burn.md). |
| Segundos | **Horas** | **É este runbook.** A coleta roda e não vence o volume. |
| Minutos ou mais | Qualquer | A coleta parou ou falha. Veja [Integração ativa não está coletando](./scheduler-stuck.md). |
| Segundos | a linha não aparece | Não medível para essa fonte (a posição de coleta não é uma data). Siga pelo passo 2 mesmo assim, pela etiqueta **Backlog**. |

:::warning[Última coleta recente não é atestado de saúde]
"Última coleta" mede **quando a plataforma falou com o fornecedor pela última vez sem erro**. Um ciclo que buscou eventos de ontem e terminou bem zera esse número do mesmo jeito. Foi exatamente por isso que um coletor 15 horas atrasado passou semanas marcando **0 s / Saudável**.
:::

## Passo 2: confirme o acúmulo e meça a velocidade

Um **Atraso dos dados** alto, sozinho, **não prova acúmulo**: um fluxo sem eventos no período mantém a posição de coleta parada de propósito, e isso é normal. O que prova acúmulo é a combinação com o teto de ciclo.

1. Veja se o card marca **Backlog**. Marcado = o último ciclo terminou no teto, ou seja, sobrou fila.
2. Anote o **Atraso dos dados** e repita a leitura **uma hora depois**:
   - **Diminuiu** → o coletor está recuperando. Estime o tempo restante e decida se dá para esperar.
   - **Igual** → o coletor empata com a origem. Nunca vai alcançar sozinho.
   - **Aumentou** → o coletor está perdendo terreno. Vá para o passo 3 agora.
3. Se você tem acesso às métricas, confirme pelo mesmo par:
   - `collector_watermark_lag_seconds` — a curva do atraso real; é a inclinação dela que importa, não o valor.
   - `increase(collector_cycles_skipped_locked_total[1h])` — ciclos pulados porque o anterior ainda rodava. Consistentemente maior que zero significa que **o ciclo passou a durar mais que o intervalo agendado**, que é a definição operacional de "não dá conta".

:::note[Ciclos pulados são proteção, não perda]
Quando um ciclo demora mais que o intervalo agendado, o seguinte é **pulado**. Isso é correto: o ciclo em andamento avança a posição de coleta e o próximo retoma de onde ele parar. Antes dessa trava, dois ou três ciclos rodavam ao mesmo tempo sobre a **mesma** posição, buscando **os mesmos eventos** — trabalho duplicado que não avançava nada e ainda pressionava a fonte, deixando cada ciclo mais lento.
:::

## Passo 3: descubra quanto do volume é descartável

Esta é a pergunta que decide o resto. Se a maior parte do que está sendo coletado é descartada logo depois, você não tem um problema de capacidade — tem um problema de **onde o descarte acontece**.

1. Abra **Operação → Roteamento** e leia as regras que se aplicam a essa integração.
2. Identifique as que **descartam** por severidade, tipo ou fornecedor.
3. Estime a proporção: se uma regra descarta tudo abaixo de determinada severidade, essa é a fatia que está sendo transportada à toa. No incidente, eram 97,6%.

O que descobrir aqui aponta o caminho:

| Descobriu | Caminho |
|---|---|
| A maior parte do volume é descartada pelo roteamento | **Passo 4** — filtro de coleta. É a alavanca certa e a mais rápida. |
| Quase tudo que é coletado é entregue | O gargalo é capacidade, não desperdício. Acione o administrador da plataforma para avaliar o dimensionamento dos coletores. |
| A origem teve um pico pontual (importação, varredura em massa) | Pode ser só esperar — desde que o passo 2 mostre o atraso **diminuindo**. |

## Passo 4: ligue o filtro de coleta

Empurre o corte que o roteamento já faz para dentro da consulta ao fornecedor. O teto de cada ciclo passa a ser gasto com eventos que serão de fato entregues.

O procedimento, a tabela de níveis do Wazuh e as ressalvas de fidelidade estão em **[Filtro de coleta](../pipelines/collection-filters.md)**. Em resumo:

1. **Visão geral → Integrações**, abra a integração para editar, seção **Filtros de coleta**.
2. Configure o filtro espelhando a regra de descarte que você já tem (para o Wazuh: nível mínimo **7** equivale a descartar severidade Informativo e Baixo).
3. Salve. Vale a partir do próximo ciclo.
4. Volte à Saúde do Pipeline e acompanhe a etiqueta **Backlog**: ela sumir é o sinal de que a coleta alcançou o presente.

:::warning[Depois de ligar o filtro, pare de medir sucesso pelo Atraso dos dados]
Ele cai enquanto o acúmulo drena — e depois **para, ou volta a subir**, mesmo com a coleta em dia. A posição de coleta avança até o último evento **que passou no filtro**, e um ciclo sem nenhum evento acima do corte mantém a posição parada de propósito. Um filtro agressivo produz justamente esse fluxo esparso.

Por isso o critério passa a ser a etiqueta **Backlog**: ela aparece quando o último ciclo terminou no teto, ou seja, quando **sobrou trabalho para o ciclo seguinte**. Ela sumir é o sinal de que a coleta alcançou o presente.

Repare que são duas coisas diferentes na mesma tela: a **etiqueta** reflete só o teto; o **card ficar amarelo** exige o par — teto atingido **e** atraso acima de 30 minutos no mesmo fluxo. Um fluxo pode mostrar a etiqueta sem o card amarelo (acúmulo pequeno, sendo drenado) e pode ter atraso alto **sem** a etiqueta (fonte silenciosa, não coleta atrasada). Detalhes em [Filtro de coleta](../pipelines/collection-filters.md#confira-se-surtiu-efeito).
:::

:::warning[O filtro não recupera o passado e não devolve o que pular]
O filtro age nas consultas seguintes. O acúmulo que já existe **continua tendo de ser drenado** — o filtro só faz a drenagem ser muito mais rápida, porque cada ciclo passa a trazer só o que interessa.

E o que for filtrado **nunca entra na plataforma**: não aparece na captura ao vivo, não gera campo novo no Drift Explorer e não fica disponível para uma rota futura. Os eventos continuam no Wazuh de origem. Leia a seção de fidelidade em [Filtro de coleta](../pipelines/collection-filters.md) antes de ligar.
:::

## Passo 5 (emergência): pular o backlog

Reservado para quando o atraso é grande demais para drenar e **ver o presente vale mais que recuperar o passado** — por exemplo, um plantão cego para o que está acontecendo agora.

**Existe botão para isso — não mexa no banco nem no Redis.** Em **Operação → Collectors**, localize a linha daquela integração e daquele fluxo, clique em **Reset** e confirme em **Zerar cursor?**. É uma ação de **administrador da plataforma**; para os demais perfis a chamada é recusada.

O botão apaga a posição de coleta nos **dois** lugares onde ela vive — o banco (fonte da verdade, usada no boot frio) e o Redis (caminho rápido, lido a cada ciclo) — numa única operação. É exatamente o par que precisa cair junto: um ciclo que ainda encontrasse a posição antiga no Redis a regravaria no banco por cima, e o reset teria sido inócuo.

:::danger[O Reset apaga mais do que a posição de coleta]
Ele remove a **linha inteira** de estado daquele `(integração, fluxo)`, não só o cursor. Vão junto o **total acumulado de eventos coletados**, o horário da **última coleta bem-sucedida**, o **último erro** e a contagem de **falhas consecutivas**.

Enquanto o próximo ciclo não gravar a linha de novo, o fluxo **desaparece da tela de Collectors** e, se for o único fluxo daquela integração, o card em Saúde do Pipeline volta a **Desconhecido** — o estado de "nunca coletou". Anote antes de clicar os números que você pretende comparar depois.
:::

:::warning[Ele não salta para o presente — recomeça no lookback padrão do fornecedor]
A recoleta **não** parte de "agora". Sem posição salva, o coletor recomeça do ponto padrão daquele fornecedor: no Wazuh (detecções), **1 hora atrás**.

Duas consequências:

- **O que fica para trás é tudo entre a posição antiga e essa 1 hora** — esse intervalo deixa de ser coletado.
- **A última hora é lida de novo.** Se a posição já estava perto do presente, essa hora já tinha sido entregue: a deduplicação absorve as repetições enquanto o evento estiver dentro da janela de dedupe configurada, mas alguns podem ser reenviados ao destino. No cenário deste runbook — coletor horas atrás — não há sobreposição: aquela hora ainda não tinha sido coletada.
:::

:::note[Se o Redis estiver fora do ar na hora, o reset "sucede" sem efeito]
A limpeza do Redis é **best-effort**: se ele estiver indisponível naquele instante, a API ainda responde sucesso e a tela ainda mostra **Cursor zerado**, mas a posição antiga sobrevive no caminho quente e o ciclo seguinte retoma de onde estava.

O mesmo vale para um ciclo que já estava em andamento quando você clicou: ao terminar, ele grava a própria posição por cima. O reset não interrompe ciclo em curso.

Como saber: depois do próximo ciclo, o **Atraso dos dados** tem de ter caído para no máximo cerca de uma hora. Se continuar nas horas anteriores, o reset não pegou — repita, de preferência logo após um ciclo terminar.
:::

Depois de pular:

1. Confirme na Saúde do Pipeline que o **Atraso dos dados** caiu para **cerca de uma hora ou menos** (e não para zero — veja acima de onde a recoleta recomeça).
2. **Ligue o filtro de coleta** (passo 4) antes de qualquer outra coisa. Sem isso, o acúmulo volta a se formar pelo mesmo motivo e você terá de pular de novo.
3. Se o período pulado for necessário depois, ele só volta por **[backfill](../pipelines/backfill.md) com o filtro desligado** — e o backfill de um intervalo de horas em alto volume não é rápido. Planeje-o para fora do pico.

## Como prevenir

- **Inclua o Atraso dos dados na ronda matinal, sempre lido junto da etiqueta Backlog.** É o único número que denuncia um coletor que roda sem erro e está no passado — mas sozinho ele não distingue isso de uma fonte simplesmente silenciosa, e num fluxo com filtro de coleta o silêncio é o normal. Veja [Saúde do Pipeline](../operations/pipeline-health.md).
- **Sempre que criar uma regra de roteamento que descarta muito volume, pergunte se o corte cabe na coleta.** Descartar no roteamento economiza entrega; filtrar na coleta economiza transporte. Se o descarte é grande, a economia certa é a segunda.
- **Alerte fora da plataforma** em `collector_watermark_lag_seconds` (por exemplo, acima de 1800 s por 15 minutos) **em conjunto** com `collector_cycles_skipped_locked_total` — nunca no primeiro sozinho, que dispara em qualquer fluxo esparso. Veja [Observabilidade](../operations/observability.md).
- **Depois de conectar uma fonte nova de alto volume**, confira o Atraso dos dados nas primeiras 24 horas: é quando o descompasso entre volume e teto de ciclo aparece.

## Próximos passos

- **[Filtro de coleta](../pipelines/collection-filters.md)** — a alavanca que resolve o caso comum.
- **[Saúde do Pipeline](../operations/pipeline-health.md)** — o que cada indicador do card mede.
- **[Integração ativa não está coletando](./scheduler-stuck.md)** — quando a coleta parou de verdade.
- **[Latência alta](./slo-burn.md)** — quando o gargalo está na entrega ao destino.
