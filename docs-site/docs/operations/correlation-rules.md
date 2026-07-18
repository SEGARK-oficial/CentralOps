---
sidebar_position: 10
title: Regras de correlação
description: Detectar padrões cross-source nos resultados de uma busca federada e abrir uma Detecção quando um padrão atinge um limite
---

# Regras de correlação

:::caution[Beta — avaliação sob demanda, não contínua]
As regras de correlação estão em **Beta**. O motor **não** observa o pipeline de ingestão nem roda em agendamento próprio: uma regra só é avaliada **ao final de uma busca federada** (um QueryJob concluído), sobre os resultados daquela busca. Enquanto ninguém executar uma busca federada, nenhuma regra roda e nenhuma Detecção de correlação é criada. Não use estas regras como sua única cobertura de detecção.
:::

Uma **regra de correlação** examina os resultados de uma busca federada — que podem vir de múltiplas fontes ao mesmo tempo — e abre uma **Detecção** (um alerta de análise) quando um padrão específico aparece naquele conjunto de resultados. Por exemplo: "se o mesmo IP falhar em fazer login 5 vezes em 5 minutos dentro dos resultados desta busca, abrir uma detecção".

Diferente dos eventos coletados das fontes (que você pesquisa em [Investigações](./search.md)), as Detecções vêm de análise: correlação, busca em tempo real ou agendamentos. Você gerencia aqui regras de **tipo threshold** — o tipo mais comum. Tipos mais avançados (sequência de eventos, agregações) estão no nosso roadmap.

Para acessar, use o menu **Conhecimento → Correlação**.

**Quem pode ver:** todos os perfis com acesso de leitura ou superior. Apenas **Engineer** e acima conseguem criar, editar ou excluir regras.

## Quando usar

- **Materializar um padrão que você acabou de buscar.** ≥5 tentativas de login falhadas da mesma origem em poucos minutos? Pode ser brute-force. Ao rodar a busca federada, a regra avalia o resultado e vira uma Detecção que você triagem na tela de [Detecções](./detections.md).
- **Reduzir ruído.** Em vez de alertar cada evento isolado (cada tentativa falhada é um alerta fraco), agrupe e alerte só quando o padrão fica suspeito.
- **Correlação sem query.** Não precisa saber linguagem de busca federada. Preencha um formulário simples: campo para agrupar, limite mínimo, janela de tempo, filtros simples.

## Quando a regra é avaliada

Este é o ponto mais importante de entender antes de criar uma regra.

Uma regra habilitada é avaliada **somente quando uma busca federada termina** (status `finished` ou `partial`), e **somente sobre os resultados daquela busca**. Não existe gatilho contínuo:

- O pipeline de coleta **não** avalia regras de correlação. Eventos ingeridos não passam pelo motor.
- **Não** há agendamento próprio (nenhuma entrada de scheduler cria buscas para alimentar as regras).
- Rodar uma busca federada é a única forma de disparar a avaliação — via **Operação → Busca federada**.

Consequência prática: o alcance temporal da sua regra é o alcance da busca. Uma regra de "10 falhas em 5 minutos" não vê nada que esteja fora do intervalo e dos filtros da busca que a acionou.

## Como funciona: tipo threshold

Ao final de uma busca federada, a regra **threshold** segue este fluxo sobre os resultados retornados:

1. **Agrupa** os eventos do resultado por um campo (ex.: `source.ip`, usando notação de caminho com ponto).
2. **Filtra** (opcional) — apenas eventos que atendem aos critérios da cláusula `where`.
3. **Conta** eventos dentro de uma janela de tempo (ex.: últimos 5 minutos), usando o **campo de timestamp** configurado.
4. **Dispara** — quando a contagem ≥ `min_count`, emite uma Detecção.
5. **Suprime** — não dispara novamente pela mesma chave de agrupamento durante a janela de supressão (ex.: 1 hora).

:::caution[A janela só existe se houver campo de timestamp]
A janela de tempo é aplicada **apenas** quando `window_seconds > 0` **e** `timestamp_field` está preenchido. Se você deixar `timestamp_field` vazio, a janela é **desligada** e a regra passa a contar **todos** os eventos do grupo presentes no resultado da busca, sem qualquer recorte temporal — o que costuma gerar falso positivo. Não existe fallback para "timestamp de ingestão".

