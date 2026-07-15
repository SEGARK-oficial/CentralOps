---
sidebar_position: 6
title: Resolver problemas de mapeamento
description: Guia por sintoma para diagnosticar e corrigir eventos que falham ao serem normalizados, direto pela interface do CentralOps.
---

# Resolver problemas de mapeamento

Quando um evento de um fornecedor não consegue ser normalizado, o CentralOps o envia para a **Quarentena** e registra o motivo. Esta página é um guia por sintoma: você parte da mensagem de erro que aparece na tela e segue até a correção, sempre pela interface da plataforma.

Você nunca precisa de terminal nem de editor de texto externo. Toda mudança de mapeamento é feita pelo **editor de mapeamento** da plataforma, em **Normalizacao -> Mappings**, que oferece pré-visualização e teste de expressão direto na tela antes de salvar.

## Quando usar

- **Eventos sumindo do Dashboard.** Você percebe que um fornecedor parou de aparecer nos painéis e suspeita que os eventos estão sendo barrados na normalização — comece pela **Quarentena**.
- **Depois de publicar uma versão nova de mapeamento.** Você ajustou as regras de um fornecedor e, logo em seguida, a contagem de quarentena disparou. Use o guia de reversão abaixo.
- **Revisão periódica de cobertura.** Durante a triagem semanal, você abre o **Drift Explorer** para ver quais campos do fornecedor ainda não estão sendo aproveitados nas investigações.

## Onde os erros aparecem

| Tela (menu) | O que ela mostra |
|-------------|------------------|
| **Normalizacao -> Quarentena** | Eventos que falharam ao mapear. A coluna de motivo traz a mensagem de erro que dá nome a cada sintoma abaixo. |
| **Normalizacao -> Drift Explorer** | Campos enviados pelo fornecedor que o seu mapeamento ainda não usa. |
| **Normalizacao -> Saude do Pipeline** | Visão geral de quanto de cada fonte está sendo normalizado com sucesso. |
| **Normalizacao -> Mappings** | O editor onde você corrige as regras, pré-visualiza o resultado e publica uma nova versão. |

:::tip[Antes de editar]
No editor de mapeamento (**Normalizacao -> Mappings**), use a pré-visualização de expressão para validar o caminho de um campo contra um evento de amostra **antes** de salvar. Assim você confirma se o campo existe e como ele chega, sem sair da interface.
:::

---

## Eventos em quarentena por falha de mapeamento

Quando um evento aparece em **Normalizacao -> Quarentena** com motivo relacionado a mapeamento, abra o evento para ver a mensagem completa. Os sintomas mais comuns estão abaixo.

### "Campo obrigatório resolveu para vazio"

Mensagem na coluna de motivo: o evento exigia um campo obrigatório (por exemplo, o horário do evento) e esse campo veio vazio.

**O que costuma causar:**

- O fornecedor não enviou o campo, ou enviou em branco.
- A regra está apontando para um nome de campo que não existe naquele tipo de evento.
- A conversão de tipo rejeitou um valor inválido (por exemplo, tentar transformar `"abc"` em número).

**Como diagnosticar:**

1. Abra uma amostra do evento na própria tela de Quarentena e confira se o campo realmente existe no que o fornecedor mandou.
2. No editor de mapeamento, use a pré-visualização de expressão para testar o caminho do campo contra essa amostra.

**Como corrigir, no editor de mapeamento:**

| Situação | Ajuste na regra |
|----------|-----------------|
| O campo às vezes vem ausente | Defina um **valor padrão** para a regra. |
| O fornecedor usa nomes diferentes para o mesmo dado | Configure um ou mais **campos alternativos** (a regra tenta o próximo quando o primeiro vem vazio). |
| O valor chega num tipo incompatível | Adicione uma **conversão de tipo** ou um **mapa de valores** antes da regra obrigatória. |
| O campo é mesmo opcional | **Retire a marcação de obrigatório** da regra. |

### "Horário inválido"

Mensagem na coluna de motivo: a regra de horário recebeu um valor de data/hora mas não conseguiu interpretá-lo — geralmente porque o fornecedor usa um formato ou fuso fora do padrão.

**Como diagnosticar:**

1. Veja como o horário chega na amostra do evento (formato, fuso, casas decimais).
2. Confirme se é um formato de data/hora reconhecível.

**Como corrigir, no editor de mapeamento:**

- **Fuso ou espaços fora do padrão:** ajuste a regra para limpar o valor antes de convertê-lo em horário.
- **O fornecedor envia o horário em milissegundos:** confirme a unidade na amostra. O tratamento de horários em milissegundos pode exigir ajuste específico — se você não encontrar a opção no editor, registre o caso e fale com o administrador da plataforma para avaliar um ajuste de mapeamento.
- **Formato totalmente fora do padrão:** documente o caso e fale com o administrador da plataforma; pode ser necessário um tratamento específico para esse fornecedor.

