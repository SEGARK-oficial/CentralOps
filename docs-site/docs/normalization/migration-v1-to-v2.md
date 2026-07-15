---
sidebar_position: 6
title: "Atualizar um mapeamento para o formato novo"
description: "Quando e como migrar um mapeamento antigo para o formato atual, pela tela de Mappings — com fallback de campos, pré-processamento e múltiplos observáveis"
---

# Atualizar um mapeamento para o formato novo

O CentralOps transforma cada evento de um vendor (Sophos, Wazuh, Microsoft Defender, etc.) em um evento padronizado, usando as regras que você define em **Normalização → Mappings**. O formato atual dessas regras oferece recursos que o formato antigo não tinha: tentar mais de um campo de origem, ler dados que vêm "embrulhados" dentro de um texto, pular uma regra quando o campo não existe e montar listas de observáveis (IPs, e-mails, hashes).

Os mapeamentos antigos continuam funcionando — você só precisa atualizar um deles quando quiser usar um desses recursos novos. Esta página mostra quando vale a pena e como fazer, sempre pela interface.

:::tip[O formato atual já é o padrão]
Mapeamentos novos que você cria pela tela de **Mappings** já nascem no formato atual. Não há nenhuma ação obrigatória da sua parte: este guia é só para quem quer **adicionar um recurso novo a um mapeamento antigo**.
:::

---

## Quando usar

Use este guia quando, ao ajustar um mapeamento, você precisar de algo que o formato antigo não suporta:

- **O vendor mudou o nome de um campo.** Algumas detecções trazem o horário em `createdAt`, outras em `raisedAt`. Você quer que a regra tente o primeiro e, se vier vazio, caia para o segundo — sem perder o evento na quarentena.
- **O dado importante vem dentro de um texto.** O Sophos, por exemplo, entrega detalhes de um e-mail (IP de origem, destinatários) embrulhados como texto dentro de um único campo. Para extrair o IP e os destinatários, a regra precisa primeiro "abrir" esse texto.
- **Um único evento gera vários observáveis.** Um alerta de e-mail pode ter vários destinatários. Você quer um observável por destinatário (no padrão da plataforma), em vez de uma lista solta de endereços.

Se nenhum desses casos se aplica, o mapeamento antigo segue funcionando e não há nada a fazer.

---

## O que muda no formato novo

| Recurso | Para que serve |
|---------|----------------|
| Pré-processamento | Abrir dados que chegam embrulhados como texto (ex.: o campo `processedData` do Sophos) antes de aplicar as regras |
| Origem alternativa (fallback) | Tentar mais de um campo de origem na ordem que você definir (ex.: `createdAt`, depois `raisedAt`) |
| Condição na regra | Executar a regra só quando o campo de origem existe — em vez de gravar um valor vazio |
| Lista de observáveis | Montar uma lista de observáveis (um IP, vários e-mails, etc.) a partir de um campo |
| Marcar campo como placeholder intencional | Sinalizar que uma regra grava um valor fixo de propósito, sem disparar alerta de campo faltando |

Todo o resto que você já usava (origem de campo, valor fixo, valor padrão, conversão de tipo, mapa de valores, marcar campo como obrigatório) continua funcionando igual no formato novo.

---

## Onde você edita um mapeamento

Tudo abaixo acontece na interface, sem terminal e sem editar arquivos:

1. Abra o menu **Normalização → Mappings**.
2. Selecione o mapeamento que quer atualizar.
3. Faça as alterações no editor de mapeamento da própria tela.
4. Use o **teste sem efeito (dry-run)** para conferir o resultado contra uma amostra de eventos antes de valer para o tráfego real.
5. Salve. Cada vez que você salva, o CentralOps guarda uma nova versão do mapeamento (o histórico fica disponível na própria tela, na lista de versões).

O editor trabalha com dois blocos: **pré-processamento** (opcional, onde você "abre" dados embrulhados) e **regras** (onde você mapeia cada campo). Mapeamentos antigos têm só a lista de regras; ao atualizar, você passa a ter esses dois blocos.

---

## Como atualizar, passo a passo

### 1. Identifique o que está faltando

Comece pelo problema concreto. Exemplos: "a severidade às vezes vem em outro campo", "preciso do IP que está escondido dentro de um texto", "quero um observável por destinatário". Cada um desses casos corresponde a um recurso da tabela acima.

### 2. Acrescente o pré-processamento, se precisar

Se o vendor entrega dados embrulhados como texto (como o `processedData` do Sophos), adicione uma etapa de pré-processamento no editor para "abrir" esse texto. A partir daí, suas regras conseguem ler os campos de dentro dele (por exemplo, o IP de origem e a lista de destinatários).

### 3. Ajuste as regras para usar os recursos novos

No editor de regras, você pode agora:

- **Definir uma origem alternativa:** a regra tenta o campo principal e, se vier vazio, usa o próximo da lista que você indicar.
- **Adicionar uma condição:** a regra só roda quando o campo de origem existe, evitando gravar valores vazios.
- **Montar uma lista de observáveis:** transforme um campo (por exemplo, a lista de destinatários) em vários observáveis no padrão da plataforma, um para cada item.

### 4. Teste antes de ativar

Antes de qualquer alteração valer para o tráfego real, rode o **teste sem efeito (dry-run)** contra uma amostra representativa (10 a 20 eventos). Confira:

