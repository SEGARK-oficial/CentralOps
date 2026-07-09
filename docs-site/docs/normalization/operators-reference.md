---
sidebar_position: 4
title: Referência de operadores de mapeamento
description: Catálogo dos operadores disponíveis no editor de mapeamento — o que cada um faz, quando usar e o que acontece quando uma conversão falha.
---

# Referência de operadores de mapeamento

Operadores são as transformações que você aplica a cada campo ao montar um mapeamento de normalização. Eles convertem o valor que o fornecedor envia (uma data em texto, um "true"/"false", uma pontuação de 0 a 1) no formato que o CentralOps espera para busca e correlação.

Você usa estes operadores no **editor de mapeamento**, dentro do menu **Normalização -> Mappings**, ao definir como cada campo de origem vira um campo normalizado.

## Quando usar

- **Onboarding de um novo fornecedor.** Você adicionou uma integração e os eventos chegam com datas em texto, severidade como string e pontuações em escala diferente. Use os operadores de conversão para colocar tudo no padrão antes de salvar o mapeamento.
- **Eventos caindo na fila de quarentena.** Em **Normalização -> Quarentena** você vê eventos rejeitados porque um campo não pôde ser convertido (ex.: uma data inválida). Esta página ajuda a identificar qual operador estava envolvido e como ajustar a regra.
- **Refino de um mapeamento existente.** Os alertas de um fornecedor estão com texto em caixa mista, espaços sobrando ou listas com itens repetidos, atrapalhando filtros e agrupamentos. Use operadores como minúscula, remover espaços ou remover duplicados para deixar os dados consistentes.

## O que acontece quando uma conversão falha

Quando um operador não consegue converter um valor (por exemplo, uma data em formato inválido ou um texto onde se esperava um número), o evento é enviado para a **fila de quarentena** com o motivo da falha. Ele não é descartado: você pode revisar e reprocessar esses eventos em **Normalização -> Quarentena** depois de corrigir o mapeamento.

Ao longo desta página, "vai para quarentena" significa exatamente isso — o evento fica retido e visível na tela de Quarentena até você ajustar a regra e reprocessar.

:::tip Como testar antes de salvar
O editor de mapeamento permite testar o resultado de uma regra contra eventos de exemplo antes de publicar. Use esse teste para confirmar que a conversão produz o valor esperado e não manda eventos para quarentena.
:::

---

## Operadores de conversão de valor

Convertem um valor de um tipo para outro (texto, número, data, lista, sim/não). São a categoria mais usada no dia a dia.

### Data em texto -> data interna (`iso_to_epoch`)

Converte uma data/hora em texto (formato ISO-8601, como `2026-04-27T12:30:00Z`) para o formato de horário interno usado pelo CentralOps.

| Situação | Resultado |
|----------|-----------|
| `2026-04-27T12:30:00Z` | convertido para o horário interno correspondente |
| Data com fuso, ex. `...+05:00` | ajustado corretamente para o fuso informado |
| Valor já no formato interno | mantido como está |
| Campo vazio | vai para quarentena (defina um valor padrão antes da conversão para tratar ausência) |

**Atenção ao fuso horário.** Se o fornecedor envia a data sem fuso (ex.: `2026-04-27T12:30:00`), o CentralOps assume UTC. Se o fornecedor opera em outro fuso, alinhe isso com o administrador da plataforma para evitar horários deslocados.

### Data interna -> data em texto (`epoch_to_iso`)

Faz o caminho inverso: converte o horário interno de volta para texto ISO-8601 com sufixo `Z`.

| Situação | Resultado |
|----------|-----------|
| Horário interno | `2026-04-27T12:30:00Z` |
| Campo vazio | vai para quarentena |

Se o fornecedor envia um horário em texto que não pode ser interpretado (ex.: `not-a-number`), o evento vai para quarentena.

### Qualquer valor -> texto (`to_str`)

Converte qualquer valor preenchido em texto. Útil quando um campo numérico precisa virar texto para busca ou exibição.

| Situação | Resultado |
|----------|-----------|
| Número `42` | `"42"` |
| Lista `[1, 2, 3]` | `"[1, 2, 3]"` |
| Campo vazio | vai para quarentena |

### Valor -> número inteiro (`to_int`)

Converte texto ou número decimal em número inteiro.

| Situação | Resultado |
|----------|-----------|
| Texto `"42"` | `42` |
| Decimal `42.7` | `42` (corta a parte decimal, não arredonda) |
| Já inteiro | mantido |
| Texto sem número, ex. `"not-a-number"` | vai para quarentena |