Se o campo estiver preenchido mas os valores forem inválidos ou ausentes nos eventos, o comportamento é **fail-closed**: a contagem vai a zero e a regra não dispara (em vez de virar silenciosamente "N em qualquer tempo"). Sempre preencha `timestamp_field` com um campo que exista de fato nos eventos daquela fonte.
:::

**Exemplo prático:**

| Campo | Valor |
|-------|-------|
| **Nome** | Múltiplas falhas de login do mesmo IP |
| **group_by_field** | `source.ip` |
| **min_count** | 5 |
| **window_seconds** | 300 (5 minutos) |
| **timestamp_field** | `event.timestamp` (obrigatório para a janela valer) |
| **where** | `[{field: "event.category", op: "eq", value: "authentication"}]` |
| **suppression_window_seconds** | 3600 (1 hora) |

Resultado: quando uma busca federada terminar, se o resultado contiver 5 ou mais eventos de autenticação do mesmo IP dentro de uma janela de 5 minutos, uma Detecção é aberta. A mesma regra não dispara novamente para aquele IP pelos próximos 60 minutos, mesmo que outras buscas sejam executadas.

## Permissões

| Ação | Permissão | Quem tem |
|------|-----------|----------|
| Ver lista de regras | `query.run` | Viewer, Operator, Engineer, Admin |
| Criar / Editar / Excluir regra | `query.save` | Engineer, Admin |

## A tela

### Lista de regras

Mostra todas as regras da sua organização:

| Coluna | O que mostra |
|--------|------------|
| **Nome** | Identificador da regra (ex.: "Múltiplas falhas de login"). |
| **Status** | Ativa (verde) ou desativada (cinza). Regras desativadas não disparam. |
| **Tipo** | Sempre "threshold" por enquanto. |
| **Grupo** | Campo pelo qual agrupa (ex.: `source.ip`). |
| **Limite** | Número mínimo de eventos para disparar. |
| **Janela** | Período em segundos no qual conta eventos. |
| **Ações** | Editar, desativar/ativar, ou excluir. |

### Criar uma regra

1. Clique em **+ Nova regra**.
2. Preencha o formulário:
   - **Nome**: descrição curta e única (ex.: "Brute-force SSH").
   - **Descrição** (opcional): mais contexto (ex.: "Múltiplas tentativas falhadas em um curto espaço de tempo").
   - **Campo de agrupamento**: qual campo usar para agrupar (ex.: `source.ip`, `user.name`). Use notação com ponto para campos aninhados.
   - **Limite mínimo**: quantos eventos no mínimo para disparar (padrão: 5).
   - **Janela de tempo**: quantos segundos de história examinar dentro do resultado da busca (padrão: 300 = 5 min).
   - **Campo de timestamp**: qual campo do evento datar para aplicar a janela (ex.: `event.timestamp`). **Obrigatório sempre que a janela for maior que zero** — sem ele a janela é desligada e a regra conta todos os eventos do grupo no resultado da busca.
   - **Filtros** (opcional): adicione condições. Exemplo: `event.category eq "authentication"` — só conta eventos de autenticação.
   - **Severidade**: nível da Detecção gerada (padrão: High). Escolha entre Low, Medium, High ou Critical.
   - **Supressão**: quantos segundos para silenciar a regra depois que dispara para a mesma chave (padrão: 3600 = 1 hora).

3. Clique **Salvar**. A regra fica habilitada e passa a ser avaliada na próxima busca federada.

### Editar uma regra

1. Clique no nome da regra ou no botão de editar.
2. Modifique os campos.
3. Clique **Salvar**.

Se a regra está habilitada, as mudanças valem a partir da próxima busca federada — nada é reavaliado retroativamente sobre buscas já concluídas.

### Ativar / Desativar

No botão de ações, escolha **Ativar** ou **Desativar**. Regras desativadas são ignoradas na avaliação.

### Excluir uma regra

Clique no botão de ações e escolha **Excluir**. A confirmação é pedida antes de remover.

## Detecções geradas

Quando uma regra dispara, é criada uma **Detecção** com:

