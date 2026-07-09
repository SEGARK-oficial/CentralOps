---
sidebar_position: 3
title: Quarentena
description: Triagem de eventos que falharam na validação antes de chegar aos destinos
---

# Quarentena

A tela de **Quarentena** reúne os eventos que não conseguiram ser processados pela plataforma — porque vieram malformados, perderam um campo obrigatório ou bateram contra uma regra de mapeamento desatualizada. Em vez de descartar esses eventos silenciosamente, o CentralOps os guarda aqui para que você decida o que fazer: descartar o que é ruído ou reprocessar depois de corrigir a regra de mapeamento.

A Quarentena fica no menu **Normalização -> Quarentena**.

:::note Quem pode agir
Qualquer pessoa com acesso de leitura consegue ver a fila e abrir os detalhes de um evento. As ações de descartar e reprocessar exigem permissão de operação. Se os botões aparecerem desabilitados para você, fale com o administrador da plataforma.
:::

## Quando usar

- **Um fornecedor mudou o formato dos eventos.** De repente dezenas de eventos do mesmo fornecedor (por exemplo, Sophos) caem em quarentena com o mesmo erro de "campo obrigatório ausente". Você usa a Quarentena para confirmar a mudança, ajustar a regra de mapeamento e reprocessar o lote.
- **Investigar uma queda no volume de um destino.** O time percebe que os eventos de um fornecedor pararam de chegar ao SIEM. A Quarentena mostra se eles estão sendo barrados na validação e qual é o erro exato.
- **Limpar ruído conhecido.** Um fornecedor envia eventos de teste ou pacotes corrompidos que nunca deveriam ser normalizados. Você filtra por fornecedor e descarta o lote de uma vez.

## O que vai para quarentena

Um evento vai para a quarentena quando não passa em alguma etapa de validação:

| Situação | O que significa |
| --- | --- |
| Falha de leitura | O evento chegou corrompido ou com formato inválido (JSON quebrado, codificação errada) e a plataforma não conseguiu interpretá-lo. |
| Campo obrigatório ausente | A regra de mapeamento espera um campo que não veio no evento (por exemplo, data/hora ou severidade). |
| Valor inválido | Um campo veio com um tipo ou valor que a regra não aceita (por exemplo, texto onde se esperava um número, ou uma severidade fora da lista permitida). |
| Integração não reconhecida | O evento chegou de uma integração que foi removida ou que ainda não foi sincronizada com a plataforma. |

Eventos que passam na validação **não** vão para a quarentena: eles seguem direto para a normalização e, em seguida, são enviados aos destinos configurados.

## Quarentena não é o mesmo que fila de reenvio

São duas situações diferentes, em pontos diferentes do caminho do evento:

| | Quarentena | Fila de reenvio (por destino) |
| --- | --- | --- |
| **Onde fica** | Menu **Normalização -> Quarentena** | Menu **Operação -> Destinos**, dentro de cada destino |
| **O que aconteceu** | O evento **não conseguiu sair** da plataforma: falhou na leitura, normalização ou validação. | O evento foi processado e enviado, mas um **destino específico recusou** a entrega (conexão fora do ar, formato incompatível, destino temporariamente protegido). |
| **Como resolver** | Descartar o evento ou corrigir a regra de mapeamento e reprocessar. | Reenviar o evento ao destino depois que ele voltar a funcionar. |

Resumo: **quarentena** = o evento não saiu da plataforma. **Fila de reenvio** = o evento saiu, mas o destino não aceitou.

A fila de reenvio é descrita em [Destinos e Roteamento](../outputs/destinations.md). O restante desta página trata da quarentena.

## Como fazer a triagem

### Passo 1 — Abrir a Quarentena

Vá ao menu **Normalização -> Quarentena**. A lista mostra, para cada evento:

- Quando foi recebido.
- De qual fornecedor / integração veio.
- Uma descrição curta do erro.
- As ações disponíveis.

### Passo 2 — Abrir e revisar o detalhe

1. Clique em um evento para expandir os detalhes.
2. Veja o conteúdo original do evento, exatamente como o fornecedor enviou.
3. Leia o erro específico (por exemplo, "campo obrigatório 'timestamp' ausente" ou "valor inválido no campo 'severity'").
4. Decida: é um problema na sua regra de mapeamento (algo a corrigir) ou uma anomalia do fornecedor (ruído a descartar)?

### Passo 3 — Escolher a ação

**Descartar** — para eventos que são ruído e nunca deveriam ser normalizados.

- Use quando o fornecedor enviou um evento malformado, um evento de teste, ou quando o campo faltou por causa de uma mudança do próprio fornecedor que você não quer tratar.
- O evento sai da fila e não é reprocessado nem enviado a nenhum destino.

**Reprocessar** — para eventos que falharam por causa de uma regra de mapeamento e que você já corrigiu.