### "Texto não numérico em campo numérico"

Mensagem na coluna de motivo: uma regra esperava um número, mas o fornecedor mandou um texto como `"N/A"` ou `"unknown"`.

**Como diagnosticar:**

1. Veja com que frequência isso acontece — o **Drift Explorer** ajuda a entender a distribuição de valores daquele campo.
2. Decida se o evento deve usar um valor padrão ou ser ignorado nesses casos.

**Como corrigir, no editor de mapeamento:**

| Situação | Ajuste na regra |
|----------|-----------------|
| O texto inválido aparece raramente | Defina um **valor padrão** numérico. |
| O fornecedor usa rótulos fixos (`"critical"`, `"N/A"`) | Use um **mapa de valores** que traduz cada rótulo para o número correspondente antes da conversão. |
| O campo só faz sentido quando válido | Use uma **condição** na regra para só preencher o campo quando o valor estiver na faixa esperada. |

### "Valor fora da faixa esperada"

Mensagem na coluna de motivo: o fornecedor usa uma escala diferente da esperada (por exemplo, severidade de 0 a 150) e a conversão esperava de 0 a 100.

**Como diagnosticar:**

1. Descubra qual é a escala real de severidade do fornecedor (0 a 10, 0 a 100, 0 a 1000, customizada).
2. Defina para qual escala você quer normalizar.

**Como corrigir, no editor de mapeamento:**

- Use um **mapa de valores** para traduzir a escala do fornecedor para a escala-alvo antes da conversão.
- Ou, se a escala do fornecedor já serve, troque a conversão por uma que apenas transforme o valor em número, sem reescalonar.

### "Lista de evidências vazia"

Mensagem na coluna de motivo: uma regra que monta uma lista de evidências (por exemplo, um conjunto de indicadores como IPs e identificadores de dispositivo) resultou em lista vazia, porque todos os itens vieram nulos.

**Como diagnosticar:**

1. Confira se a regra de lista foi marcada como obrigatória por engano — esse tipo de regra não pode ser obrigatório.
2. Verifique se todos os itens da lista realmente estão ausentes na amostra do evento.

**Como corrigir, no editor de mapeamento:**

- **Retire a marcação de obrigatório** da regra de lista (não é suportada para esse tipo de regra).
- Se você precisa garantir pelo menos um item, adicione uma **condição** para que a regra só seja aplicada quando o dado de origem existir.

---

## Aviso de "sempre usa o valor padrão"

Ao pré-visualizar um mapeamento contra eventos de amostra, o editor pode avisar que uma regra **sempre** cai no valor padrão — ou seja, a origem nunca traz um valor.

**Provavelmente não é problema, se:**

- O fornecedor não oferece esse campo (lacuna conhecida).
- A regra é um espaço reservado para uso futuro.
- Os campos alternativos são todos opcionais.

**Provavelmente é problema, se:**

- O nome do campo está escrito errado.
- Faltou alguma etapa de preparação que deveria gerar o dado de origem.

**Como resolver, no editor de mapeamento:**

1. **Confirme que é intencional.** Marque a regra como "padrão esperado" para silenciar o aviso (procure essa opção na configuração da regra).
2. **Remova a regra**, se o campo não for importante.
3. **Investigue a origem.** Use a pré-visualização de expressão para testar o caminho do campo contra uma amostra real. Se a pré-visualização retorna vazio, a expressão está certa mas o fornecedor não envia aquele dado — nesse caso, marque como padrão esperado.

---

## Muitos campos não mapeados no Drift Explorer

A tela **Normalizacao -> Drift Explorer** lista os campos novos detectados nos eventos do fornecedor que o seu mapeamento ainda não aproveita.

**Campos que normalmente dá para ignorar:**

- Identificadores internos, metadados e marcadores de versão sem significado para a investigação.
- Identificadores opacos sem valor semântico.
- Campos que duplicam informação já capturada em outro lugar.

**Lacunas reais que valem a pena mapear:**

- Endereços IP, hostnames, nomes de usuário.
- Caminhos de arquivo, hashes, domínios.
- Severidade, tipo de detecção, categoria.
- Horários e fusos.
- Detalhes de processo, arquivo e rede.

**Fluxo de trabalho:**

1. Revise os campos não mapeados no **Drift Explorer**.
2. Pergunte-se: "Isso é acionável numa investigação de segurança?"
3. Decida:
   - **Sim** → abra **Normalizacao -> Mappings** e adicione uma regra para o campo.
   - **Não** → registre-o como ignorado, deixando uma anotação no mapeamento explicando o motivo, para que a próxima pessoa não o reavalie sem necessidade.

