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

## Modos de avaliação: batch vs inflight

Uma regra pode rodar em dois modos distintos, com diferenças críticas de latência e semântica.

### Modo batch (padrão)

Você define a regra com `eval_mode='batch'`. Uma regra habilitada é avaliada **somente quando uma busca federada termina** (status `finished` ou `partial`), e **somente sobre os resultados daquela busca**. Não existe gatilho contínuo:

- O pipeline de coleta **não** avalia regras de correlação. Eventos ingeridos não passam pelo motor.
- **Não** há agendamento próprio (nenhuma entrada de scheduler cria buscas para alimentar as regras).
- Rodar uma busca federada é a única forma de disparar a avaliação — via **Operação → Busca federada**.

Consequência prática: o alcance temporal da sua regra é o alcance da busca. Uma regra de "10 falhas em 5 minutos" não vê nada que esteja fora do intervalo e dos filtros da busca que a acionou.

:::caution[Beta — avaliação sob demanda, não contínua]
Batch está em **Beta** pelo motivo acima: você precisa executar uma busca federada para a regra rodar. Não é monitoramento contínuo.
:::

### Modo inflight (avaliação em voo)

Você define a regra com `eval_mode='inflight'`. A regra é avaliada **por evento, no pipeline de ingestão, antes do dado chegar ao SIEM**. Uma Detection é emitida imediatamente quando o evento bate na regra — sem esperar por busca federada.

Vantagens: latência em tempo real, não depende de buscas manuais.

Limitações importantes:
- **Sem janela de tempo.** Um inflight roda sobre um único evento por vez, logo `window_seconds` e `timestamp_field` são ignorados. Agrupar por `group_by_field` (ex.: `source.ip`) tira vários eventos do mesmo IP no mesmo ciclo de coleta, mas não numa janela temporal no sentido de "últimas 5 minutos" — é "neste ciclo de coleta".
- **Sem contagem de threshold.** Não existe `min_count`. Cada evento casado gera uma Detection (ou é descartado por supressão de dedup).
- **Operadores diferentes.** Inflight suporta três operadores extras: `in`, `nin`, `exists`. Batch não tem esses.
- **Cardinalidade limitada.** O máximo de chaves de dedup por regra por ciclo é 50. Se uma regra gera mais de 50 variações (ex.: 100 IPs diferentes num ciclo), os matches seguem contados mas nenhuma Detection nova é criada após a 50ª chave.

**Resumo:**

| Aspecto | Batch | Inflight |
|--------|-------|----------|
| **Gatilho** | Fim de busca federada | Cada evento no pipeline |
| **Latência** | Delay até próxima busca | Tempo real |
| **Janela** | Sim, `window_seconds` | Não, ignorado |
| **Min. count** | Sim, `min_count` | Não, ignorado (1 por evento) |
| **Group by** | Agrupa dentro da busca | Agrupa dentro do ciclo de coleta |
| **Operadores** | eq, ne, contains, gt/lt/gte/lte | +in, nin, exists |
| **Uso típico** | Investigação pós-coleta | Detecção urgente em tempo real |

## Autoria de regras — operadores e gotchas

### Operadores de filtro

O campo `where_json` aceita um array de cláusulas, cada uma com formato:
```json
{ "field": "nome.do.campo", "op": "operador", "value": valor }
```

Operadores suportados:

| Operador | Significado | Exemplo | Notas |
|----------|-------------|---------|-------|
| `eq` | Igual | `{ "field": "event.type", "op": "eq", "value": "auth_failed" }` | Padrão, pode omitir `op`. Compara strings; case-sensitive. |
| `ne` | Diferente | `{ "field": "user.name", "op": "ne", "value": "admin" }` | Vacuidade: campo ausente ≠ admin. Auto-injeta `exists=true`. |
| `contains` | Contém (substring) | `{ "field": "message", "op": "contains", "value": "error" }` | Busca a string dentro da string do evento. |
| `gt`, `lt`, `gte`, `lte` | Maior, menor, etc. | `{ "field": "severity", "op": "gte", "value": 3 }` | Coagem lado esquerdo a float. "5" vs 3 funciona. |
| `in` (inflight) | Está em lista | `{ "field": "source.ip", "op": "in", "value": ["10.0.0.1", "10.0.0.2"] }` | **JSON array, não CSV string.** Rejeita `"10.0.0.1,10.0.0.2"`. |
| `nin` (inflight) | Não está em lista | `{ "field": "source.ip", "op": "nin", "value": ["10.0.0.1"] }` | Vacuidade + allowlist. Auto-injeta `exists=true`. |
| `exists` (inflight) | Campo existe | `{ "field": "user.id", "op": "exists", "value": true }` | True/false. Raro usar manualmente (auto-injetado). |

