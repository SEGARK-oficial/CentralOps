---
sidebar_position: 3
title: Detecções
description: Alertas de 1ª classe originários de queries e correlações, com triagem e supressão inteligente
---

# Detecções

A tela **Detecções** mostra alertas duráveis gerados por buscas de segurança (scheduled queries e correlações cross-source). Cada alerta aqui é um registro consultável, com status de triagem (aberto, reconhecido ou fechado) e supressão automática de ruído. Diferente dos **Alertas** (que são eventos brutos de coleta), as Detecções são análises de 1ª classe, produzidas pelo motor de IA e regras que você configurar.

Para acessar, use o menu **Operação → Detecções**.

**Quem pode ver:** todos os perfis autenticados, escopado pela organização. Apenas **Operator e superiores** conseguem mudar o status (triagem).

## Quando usar

- **Revisar análises ativas do motor.** Os alertas Critical e High aqui são resultado de correlações ou buscas agendadas — priorize pelas detecções abertas.
- **Triagem: acknowledge e fechar.** Ao investigar um alerta, reconheça-o para avisar a equipe que está sendo tratado; feche quando resolvido.
- **Auditar supressões.** A plataforma agrupa detecções repetidas (mesma origem, mesma janela) em um único alerta com count incrementado — confira o `count` para ver se há spam.
- **Rastrear origens.** Veja se a detecção veio de uma query agendada, busca ao vivo ou regra de correlação. Detecções de correlação muitas vezes apontam padrões multi-fonte.

## Origens de uma detecção

| Origem | O que significa |
|--------|-----------------|
| **Scheduled Query** | Uma busca que roda em intervalo fixo (diária, a cada hora) e emite um alerta quando encontra correspondência. |
| **Live Query** | Uma busca disparada manualmente pelo operador (on-demand) via "Busca Federada" que gerou um alerta. |
| **Correlation** | Uma regra de correlação que avaliou múltiplas fontes simultaneamente e detectou um padrão de risco. |

## Severidade (OCSF)

A severidade segue o padrão OCSF (Open Cybersecurity Schema Framework):

| Nível | Significado |
|-------|-------------|
| **Critical (5)** | Falha de segurança confirmada, ataque em andamento ou perda de controle. Ação imediata. |
| **High (4)** | Padrão de risco bem definido, evidência forte. Investigar nas próximas horas. |
| **Medium (3)** | Comportamento suspeito, mas sem confirmação. Investigar quando houver tempo. |
| **Low (1–2)** | Atividade anômala ou informativa. Útil para trend analysis. |

## Permissões necessárias

| Ação | Quem pode fazer |
|------|-----------------|
| **Ver Detecções** | Todos os perfis autenticados (escopado por organização). |
| **Triagem (Ack/Fechar/Reabrir)** | Operator ou superior (mesma permissão de **QUERY_RUN**: rodar buscas ao vivo). |

:::note Erro 403 ao triagar?
Se receber erro "acesso negado" ao tentar mudar o status de uma detecção, seu perfil é Viewer. Peça a um Operator que marque como reconhecida/fechada, ou solicite promoção a Operator ao administrador.
:::

## A tela

### Filtros disponíveis

| Filtro | Como funciona |
|--------|---------------|
| **Status** | Aberta, Reconhecida (Ack), ou Fechada. Escolha um ou mais para refinar. |
| **Severidade** | Critical, High, Medium, Low. Filtre pelas que mais importam agora. |
| **Origem** | Scheduled Query, Live Query ou Correlation. |
| **Data** | Últimas 24h, 7 dias, 30 dias ou intervalo personalizado. |

### Colunas da lista

| Coluna | O que mostra |
|--------|-------------|
| **Status** | Aberta (●), Reconhecida (◐), Fechada (✓). |
| **Severidade** | Nível de risco, com cor destacada. |
| **Título** | Resumo do que foi detectado (ex.: "5 tentativas falhadas em 1h"). |
| **Origem** | Scheduled Query, Live Query ou Correlation. |
| **Count** | Quantas vezes a mesma detecção disparou na janela de supressão. |
| **Primeira vista** | Quando o primeiro evento do grupo foi registrado. |
| **Última vista** | Quando a detecção mais recente foi adicionada ao grupo. |