---

## Uma mudança de mapeamento quebrou a produção (reverter)

Você publicou uma versão nova do mapeamento e os eventos começaram a cair em quarentena. O caminho mais rápido é voltar para a versão anterior e investigar com calma.

**Passos para reverter, em Normalizacao -> Mappings:**

1. Abra o mapeamento afetado e veja o **histórico de versões**.
2. Identifique a última versão que funcionava (a anterior à mudança problemática).
3. **Ative** essa versão anterior.
4. O efeito é imediato: os eventos novos passam a usar o mapeamento antigo.
5. Os eventos que já caíram em quarentena durante a versão problemática continuam lá. Depois de corrigir o mapeamento, reprocesse esses eventos a partir da tela **Normalizacao -> Quarentena** (selecione os eventos afetados e reprocesse).

**Como evitar a próxima regressão:**

- Sempre use a **pré-visualização** do editor antes de publicar.
- Teste contra uma amostra representativa de eventos do fornecedor, não apenas um.
- Em mudanças grandes, publique em etapas: primeiro as regras de campos opcionais e, depois, as alterações em campos obrigatórios (que têm risco maior).

---

## A regra não encontra o dado mesmo com o campo presente

Você apontou a regra para um caminho de campo, mas o valor chega vazio embora o evento do fornecedor tenha o dado. Use a pré-visualização de expressão no editor para testar cada hipótese contra uma amostra real.

**Causas comuns:**

| Causa | Como confirmar e corrigir |
|-------|---------------------------|
| Diferença de maiúsculas/minúsculas | O fornecedor usa, por exemplo, `Device.name` (com inicial maiúscula). Ajuste o caminho na regra para bater exatamente. |
| Falta o índice de uma lista | O fornecedor manda uma lista de valores. Aponte para a posição desejada da lista em vez do campo inteiro. |
| Erro de digitação no caminho aninhado | O caminho real tem um nível a mais ou a menos. Corrija o caminho na regra. |
| O fornecedor usa estruturas diferentes por tipo de evento | Configure **campos alternativos** para cobrir as duas formas (a regra tenta o próximo quando o primeiro vem vazio). |

---

## O mapa de valores não está traduzindo

Você definiu um mapa de valores (por exemplo, traduzindo `"high"` para um número), mas os eventos passam direto sem tradução.

**Causas comuns:**

| Causa | Como corrigir, no editor de mapeamento |
|-------|----------------------------------------|
| Tipos diferentes entre a origem e as chaves do mapa | A origem chega como número, mas as chaves do mapa são texto (ou vice-versa). Normalize o tipo antes do mapa de valores. |
| Valor fora do mapa | Por padrão, valores não listados passam sem tradução. Se você quer um destino fixo para o desconhecido, inclua uma entrada de "todos os demais" no mapa. |

---

## Como ler as mensagens de erro

A tabela abaixo traduz as mensagens mais comuns que aparecem na coluna de motivo da **Quarentena** e indica onde agir.

| Mensagem | Causa provável | Onde corrigir |
|----------|----------------|---------------|
| Campo obrigatório resolveu para vazio | Campo obrigatório ausente, ou conversão de tipo falhou | Revise a origem e o valor padrão da regra |
| Horário inválido | Formato ou fuso de data/hora não suportado | Ajuste a regra de horário; limpe o valor antes de converter |
| Texto não numérico em campo numérico | Fornecedor mandou texto num campo numérico | Adicione mapa de valores ou valor padrão |
| Conversão de tipo desconhecida | Nome de conversão escrito errado | Confira o nome na referência de operadores |
| Caminho de expressão inválido | Erro de digitação no caminho do campo | Valide com a pré-visualização de expressão |
| Lista de evidências vazia | Todos os itens da lista vieram nulos | Use uma condição para preencher só quando houver dado |

---

## Onde buscar ajuda

1. **Confira o Drift Explorer** (**Normalizacao -> Drift Explorer**) para ver campos do fornecedor ainda não mapeados.
2. **Use a pré-visualização do editor** (**Normalizacao -> Mappings**) para encontrar regras que sempre caem no valor padrão.
3. **Valide o caminho de cada campo** com a pré-visualização de expressão, contra uma amostra real, antes de salvar.
4. **Consulte a referência de operadores** para detalhes de conversões e condições disponíveis.
5. **Compare com mapeamentos de fornecedores parecidos** (por exemplo, alertas e detecções do mesmo fornecedor) para reaproveitar padrões.

Se um problema persistir mesmo após esses ajustes, ou se a correção parecer depender de algo fora do editor de mapeamento, fale com o administrador da plataforma.