### Gotchas e armadilhas

**in / nin exigem LISTA JSON**

Errado:
```json
{ "field": "source.ip", "op": "in", "value": "10.0.0.1,10.0.0.2" }
```
Isto é rejeitado na compilação (`bad_json`). Se você quer múltiplos valores, use um array:
```json
{ "field": "source.ip", "op": "in", "value": ["10.0.0.1", "10.0.0.2"] }
```

**Operadores negativos (ne, nin) casam por vacuidade — mas é seguro**

Se um evento não tem o campo `user.name`, tanto `ne` quanto `nin` o deixam passar. Sem proteção, uma regra "não-admin" dispararia sobre eventos sem ID. O compilador fecha isto automaticamente injetando `exists=true` — você não vê nem precisa escrever manualmente. É transparente.

**Comparação numérica é automática, mas case-sensitive**

```json
{ "field": "severity", "op": "gte", "value": 4 }
```

Se o evento tem `"severity": "5"` (string), isto funciona — o motor coage `"5"` a `5.0` antes de comparar. Mas:

```json
{ "field": "event_id", "op": "eq", "value": 1 }
```

Se o evento tem `"event_id": "1"` (string), isto **não** funciona — `eq` compara strings e `"1"` ≠ `1`. Use `"1"` como valor.

**Caminhos de campo usando ponto**

```json
{ "field": "raw.user.name", "op": "eq", "value": "alice" }
```

Navega `raw` → `user` → `name`. Se em algum nível não for dict, resolve para `None` (e `eq` não casa).

**Arrays não são navegados**

Se `raw.events` é uma lista, um path como `raw.events.0.type` não funciona — resolve para `None`. O motor não sabe entrar em arrays. Agrupe por um campo de topo, não por dentro de listas.

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

## Diagnóstico: por que a regra não dispara

Sua regra está criada e habilitada, mas não gera Detections. Aqui estão as causas reais, em ordem de frequência.

### 1. Eval_mode errado

Você criou a regra em modo `batch`, mas esperava que ela rodasse em tempo real. Ou criou em `inflight` e esperava que ela rodasse ao fim da busca federada.

**Diagnóstico:** abra a regra. Confira o campo `eval_mode` (pode estar em abas ou settings). Mude conforme necessário.

- Batch roda somente após uma busca federada terminar (você precisa executar uma busca manualmente).
- Inflight roda no pipeline de coleta, por evento, em tempo real.

### 2. Regra desabilitada

O `Status` da regra é "Desativada" (cinza). Regras desativadas não são avaliadas.

**Diagnóstico:** na lista de regras, procure pelo ícone de status. Se estiver cinza, clique em ações → **Ativar**.

### 3. Modo batch: nenhuma busca executada

A regra está em batch, mas você nunca rodou uma busca federada que pudesse acioná-la.

**Diagnóstico:** vá em **Operação → Busca federada**, execute uma busca que cubra as fontes e período de interesse, e aguarde conclusão. A regra é avaliada ao final.

### 4. Where_json não compila

A cláusula `where` tem um erro de sintaxe ou semântica que a torna inválida. A regra é rejeitada no boot.

Razões possíveis (procure no **log de aplicação** pela mensagem de rejeição):
- `bad_json`: JSON malformado, ou operador numérico (gt/lt/gte/lte) com valor não-numérico, ou `in`/`nin` sem array, ou campo `field` ausente.
- `empty_where`: array vazio ou sem nenhuma cláusula (em inflight, regra vazia dispararia 100% dos eventos).
- `unknown_op`: operador não reconhecido (ex.: `"op": "match"`, que não existe).
- `over_cap`: mais de 10 cláusulas numa regra (limite inflight).

**Diagnóstico:** abra a regra, valide o JSON — use um validador de JSON online se necessário. Confira operadores: são apenas `eq`, `ne`, `contains`, `gt`, `lt`, `gte`, `lte` + `in`, `nin`, `exists` (inflight).

### 5. Modo batch: campo de timestamp vazio ou inválido

Você configurou uma janela (`window_seconds > 0`), mas deixou `timestamp_field` vazio ou apontando para um campo que não existe.

**Diagnóstico:** abra a regra. Confira `timestamp_field` — está preenchido? Se sim, ele existe de fato nos eventos daquela fonte? Execute uma investigação / busca federada para ver um evento de exemplo, verifique o caminho do campo.

