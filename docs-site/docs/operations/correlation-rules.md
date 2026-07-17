---
sidebar_position: 10
title: Regras de correlação
description: Detectar padrões cross-source e disparar alertas quando um padrão atinge um limite em uma janela de tempo
---

# Regras de correlação

Uma **regra de correlação** observa eventos de múltiplas fontes ao mesmo tempo e dispara uma **Detecção** (um alerta de análise) quando um padrão específico acontece. Por exemplo: "se o mesmo IP falhar em fazer login 5 vezes em 5 minutos, abrir uma detecção".

Diferente dos eventos coletados das fontes (que você pesquisa em [Investigações](./search.md)), as Detecções vêm de análise: correlação, busca em tempo real ou agendamentos. Você gerencia aqui regras de **tipo threshold** — o tipo mais comum. Tipos mais avançados (sequência de eventos, agregações) estão no nosso roadmap.

Para acessar, use o menu **Conhecimento → Correlação**.

**Quem pode ver:** todos os perfis com acesso de leitura ou superior. Apenas **Engineer** e acima conseguem criar, editar ou excluir regras.

## Quando usar

- **Detectar ataque em andamento.** ≥5 tentativas de login falhadas do mesmo origem em poucos minutos? Pode ser brute-force. A regra dispara automaticamente e vira uma Detecção que você triagem na tela de [Detecções](./detections.md).
- **Reduzir ruído.** Em vez de alertar cada evento isolado (cada tentativa falhada é um alerta fraco), agrupe e alerte só quando o padrão fica suspeito.
- **Correlação sem query.** Não precisa saber linguagem de busca federada. Preencha um formulário simples: campo para agrupar, limite mínimo, janela de tempo, filtros simples.

## Como funciona: tipo threshold

A regra **threshold** segue este fluxo:

1. **Agrupa** eventos por um campo (ex.: `source.ip`, usando notação de caminho com ponto).
2. **Filtra** (opcional) — apenas eventos que atendem aos critérios da cláusula `where`.
3. **Conta** eventos únicos dentro de uma janela de tempo (ex.: últimos 5 minutos).
4. **Dispara** — quando a contagem ≥ `min_count`, emite uma Detecção.
5. **Suprime** — não dispara novamente pela mesma chave de agrupamento durante a janela de supressão (ex.: 1 hora).

**Exemplo prático:**

| Campo | Valor |
|-------|-------|
| **Nome** | Múltiplas falhas de login do mesmo IP |
| **group_by_field** | `source.ip` |
| **min_count** | 5 |
| **window_seconds** | 300 (5 minutos) |
| **where** | `[{field: "event.category", op: "eq", value: "authentication"}]` |
| **suppression_window_seconds** | 3600 (1 hora) |

Resultado: se 5 ou mais eventos de autenticação vierem do mesmo IP nos últimos 5 minutos, dispara uma Detecção. A mesma regra não dispara novamente para aquele IP pelos próximos 60 minutos.

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
   - **Janela de tempo**: quantos segundos de história examinar (padrão: 300 = 5 min).
   - **Filtros** (opcional): adicione condições. Exemplo: `event.category eq "authentication"` — só conta eventos de autenticação.
   - **Severidade**: nível da Detecção gerada (padrão: High). Escolha entre Low, Medium, High ou Critical.
   - **Supressão**: quantos segundos para silenciar a regra depois que dispara para a mesma chave (padrão: 3600 = 1 hora).

3. Clique **Salvar**. A regra entra em modo ativo.

### Editar uma regra

1. Clique no nome da regra ou no botão de editar.
2. Modifique os campos.
3. Clique **Salvar**.

Se a regra está ativa, as mudanças valem imediatamente.

### Ativar / Desativar

No botão de ações, escolha **Ativar** ou **Desativar**. Regras desativadas não disparam.

### Excluir uma regra

Clique no botão de ações e escolha **Excluir**. A confirmação é pedida antes de remover.

## Detecções geradas

Quando uma regra dispara, é criada uma **Detecção** com:

- **Fonte**: "correlation" (diferente de "scheduled_query" ou "live_query").
- **Severidade**: a que você escolheu ao criar a regra.
- **Status**: começa em "open" — você triagem em [Detecções](./detections.md).
- **Chave de dedup**: inclui a chave de agrupamento, garantindo que eventos do mesmo grupo não geram duplicatas enquanto a supressão estiver ativa.

Você vê as Detecções em **Operação → Detecções**.

## Limites e roadmap

- **Tipo**: atualmente, apenas **threshold** (contagem em janela). **Sequence** (padrão A → B → C) e **Aggregation** (soma, média) estão no roadmap.
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
   - **Filtros**: adicione `event.action eq "ssh_login_failed"`
   - **Severidade**: High
   - **Supressão**: 3600

4. Clique **Salvar**.

A partir daqui, sempre que 10 ou mais falhas de SSH vierem do mesmo IP em 5 minutos, uma Detecção será criada.

### Revisar e triagem de uma Detecção gerada

1. Abra **Operação → Detecções**.
2. Filtre por status "open".
3. Clique em uma Detecção com `source = "correlation"`.
4. No detalhe, veja a regra que a gerou e os eventos que acionaram.
5. Mude o status para "acknowledged" (investigando) ou "closed" (resolvido/falso positivo).

### Ajustar uma regra que gera muitos falsos positivos

1. Abra **Conhecimento → Correlação**.
2. Clique em editar na regra problemática.
3. Aumente `min_count` (ex.: de 5 para 10).
4. Ou aumente `window_seconds` para exigir uma concentração maior em menos tempo.
5. Ou adicione um filtro mais específico na cláusula `where`.
6. Clique **Salvar**.

## Próximos passos

- **Quer triagem as Detecções geradas?** Vá em **Operação → Detecções** ([Detecções](./detections.md)).
- **Precisa de análise mais profunda?** Use **Operação → Busca federada** ([Busca federada](./detections.md)) para consultar múltiplas fontes com queries avançadas.
- **Quer ver todos os eventos?** Vá em **Operação → Investigações** ([Investigações](./search.md)).