**Atenção.** Um campo do tipo sim/não não pode ser convertido diretamente em inteiro. Se precisar transformar sim/não em `1`/`0`, use o mapa de valores da própria regra (associando, por exemplo, `true` a `1` e `false` a `0`).

### Valor -> sim/não (`to_bool`)

Converte texto ou número em um valor verdadeiro/falso.

| Entrada | Resultado |
|---------|-----------|
| `true`, `1`, `yes`, `y` (sem distinção de maiúsculas) | verdadeiro |
| `false`, `0`, `no`, `n`, vazio | falso |
| Número diferente de zero | verdadeiro |
| Número `0` | falso |
| Texto ambíguo, ex. `"maybe"` | vai para quarentena |

### Pontuação 0–1 -> percentual 0–100 (`score_to_percent`)

Converte uma pontuação na escala de 0 a 1 (ex.: confiança de um alerta) para um percentual de 0 a 100.

| Situação | Resultado |
|----------|-----------|
| `0.75` | `75` |
| Valor já entre 0 e 100 | mantido |
| Valor fora do intervalo, ex. `150` | vai para quarentena |
| Campo vazio | permanece vazio (sem erro) |

**Atenção.** Verifique primeiro a escala do fornecedor. Se ele já envia a pontuação de 0 a 100, não aplique esta conversão — ela espera valores entre 0 e 1 e rejeitaria o valor.

### Texto em minúsculas (`lowercase`) e maiúsculas (`uppercase`)

Padronizam o caixa do texto. Muito úteis para deixar buscas e agrupamentos consistentes (ex.: `HTTP`, `Http` e `http` viram um único valor).

| Operador | `HTTP` vira | Campo vazio |
|----------|-------------|-------------|
| Minúsculas | `http` | permanece vazio |
| Maiúsculas | `HTTP` | permanece vazio |

Aplicar a um valor que não é texto (um número, por exemplo) envia o evento para quarentena.

### Remover espaços nas pontas (`trim`)

Remove espaços em branco no início e no fim do texto. Espaços internos não são afetados.

| Situação | Resultado |
|----------|-----------|
| `"  exemplo  "` | `"exemplo"` |
| Campo vazio | permanece vazio |

### Envolver em lista (`to_array`)

Transforma um valor único em uma lista de um item. Se o valor já é uma lista, mantém como está. Útil para campos OCSF que sempre esperam uma lista.

| Situação | Resultado |
|----------|-----------|
| `"exemplo"` | `["exemplo"]` |
| `["a", "b"]` | `["a", "b"]` |
| Campo vazio | lista vazia `[]` |

### Remover duplicados (`dedup`)

Remove itens repetidos de uma lista, preservando a ordem da primeira ocorrência.

| Situação | Resultado |
|----------|-----------|
| `[1, 2, 1, 3]` | `[1, 2, 3]` |
| Lista vazia | lista vazia |
| Campo vazio | permanece vazio |

**Atenção.** Para listas de valores simples (textos, números) a remoção funciona normalmente. Para listas de itens compostos, prefira a regra de construção de lista (descrita mais abaixo), que oferece controle fino sobre por qual campo deduplicar.

### Táticas MITRE do Sophos -> formato OCSF (`mitre_tactic_to_ocsf`)

Converte as táticas MITRE no formato que a integração Sophos envia para o formato OCSF usado pelo CentralOps.

| Situação | Resultado |
|----------|-----------|
| Táticas no formato Sophos | convertidas para o formato OCSF |
| Itens sem tática preenchida | descartados silenciosamente |
| Itens incompletos (faltando identificador ou nome) | vai para quarentena |
| Campo vazio | permanece vazio |

**Atenção.** Este operador é específico do formato da integração Sophos. Se outro fornecedor envia táticas MITRE em formato diferente, ele não se aplica — fale com o administrador da plataforma sobre o suporte ao formato desse fornecedor.

---

## Operadores de condição

Permitem aplicar uma regra apenas quando uma condição é satisfeita. Se a condição for falsa, a regra é ignorada para aquele evento. Use-os quando um campo só deve ser preenchido em certos casos (ex.: definir a direção do tráfego apenas quando o fornecedor informa que é de saída).

### Campo existe (`exists`)

Verdadeiro quando o campo de origem está preenchido (não vazio). Útil para só copiar um valor quando ele realmente vem no evento.

- Lista ou objeto vazio contam como "existe".
- Campo ausente ou vazio conta como "não existe".

### Campo é igual a (`equals`)

Verdadeiro quando o campo de origem é exatamente igual a um valor que você define.

- A comparação distingue tipo e maiúsculas: `"Outbound"` é diferente de `"outbound"`, e o número `3` é diferente do texto `"3"`.
- Se o campo está vazio, a condição é falsa.