Se o campo estiver realmente ausente ou vazio, a **janela é desligada em silêncio** e a regra passa a contar todos os eventos do grupo presentes no resultado da busca, sem recorte temporal — isto gera falso positivo, não falha de disparo, mas é um modo silencioso de "sua regra não faz o que você esperava".

### 6. Campo podado pelo mapeamento (`raw_reduction`)

A fonte é mapeada com um bloco `raw_reduction` que **remove** ou **encurta** o campo que sua regra tenta usar. Note que hoje o campo pode ter sido apagado por inteiro, não apenas truncado.

**Diagnóstico:** o bloco `raw_reduction` **não é editável pelo editor visual de mapeamento** — ele vive na definição JSON e é alterado por quem administra a plataforma. Peça a definição atual do mapeamento e verifique se o campo da sua regra cai em algum spec `drop`, `keep_only`, `max_bytes` ou `drop_nulls`. Se cair, ajuste o mapeamento ou reescreva a regra sobre um campo que sobrevive à poda. Veja [Especificação da DSL](../normalization/dsl-spec.md).

### 7. Path atravessa array

Um campo do seu `where` está dentro de uma lista, ex.: `raw.events.0.type`.

**Diagnóstico:** confira a estrutura dos dados. Se o campo está dentro de um array (ex.: `events` é uma lista), o motor não sabe navegar dentro. Use um campo de topo, ou agrupe por um campo que não requer entrar na lista.

### 8. Group_by_field não resolve (inflight)

Modo inflight: você configurou `group_by_field` apontando para um campo que não existe ou está dentro de um array.

**Diagnóstico:** cada evento casado produz uma Detection usando `group_by_field` como chave. Se o campo não existe no evento, a Detection não é criada — em vez disso, você vê um erro `group_by_unresolved` nos logs. Verifique o campo, ou deixe em branco (NULL) se quer uma Detection por regra por ciclo.

## Limites e roadmap

### Limites gerais

- **Quota por organização**: existe limite de regras por organização. Se receber erro 409 (Conflict), consulte o administrador.
- **Sem filtros complexos**: os filtros `where` suportam operadores simples: `eq`, `ne`, `contains`, `gt`/`lt`/`gte`/`lte`. Para lógica avançada (OR, NOT, wildcards), use a [Busca federada](./search.md) e analise manualmente.

### Limites específicos do inflight

- **50 regras por ciclo**: máximo de regras inflight avaliadas num ciclo de coleta. Se sua organização tiver mais, as que cabem são carregadas e avaliadas; as demais são adiadas para o próximo ciclo. Não há perda de eventos — apenas avaliação em ondas. Em caso de reach o limite, logs avisar.
- **10 cláusulas por regra**: máximo de predicados no `where_json` de uma regra inflight. Uma regra com 11 cláusulas é rejeitada em compile-time.
- **500 cláusulas totais por ciclo**: máx de 50 regras × 10 cláusulas = 500 predicados avaliados por evento. Se você tem 50 regras com 10 cláusulas cada, atinge o teto e nenhuma regra extra pode ser carregada naquele ciclo.
- **50 chaves de dedup por regra por ciclo**: se uma regra gera mais de 50 variações (ex.: 100 IPs diferentes num ciclo usando `group_by_field="source.ip"`), as primeiras 50 geram Detections, as demais não — mas **todos os matches seguem sendo contados nos logs**. Nenhum evento é perdido. Isto costuma indicar que `group_by_field` tem cardinalidade muito alta (ex.: um ID único por evento). Ajuste a regra ou revise o agrupamento.

:::note[O que fazer ao atingir tetos]
- **Muitas regras?** Revise quais são críticas. Priorize pelo risco e desabilite as menos importantes.
- **Muitas cláusulas?** Simplifique a lógica do `where` — combine predicados ou use campos mais específicos.
- **Muitas chaves de dedup?** Revise `group_by_field` — talvez esteja muito granular. Ex.: se agrupa por `user.id` e tem milhares de usuários por ciclo, cada um gera uma chave diferente. Considere agrupar por `user.department` ou deixar em branco (uma Detection por regra).
:::

### Roadmap

- **Batch**: gatilho contínuo no pipeline (inflight já cobre isto). Agendamento próprio (sem precisar rodar busca manual).
- **Tipo**: sequência de eventos (A → B → C), agregações (soma, média), regras SQL custom.
- **Batch: sem `timestamp_field`**: fallback automático para timestamp de ingestão (hoje a janela é desligada em silêncio).

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