- **Fonte**: "correlation" (diferente de "scheduled_query" ou "live_query").
- **Severidade**: a que você escolheu ao criar a regra.
- **Status**: começa em "open" — você triagem em [Detecções](./detections.md).
- **Chave de dedup**: inclui a chave de agrupamento, garantindo que eventos do mesmo grupo não geram duplicatas enquanto a supressão estiver ativa.
- **Contagem (`count`)**: começa em 1 e é incrementada a cada **nova avaliação da regra** que reincide na mesma chave de dedup. Ela **não** reflete quantos eventos foram correlacionados — para isso, olhe os eventos no detalhe da Detecção. Veja [Detecções](./detections.md#deduplicação-e-supressão).

Você vê as Detecções em **Operação → Detecções**.

## Limites e roadmap

- **Sem gatilho automático (Beta)**: a avaliação acontece apenas ao final de uma busca federada. Gatilho contínuo no pipeline de ingestão e agendamento próprio da regra estão no roadmap.
- **Tipo**: atualmente, apenas **threshold** (contagem em janela). **Sequence** (padrão A → B → C) e **Aggregation** (soma, média) estão no roadmap.
- **Janela depende de `timestamp_field`**: sem esse campo, a janela é desligada e a contagem passa a considerar todos os eventos do grupo no resultado da busca.
- **Sem filtros complexos**: os filtros `where` suportam operadores simples: `eq` (igual), `ne` (diferente), `contains` (contém), `gt`/`lt`/`gte`/`lte` (comparações numéricas). Para lógica muito avançada, use a [Busca federada](./detections.md) e salve como Correlation Rule depois.
- **Quota por organização**: existe limite de regras por organização. Se receber erro 409 (Conflict), consulte o administrador.

## Passo a passo

### Criar uma regra para detectar brute-force

1. Abra **Conhecimento → Correlação**.
2. Clique em **+ Nova regra**.
3. Preencha:
   - **Nome**: "Brute-force SSH"
   - **Descrição**: "Múltiplas conexões SSH falhadas do mesmo IP"
   - **Campo de agrupamento**: `source.ip`
   - **Limite mínimo**: 10
   - **Janela de tempo**: 300 (5 minutos)
   - **Campo de timestamp**: `event.timestamp` (sem ele a janela de 5 minutos não é aplicada)
   - **Filtros**: adicione `event.action eq "ssh_login_failed"`
   - **Severidade**: High
   - **Supressão**: 3600

4. Clique **Salvar**.

5. Abra **Operação → Busca federada** e execute uma busca que cubra as fontes e o período de interesse.

Quando essa busca terminar, a regra é avaliada sobre os resultados dela: se houver 10 ou mais falhas de SSH do mesmo IP dentro de uma janela de 5 minutos, uma Detecção é criada. Repita a busca sempre que quiser reavaliar — a regra não roda sozinha.

### Revisar e triagem de uma Detecção gerada

1. Abra **Operação → Detecções**.
2. Filtre por status "open".
3. Clique em uma Detecção com `source = "correlation"`.
4. No detalhe, veja a regra que a gerou e os eventos que acionaram.
5. Mude o status para "acknowledged" (investigando) ou "closed" (resolvido/falso positivo).

### Ajustar uma regra que gera muitos falsos positivos

1. Abra **Conhecimento → Correlação**.
2. Clique em editar na regra problemática.
3. **Confira primeiro o campo de timestamp.** Se estiver vazio, a janela não está sendo aplicada e a regra conta todos os eventos do grupo no resultado da busca — essa é a causa mais comum de falso positivo. Preencha com um campo que exista nos eventos daquela fonte.
4. Aumente `min_count` (ex.: de 5 para 10).
5. Ou reduza `window_seconds` para exigir uma concentração maior em menos tempo.
6. Ou adicione um filtro mais específico na cláusula `where`.
7. Clique **Salvar**. As mudanças valem a partir da próxima busca federada.

## Próximos passos

- **Quer triagem as Detecções geradas?** Vá em **Operação → Detecções** ([Detecções](./detections.md)).
- **Quer que suas regras rodem?** Execute uma **Operação → Busca federada** ([Busca federada](./detections.md)) — é o que dispara a avaliação das regras habilitadas.
- **Quer ver todos os eventos?** Vá em **Operação → Investigações** ([Investigações](./search.md)).