### Campo está em uma lista (`in`)

Verdadeiro quando o valor do campo é um dos valores de uma lista que você define (ex.: severidade `critical` ou `high`).

- A ordem dos valores na lista não importa.
- Se o campo está vazio, a condição é falsa.
- A lista de valores é fixa, definida na própria regra.

### Negação (`not`)

Inverte uma condição: a regra se aplica quando a condição interna é falsa. Por exemplo, "preencher a direção do tráfego apenas quando ela não for `unknown`".

Evite encadear negações dentro de negações — fica difícil de ler. Prefira reescrever a condição de forma direta.

---

## Operador de pré-processamento

Roda uma vez por evento, antes das demais regras, para preparar dados que estão "embrulhados" dentro de um campo.

### Interpretar texto JSON (`json_parse`)

Alguns fornecedores enviam parte do alerta como um texto JSON dentro de um único campo. Este operador interpreta esse texto e expõe os campos internos para que você possa mapeá-los normalmente nas regras seguintes.

Ao configurar, você indica:

| Campo | Para que serve |
|-------|----------------|
| Campo de origem | de onde extrair o texto JSON dentro do evento bruto |
| Destino | onde guardar o resultado interpretado, para uso nas regras seguintes |
| Modo tolerante | quando ligado, textos com erro são ignorados em silêncio; quando desligado, um texto inválido manda o evento para quarentena |

**Comportamento:**

- Campo vazio na origem: nada é interpretado, segue vazio.
- Texto JSON válido: os campos internos ficam disponíveis para as regras.
- Texto malformado: vai para quarentena (ou é ignorado, se o modo tolerante estiver ligado).
- Eventos com payload JSON muito grande são rejeitados automaticamente por proteção da plataforma, mesmo no modo tolerante. Esse limite é definido pela equipe de infraestrutura no momento do deploy. Se precisar alterá-lo, fale com o administrador da plataforma.

**Recomendações:**

- Ative o modo tolerante para fornecedores com problemas ocasionais de codificação, para não quarentenar eventos por causa de um registro isolado.
- Se você usa mais de um pré-processamento no mesmo mapeamento, dê destinos diferentes a cada um para evitar conflito.

---

## Tipos de regra

Além dos operadores acima, uma regra pode ser de um destes tipos.

### Regra simples (valor único)

É o tipo padrão. Preenche um campo normalizado com um único valor (texto, número, data, sim/não), opcionalmente aplicando uma conversão.

### Regra de construção de lista (`array_builder`)

Tipo especial para montar listas de observáveis OCSF (endereços IP, e-mails, hashes etc.) a partir de várias origens em uma única regra.

Em cada item da lista você define o nome e o tipo do observável, de onde extrair o valor e, opcionalmente:

- **Ignorar vazios** — não inclui itens cujo valor de origem está vazio (ligado por padrão).
- **Expandir** — quando a origem é uma lista, gera um observável por elemento.
- **Remover duplicados por campo** — evita observáveis repetidos.

Itens desse tipo de regra nunca mandam o evento para quarentena por valor ausente: valores vazios simplesmente não entram na lista.

Para um passo a passo completo, veja a [receita de construção de observáveis no cookbook](cookbook.md).

---

## Como encontrar o operador certo

| Você precisa... | Use | Categoria |
|-----------------|-----|-----------|
| Corrigir/converter data em texto para o horário interno | `iso_to_epoch` | conversão |
| Converter horário interno para texto ISO | `epoch_to_iso` | conversão |
| Transformar número em texto para busca | `to_str` | conversão |
| Garantir que o valor é um número inteiro | `to_int` | conversão |
| Interpretar `true`/`false` como sim/não | `to_bool` | conversão |
| Converter pontuação 0–1 para percentual 0–100 | `score_to_percent` | conversão |
| Padronizar maiúsculas/minúsculas para busca | `lowercase` / `uppercase` | conversão |
| Remover espaços nas pontas | `trim` | conversão |
| Envolver um valor único em lista | `to_array` | conversão |
| Remover itens repetidos de uma lista | `dedup` | conversão |
| Converter táticas MITRE da Sophos para OCSF | `mitre_tactic_to_ocsf` | conversão |
| Aplicar a regra só se o campo existir | `exists` | condição |
| Aplicar a regra só se o campo for igual a um valor | `equals` | condição |
| Aplicar a regra só se o campo estiver em uma lista | `in` | condição |
| Aplicar a regra só se uma condição NÃO for satisfeita | `not` | condição |
| Interpretar um texto JSON embutido no evento | `json_parse` | pré-processamento |
| Montar uma lista de observáveis OCSF | `array_builder` | tipo de regra |