- Pré-requisito: você (ou alguém do time) já ajustou a regra de mapeamento na tela **Normalização -> Mappings**.
- O evento volta para o processamento em segundo plano, é normalizado de novo com a regra atualizada e segue para os destinos configurados.

### Passo 4 — Confirmar que funcionou

Depois de reprocessar:

- **Funcionou:** o evento desaparece da quarentena e é entregue aos destinos.
- **Falhou de novo:** o evento volta para a quarentena com um novo erro. Volte ao passo 2.
- **Foi entregue, mas um destino recusou:** o evento aparece na fila de reenvio daquele destino (menu **Operação -> Destinos**).

## Corrigir a regra de mapeamento e reprocessar

O caso mais comum é um evento que falhou porque o fornecedor passou a enviar um campo com outro nome. O fluxo de ponta a ponta é:

1. Na **Quarentena**, abra o evento e leia o erro (por exemplo, "campo obrigatório 'severity' ausente").
2. Veja o conteúdo original do evento. Suponha que ele traz a severidade em um campo chamado `alert_level` em vez de `severity`.
3. Vá ao menu **Normalização -> Mappings** e abra a regra do fornecedor correspondente.
4. No editor de mapeamento, aponte o campo de severidade para `alert_level` e salve uma nova versão da regra.
5. Volte à **Quarentena**, abra o evento e clique em reprocessar.
6. Após alguns segundos, o evento sai da quarentena e é enviado normalmente.

:::tip
O editor de mapeamento é um formulário guiado na tela **Mappings** — você não precisa escrever código solto aqui. Para detalhes de como montar e testar uma regra, veja a página de [Mappings](../normalization/overview.md). Sempre teste o ajuste reprocessando **um** evento antes de aplicar a um lote inteiro.
:::

## Filtros

Use os filtros para focar na triagem que importa no momento.

- **Por fornecedor / integração:** mostra só os eventos de um fornecedor (por exemplo, "todos os erros de Sophos desta manhã").
- **Por tipo de erro:** falha de leitura, campo obrigatório ausente, valor inválido ou integração não reconhecida.
- **Por data:** últimas 24 horas (padrão), últimos 7 dias ou um intervalo personalizado.

## Ações em massa

Quando há muitos eventos do mesmo tipo (por exemplo, uma mudança de fornecedor que derrubou um lote inteiro):

1. Filtre pelo fornecedor ou tipo de erro.
2. Selecione os eventos e use a ação em massa de **descartar** para limpar ruído, ou de **reprocessar** depois de corrigir a regra de mapeamento.

Comece sempre validando com um único evento antes de reprocessar centenas.

## Exemplos de triagem

### Um fornecedor mudou o formato dos eventos

**Sintoma:** muitos eventos do mesmo fornecedor em quarentena com o erro "campo X ausente".

1. Abra três eventos diferentes e compare o conteúdo original — procure um campo novo que antes não existia, ou um campo que sumiu.
2. Vá ao menu **Normalização -> Mappings**, abra a regra do fornecedor e ajuste o campo afetado.
3. Reprocesse **um** evento para confirmar.
4. Se funcionou, volte à Quarentena e reprocesse o lote em massa.

### Falsos positivos de validação

**Sintoma:** eventos em quarentena com erro de valor inválido, mas o campo tem um valor que parece legítimo.

1. Abra o evento e compare o valor original com o que a regra espera (por exemplo, o evento traz a severidade como texto, mas a regra espera um número).
2. Em **Mappings**, ajuste a regra para converter o campo no formato esperado.
3. Reprocesse o evento e confirme.

### Campo obrigatório faltando

**Sintoma:** erro "campo obrigatório 'timestamp' ausente".

1. Se o campo existe no evento original com outro nome, aponte a regra para esse nome em **Mappings**.
2. Se o fornecedor realmente não envia o campo, configure na regra um valor padrão para ele.
3. Reprocesse e confirme.

## Observações

- **Quarentena não é descarte definitivo.** Você sempre pode reprocessar um evento depois de corrigir a regra.
- **Tudo fica registrado.** Cada descarte e cada reprocessamento ficam no histórico, com quem fez e quando.
- **Retenção.** Os eventos ficam na quarentena por um período limitado e depois são removidos automaticamente. O tempo exato de retenção é definido pela equipe de infraestrutura no momento do deploy. Se precisar alterá-lo, fale com o administrador da plataforma.

## Próximos passos

- **Está caindo muita coisa na quarentena?** Veja [Solução de problemas em Mappings](../normalization/troubleshooting.md).
- **Precisa ajustar uma regra de mapeamento?** Veja [Mappings](../normalization/overview.md).
- **Quer entender para onde os eventos vão depois de normalizados?** Veja [Destinos e Roteamento](../outputs/destinations.md) e [Configurar Rotas](../outputs/routing.md).
