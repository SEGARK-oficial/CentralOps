---
sidebar_position: 2
title: Editor de mapeamento — referência de campos
description: O que cada opção do editor de mapeamento faz e quando usar — traduzir campos de um fornecedor para o padrão OCSF direto pela interface.
---

# Editor de mapeamento — referência de campos

O editor de mapeamento é onde você ensina o CentralOps a traduzir os eventos de um fornecedor para o padrão único da plataforma (OCSF). Cada regra que você cria diz: "pegue este campo do evento original e grave neste campo padronizado", aplicando conversões pelo caminho (texto para número, fuso de data, tabelas de tradução, etc.).

Você edita os mapeamentos na interface, sem escrever código de infraestrutura. O caminho é:

- Menu **Normalização -> Mappings** para ver e editar todos os mapeamentos.
- Ou, a partir de um conector específico, em **Visão geral -> Integrações**, abra a integração e vá até a seção de mapeamento dela.

Cada mapeamento tem uma lista de **regras**. Cada regra grava um campo do evento normalizado. Você monta as regras pelo editor e valida o resultado antes de salvar.

## Quando usar

Mexa no editor de mapeamento quando:

- **Um novo fornecedor entrou em produção e os campos chegam "crus".** Você precisa apontar quais campos do produto viram severidade, horário, IP de origem, usuário, etc., para que os alertas apareçam corretos nas telas de Alertas e Investigações.
- **O fornecedor manda severidade como texto e o padrão espera número.** Ex.: o produto envia `high`, `medium`, `low` e o OCSF precisa de `4`, `3`, `2`. Você cria uma tabela de tradução na regra (ver [Tabela de tradução](#tabela-de-tradução-value_map)).
- **Eventos importantes estão indo parar na Quarentena por falta de um campo crítico.** Você marca o campo como obrigatório para garantir que qualquer evento sem ele seja isolado para revisão em vez de passar pela metade (ver [Campo obrigatório](#campo-obrigatório-required)).
- **O fornecedor mudou o nome de um campo ou passou a mandar a mesma informação em lugares diferentes.** Você adiciona fontes alternativas para a mesma regra (ver [Fontes alternativas](#fontes-alternativas-fallback_source)).

## Antes de começar: validar antes de salvar

Sempre que terminar de montar ou ajustar regras, rode a **validação (ensaio/dry-run)** dentro do próprio editor antes de salvar. Ela aplica o mapeamento a eventos de exemplo e mostra, campo a campo, o que cada regra produziria. Use isso para conferir se as severidades, horários e demais campos estão saindo como você espera. Quando estiver satisfeito, salve a nova versão do mapeamento — o histórico de versões fica registrado e pode ser comparado.

---

## Estrutura de uma regra

Cada regra responde duas perguntas: **de onde vem o valor** e **para onde ele vai**. As demais opções são transformações aplicadas no meio do caminho.

| Opção na regra | O que faz | Obrigatória? |
|----------------|-----------|--------------|
| Campo de destino (`target`) | Para qual campo padronizado o valor vai | Sim |
| Campo de origem (`source`) | De qual campo do evento original o valor vem | Sim* |
| Valor fixo (`const`) | Um valor constante, igual para todos os eventos | Sim* |
| Valor padrão (`default`) | O que usar quando a origem vier vazia | Não |
| Conversão antes da tradução (`pre_cast`) | Converte o tipo antes de consultar a tabela de tradução | Não |
| Tabela de tradução (`value_map`) | Troca valores do fornecedor pelos valores padrão | Não |
| Conversão de tipo (`type_cast`) | Converte o formato final (data, número, texto) | Não |
| Campo obrigatório (`required`) | Isola o evento se este campo vier vazio | Não |
| Fontes alternativas (`fallback_source`) | Outros campos a tentar se o principal vier vazio | Não |
| Condição (`when`) | Só aplica a regra se uma condição for verdadeira | Não |
| Padrão esperado (`expected_always_default`) | Marca que é normal este campo cair sempre no valor padrão | Não |

\* Toda regra precisa de **um campo de origem OU um valor fixo** — nunca os dois ao mesmo tempo.

---

### Campo de destino (`target`)

**O que é:** o campo padronizado (OCSF) que esta regra preenche.

**Como informar:** o caminho do campo separado por ponto, por exemplo:

- `normalized.class_uid`
- `normalized.finding_info.title`
- `normalized.device.name`

**Lembre-se:** o campo de destino nunca fica em branco.

---

### Campo de origem (`source`)

**O que é:** o campo do evento original (como o fornecedor enviou) de onde o valor é lido.

**Como informar:** o nome do campo. Pode ser um campo simples, um campo aninhado ou uma expressão de busca:

| Exemplo | O que pega |
|---------|------------|
| `severity` | um campo simples na raiz do evento |
| `device.name` | um campo dentro de outro |
| `createdAt \|\| raisedAt` | tenta `createdAt`; se vazio, usa `raisedAt` |

**Comportamento:**

- Se a origem vier vazia, a regra passa para o **valor padrão** (se houver).
- Uma lista vazia conta como valor preenchido — não é o mesmo que vazio.

> **Origem ou valor fixo, nunca os dois.** Cada regra usa **ou** um campo de origem **ou** um valor fixo. Se você preencher os dois, a validação acusa erro (ver [Quando algo dá errado](#quando-algo-dá-errado)).

---

### Valor fixo (`const`)

**O que é:** um valor constante, gravado igual em todos os eventos daquele fornecedor — sem ler nada do evento original.

**Quando usar:** para campos OCSF que são sempre os mesmos para um fornecedor. Por exemplo, o código da classe do evento, ou um rótulo fixo como `Detection Finding`.

**Comportamento:** o valor fixo ainda passa pelas etapas de **valor padrão**, **tabela de tradução** e **conversão de tipo**, se você as configurar.

---

### Valor padrão (`default`)

**O que é:** o que gravar quando a origem (ou o valor fixo) vier vazia.

**Comportamento:**

- Se o valor estava vazio, ele vira o valor padrão.
- Se o valor veio preenchido, o padrão é ignorado.

É a primeira coisa aplicada depois de ler a origem.

---

### Conversão antes da tradução (`pre_cast`)

**O que é:** uma conversão de tipo aplicada **antes** de consultar a tabela de tradução.

**Quando usar:** o fornecedor manda o valor em um tipo, mas a sua tabela de tradução usa outro. Exemplo clássico: o produto envia a severidade como número (`0` a `10`), mas as chaves da sua tabela são texto (`high`, `medium`, `low`). Você converte número para texto antes da busca.

Os tipos de conversão disponíveis estão na [Referência de operadores](operators-reference.md#operadores-de-conversão-de-valor).

---

### Tabela de tradução (`value_map`)

**O que é:** uma tabela que troca os valores do fornecedor pelos valores que o padrão exige.

**Quando usar:** o fornecedor envia categorias em texto e o OCSF espera códigos numéricos — o caso mais comum é severidade.

| Valor do fornecedor | Valor padronizado |
|---------------------|-------------------|
| `critical` | `5` |
| `high` | `4` |
| `medium` | `3` |
| `low` | `2` |
| `info` | `1` |

**Comportamento:**

- Se o valor de entrada está na tabela, usa o valor traduzido.
- Se não está, o valor passa sem alteração (ou cai no **valor padrão**, se você definir um).
- A busca por texto não diferencia maiúsculas de minúsculas — `high` e `HIGH` são tratados igual.

---

### Conversão de tipo (`type_cast`)

**O que é:** a conversão do formato final do valor, aplicada por último.

**Exemplos de uso:**

- Converter uma data em texto (ISO) para o formato de horário do padrão.
- Forçar um texto para maiúsculas.
- Converter um texto numérico em número.

Os tipos de conversão disponíveis estão na [Referência de operadores](operators-reference.md#operadores-de-conversão-de-valor).

---

### Campo obrigatório (`required`)

**O que é:** marca o campo como indispensável. Se o valor final vier vazio, o evento inteiro é isolado para revisão.

**Quando usar:** para campos OCSF que não podem faltar — por exemplo o horário do evento ou o identificador do achado. Marcando como obrigatório, qualquer evento sem esse campo vai para a **Quarentena** em vez de seguir incompleto, o que evita alertas "quebrados" nas telas de operação.

**O que acontece quando falta:** o evento aparece em **Normalização -> Quarentena**, com o motivo indicando que faltou um campo do mapeamento. De lá você revisa o evento, corrige o mapeamento e reprocessa.

---

### Fontes alternativas (`fallback_source`)

**O que é:** uma lista de outros campos a tentar, em ordem, se o campo de origem principal vier vazio.

**Quando usar:** o fornecedor coloca a mesma informação em campos diferentes dependendo do tipo de evento, ou mudou o nome do campo entre versões. Você lista as alternativas e a plataforma tenta uma a uma.

**Comportamento:**

1. Tenta o campo de origem principal.
2. Se vier vazio, tenta a primeira alternativa.
3. Continua até achar um valor preenchido.
4. Se todas vierem vazias, usa o **valor padrão**.

---

### Condição (`when`)

**O que é:** um filtro que faz a regra rodar **só quando** uma condição for verdadeira.

**Quando usar:** você só quer preencher um campo em certos eventos. Por exemplo, gravar o e-mail do remetente apenas quando o evento realmente tiver um remetente.

**Comportamento:** se a condição não for atendida, a regra é **pulada** por completo — o campo de destino fica sem ser escrito (diferente de gravar vazio).

As condições disponíveis estão na [Referência de operadores](operators-reference.md#operadores-de-condição).

---

### Padrão esperado (`expected_always_default`)

**O que é:** uma marcação informativa. Diz que é normal este campo sempre cair no valor padrão, porque o fornecedor simplesmente não fornece essa informação.

**Quando usar:** durante a validação, a plataforma avisa quando uma regra está sempre usando o valor padrão (sinal de que pode estar mal configurada). Se você sabe que é esperado — o fornecedor não tem aquele dado — marque esta opção para que o aviso não apareça.

**Importante:** essa marcação não muda o resultado da regra. Ela só silencia o aviso de validação para casos conhecidos.

---

## Etapas de preparação (preprocess)

Alguns fornecedores entregam parte da informação "embrulhada" — por exemplo, um campo de texto que na verdade contém outro evento em formato JSON. As **etapas de preparação** rodam uma vez por evento, antes de qualquer regra, para "desembrulhar" esse conteúdo e deixá-lo disponível para as regras lerem.

Hoje a operação de preparação disponível é a leitura de um campo de texto que contém JSON, transformando-o em campos navegáveis. Depois da preparação, suas regras conseguem apontar para os campos de dentro desse conteúdo.

| O que você informa | Significado |
|--------------------|-------------|
| A operação | Qual preparação aplicar (atualmente, ler texto JSON) |
| O campo de origem | De onde vem o conteúdo embrulhado, no evento original |
| O destino interno | Onde guardar o resultado para as regras usarem |
| Tolerância a erro | Se ligada, conteúdo malformado é ignorado em silêncio em vez de falhar |

As operações de preparação disponíveis estão na [Referência de operadores](operators-reference.md#operador-de-pré-processamento).

---

## Em que ordem as transformações acontecem

Para cada regra, a plataforma aplica as opções nesta ordem:

1. **Lê a origem** (ou usa o valor fixo).
2. **Valor padrão** — se vier vazio, usa o padrão.
3. **Conversão antes da tradução** — ajusta o tipo para a tabela.
4. **Tabela de tradução** — troca o valor pelo padronizado.
5. **Conversão de tipo** — ajusta o formato final.
6. **Grava** no campo de destino.

Saber essa ordem ajuda a entender por que, por exemplo, a conversão de número para texto precisa vir **antes** da tabela de tradução de severidade.

---

## Construir listas de observáveis

Há um tipo especial de regra para montar **listas de observáveis** (IPs, e-mails, hashes, etc.) a partir de vários campos do evento de uma vez. Em vez de uma regra por item, você descreve cada observável que quer extrair e a plataforma monta a lista.

Para cada item você informa:

| O que você informa | Significado |
|--------------------|-------------|
| Nome e tipo | Como identificar o observável (ex.: "endereço IP", "endereço de e-mail") |
| O campo de origem | De onde ler o valor no evento |
| Expandir lista | Se o campo já é uma lista (ex.: vários destinatários), gera um observável por item |

No nível da regra você ainda pode:

- **Omitir vazios** — não cria observáveis sem valor.
- **Remover duplicados** — descarta repetições, mantendo a primeira ocorrência.

Para um passo a passo, veja o [Cookbook](cookbook.md) e os [Casos de uso](use-cases.md).

---

## Quando algo dá errado

Você descobre problemas de mapeamento em dois momentos.

### Ao salvar (problema na definição da regra)

Se uma regra estiver mal montada, a plataforma **bloqueia o salvamento** e mostra um aviso de validação na tela do editor, indicando qual regra ou campo está com erro. Os casos mais comuns são:

- A expressão do campo de origem está escrita de forma inválida.
- Um nome de conversão de tipo não existe.
- A regra tem campo de origem **e** valor fixo ao mesmo tempo.
- Faltou um campo obrigatório na própria regra.

**O que fazer:** corrija a regra apontada pelo aviso e rode a validação novamente antes de salvar. Você sempre acessa o editor por **Normalização -> Mappings** (ou pela seção de mapeamento da integração em **Visão geral -> Integrações**).

### Ao processar eventos (problema com um evento real)

Mesmo com o mapeamento salvo e válido, um evento específico pode falhar — por exemplo, um campo obrigatório veio vazio, ou um valor não pôde ser convertido (texto onde se esperava número). Nesses casos o evento vai para a **Quarentena**.

**O que fazer:**

1. Abra **Normalização -> Quarentena** para ver os eventos isolados e o motivo de cada um.
2. Use o motivo para identificar qual regra precisa de ajuste (campo obrigatório, conversão, etc.).
3. Corrija o mapeamento no editor e reprocesse os eventos isolados.

Para um guia detalhado, veja [Resolução de problemas](troubleshooting.md).