- **Quarentena:** algum evento da amostra caiu na quarentena? Se sim, costuma ser uma regra a corrigir — veja a [Resolução de problemas](troubleshooting.md).
- **Campos novos detectados (drift):** o teste aponta campos que não estão mapeados. Confirme se ficar de fora foi intencional.

### 5. Ative e acompanhe

Quando o teste estiver limpo, salve para criar a nova versão e ative-a na lista de versões da tela. A partir daí, os eventos novos passam a usar as regras atualizadas. Acompanhe por cerca de uma hora, de olho em qualquer aumento de quarentena. Eventos já processados antes não mudam.

---

## Exemplo real: detecção do Sophos

**Situação:** o mapeamento atual já preenche os campos básicos (classe da detecção, horário, identificador, severidade), mas não consegue extrair os dados de e-mail, que chegam embrulhados como texto dentro do campo `processedData`.

**Como resolver, pela tela de Mappings:**

1. **Adicione o pré-processamento** para "abrir" o campo `processedData`. Depois disso, os campos internos (como o IP de origem e a lista de destinatários) ficam acessíveis para as regras.
2. **Acrescente uma regra de lista de observáveis** que produz:
   - um observável de IP a partir do IP de origem encontrado dentro do texto;
   - um observável de e-mail para cada destinatário da lista.
3. **Rode o teste sem efeito** e confira que tudo continua mapeando, agora com os observáveis de e-mail e IP presentes.

O resultado é o mesmo mapeamento de antes, agora também extraindo os dados de e-mail que estavam "escondidos".

---

## Padrões comuns

### O nome do campo do vendor varia

O vendor às vezes manda o horário em `createdAt`, às vezes em `raisedAt`. Configure a regra com **origem alternativa**: ela tenta `createdAt` primeiro e, se vier vazio, segue para `raisedAt` (e os demais que você listar). Assim o evento não vai para a quarentena por falta de horário.

### Dado importante vem embrulhado como texto

Um campo chega como texto que contém outros campos lá dentro (por exemplo, `jsonData` com um IP). Adicione uma etapa de **pré-processamento** para "abrir" esse texto e, depois, mapeie normalmente o campo de dentro (o IP) na sua regra.

### Vários destinatários em um único evento

Em vez de gravar uma lista solta de endereços, use uma regra de **lista de observáveis** com a opção de gerar um item por destinatário. A saída fica no padrão de observáveis da plataforma — um observável de e-mail por destinatário.

### Não gravar valor quando o campo está ausente

Algumas regras gravam um valor vazio mesmo quando o campo de origem não existe (por exemplo, o e-mail do remetente). Adicione uma **condição** à regra para que ela só rode quando o campo existir; assim o campo fica de fora do evento em vez de ir vazio.

---

## Histórico de versões e como reverter

Cada vez que você salva um mapeamento, o CentralOps guarda uma nova versão. Na tela de **Mappings**, ao selecionar um mapeamento, você vê a lista de versões e qual está ativa (a versão ativa fica marcada).

- **Só uma versão fica ativa por vez** para cada combinação de collector e tipo de evento.
- Ao ativar uma versão, os eventos **novos** passam a usá-la imediatamente; eventos já processados não mudam.
- **Para reverter:** abra o mapeamento, vá até a lista de versões e ative a versão anterior. Os eventos novos voltam a usar a regra antiga na hora. Depois você corrige a versão nova com calma e salva de novo, gerando mais uma versão.

---

## Resolução de problemas

| Sintoma | O que verificar |
|---------|-----------------|
| Ao salvar, a tela acusa erro de formato do mapeamento | Confirme que o mapeamento tem os dois blocos do formato atual (**pré-processamento** e **regras**), e não apenas uma lista solta de regras. Mapeamentos antigos têm só a lista; ao atualizar, é preciso passar a usar os dois blocos. |
| A tela reclama de um nome de campo de destino reservado (começa com `_`) | Nomes que começam com `_` são reservados para os campos gerados no pré-processamento. Use o pré-processamento para gerar esse campo, ou escolha um nome de destino normal para a regra. |
| Uma condição na regra parece ser ignorada | Condições só funcionam no formato atual. Confirme que o mapeamento já foi atualizado (tem os blocos de pré-processamento e regras) e não está mais no formato antigo. |

Se um teste sem efeito ou o tráfego real estiver mandando eventos para a quarentena, veja a [Resolução de problemas](troubleshooting.md) para diagnóstico por sintoma.

---

## Perguntas frequentes

**Posso ter uma versão antiga e uma nova ativas ao mesmo tempo?**
Não. Só uma versão fica ativa por combinação de collector e tipo de evento. Troque ativando a versão desejada na lista de versões. Eventos já processados com a versão anterior permanecem como estão.

**Atualizar o mapeamento quebra os dados já existentes?**
Não. Eventos anteriores já foram normalizados e não mudam. Só os eventos **novos** usam o mapeamento atualizado.

**Como sei qual versão está ativa?**
Em **Normalização → Mappings**, selecione o mapeamento e olhe a lista de versões: a versão ativa fica marcada.

**Posso voltar para a versão anterior depois de atualizar?**
Sim. Basta ativar a versão anterior na lista de versões. Você perde os recursos novos, mas as regras antigas voltam a funcionar imediatamente.

**Existe um formato ainda mais novo?**
Não. O formato descrito aqui é o atual. Se surgirem recursos novos no futuro, eles serão documentados em guias próprios.