## Deduplicação e supressão

Detecções com o **mesmo ``dedup_key``** (origem, campo de chave) dentro da **mesma janela de supressão** (padrão: 3600 segundos = 1 hora) são agrupadas em um único alerta.

**Exemplo:** uma query detecta 5 tentativas falhadas do mesmo usuário em 50 minutos. Em vez de 5 alertas, você vê 1 alerta com `count=5` e `last_seen` atualizado.

Benefício: reduz ruído e evita que você feche o mesmo alerta múltiplas vezes.

## Passo a passo

### Ver detecções abertas do dia

1. No menu, abra **Operação → Detecções**.
2. Filtre **Status** = Aberta.
3. Filtre **Data** = Últimas 24h.
4. Confirme. A lista mostra apenas as detecções abertas recentes.

### Reconhecer uma detecção (Ack)

1. Localize a detecção na lista.
2. Abra o detalhe ou use a ação **Reconhecer**.
3. O status muda para **Reconhecida** — avisa a equipe que você está investigando.

### Fechar uma detecção

1. Quando terminar a investigação, abra o detalhe da detecção.
2. Use a ação **Fechar**.
3. A detecção muda para **Fechada**. Se disparar novamente (fora da janela de supressão), um novo alerta aparece.

### Reabrir uma detecção

1. Se a investigação não resolveu o problema, abra a detecção fechada.
2. Use a ação **Reabrir**.
3. O status volta para **Aberta**.

### Detalhe de uma detecção

Clique no alerta para ver:

- **Severidade** — nível de risco.
- **Status** — Aberta / Reconhecida / Fechada.
- **Origem** — qual query ou correlação gerou.
- **Regra** — nome da regra de correlação (se aplicável).
- **Contagem** — quantas vezes repetiu.
- **Primeira/Última vista** — intervalo do grupo.
- **Dedup key** — chave de deduplicação (interno; útil para debug).

## Casos de uso rápidos

| Pergunta | Como responder |
|----------|----------------|
| Tenho alertas Critical para triagar? | Filtre Severidade = Critical e Status = Aberta. Clique em cada um para investigar. |
| Qual tema está gerando mais alertas? | Filtre por Origem (ex.: Scheduled Query) e olhe a distribuição. |
| Este é falso positivo? | Filtre pela regra/origem e veja se muitos alertas parecidos aparecem repetidamente. Se sim, revise a regra com um Engineer. |
| Como vejo o evento que originou a detecção? | Abra o detalhe. Se houver link a um evento (Search Result), clique para ver os dados brutos. |

## O que esperar (e limites)

- **Apenas triagem aqui.** Esta tela serve para reconhecer e fechar alertas de análise. Não é o lugar para investigar eventos brutos — vá em **Operação → Alertas** ou **Operação → Investigações** para dados completos.
- **Dedup inteligente.** Alertas com o mesmo padrão (dedup_key) em um intervalo curto são agrupados. Após a janela de supressão (padrão 1 hora), um novo alerta da mesma regra é legítimo.
- **Retenção.** Detecções são armazenadas de forma durável. Eventos muito antigos podem não aparecer em buscas rápidas — use as telas de **Investigações** ou os destinos configurados para histórico de longo prazo.

## Próximos passos

- **Investigar a raiz de um alerta?** Vá em **Operação → Investigações** ([Investigações](./search.md)) para buscar os eventos de coleta que geraram a detecção.
- **Ver eventos brutos de segurança?** Vá em **Operação → Alertas** ([Alertas](./alerts.md)).
- **Criar ou editar regras de correlação?** Vá em **Conhecimento → Correlação** (visível apenas a Engineer e acima).
- **Executar uma busca on-demand?** Vá em **Operação → Busca Federada** ([Busca Federada](./search.md)).
