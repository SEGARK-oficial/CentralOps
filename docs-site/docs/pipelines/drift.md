---
sidebar_position: 3
title: Campos novos detectados (drift)
description: Descubra campos que os fornecedores começaram a enviar e ainda não estão sendo aproveitados
---

# Campos novos detectados (drift)

O CentralOps detecta automaticamente quando um evento chega com um campo que ainda não está sendo aproveitado pela sua regra de mapeamento. Esses campos novos ficam reunidos em uma lista para você decidir o que fazer com cada um.

## Quando usar

- **O fornecedor atualizou a integração.** A Sophos (ou Defender, NinjaOne, etc.) passou a enviar campos novos em uma atualização. Você quer saber quais são e se vale a pena incorporá-los aos eventos normalizados.
- **Você suspeita que está perdendo contexto.** Uma investigação ficou sem um dado que você esperava ter (por exemplo, a confiança da detecção ou o tipo de malware). A lista de campos novos mostra o que está chegando mas ainda não aparece nos eventos tratados.
- **Auditoria de completude.** Antes de uma revisão de conformidade, você quer confirmar que está coletando todos os campos relevantes de cada fornecedor e que nada importante foi marcado como ignorado por engano.

## O que é "campo novo detectado"

Quando um evento chega com um campo que a sua regra de mapeamento ainda não usa, o CentralOps registra esse campo como **novo detectado** em vez de descartá-lo silenciosamente.

Por que isso importa: campos não aproveitados ficam **invisíveis** no evento normalizado, mesmo que sejam úteis. Exemplos típicos:

- Confiança da detecção (ótimo para priorizar e filtrar alertas).
- Tipo/família do malware (contexto da ameaça).
- Status de remediação (se o arquivo foi removido ou colocado em quarentena pelo fornecedor).

Se esses campos passarem despercebidos, você perde contexto que já estava disponível na origem.

## Isolamento entre organizações

A detecção de campos novos é **isolada por organização**. Você só enxerga os campos novos da sua própria organização, e o mesmo campo desconhecido em organizações diferentes é tratado como entrada separada. Não há mistura de dados entre clientes de um mesmo fornecedor.

## Onde encontrar na interface

Acesse o menu **Normalização -> Drift Explorer**. O Dashboard (menu **Visão geral -> Dashboard**) também costuma destacar os campos novos em um cartão de resumo, com atalho para a tela completa.

A tela lista os campos detectados com as seguintes informações:

| Coluna | Descrição |
|--------|-----------|
| Campo | Nome do campo recebido (ex.: confiança da detecção). |
| Fornecedor | Plataforma de origem (Sophos, Defender, etc.). |
| Tipo | Tipo inferido do valor (texto, número, verdadeiro/falso, lista ou estrutura aninhada). |
| Contagem | Quantas vezes o campo foi visto desde a primeira observação (acumulado, sem janela). Como a detecção amostra uma fração dos eventos, esta contagem reflete os eventos amostrados — o volume real é proporcionalmente maior. |
| Visto por último | Data e hora do evento mais recente que trouxe esse campo. |
| Status | Novo, ignorado ou já mapeado. |
| Ação | Ignorar ou marcar como mapeado. |

## O que fazer com um campo novo

Quando um campo novo aparece, você tem três caminhos.

### Opção A: ignorar o campo

Use quando o campo é mesmo irrelevante (por exemplo, um identificador interno do fornecedor que não agrega à investigação).

1. Na tela **Normalização -> Drift Explorer**, localize o campo.
2. Use a ação de **ignorar** na linha do campo.
3. O status muda para **ignorado** e o campo deixa de aparecer na lista de novos. Os dados originais continuam preservados, caso você precise revisar depois.

### Opção B: marcar como mapeado (já tratado)

Use quando você já ajustou a regra de mapeamento para aproveitar o campo e quer apenas tirá-lo da lista de pendências.

1. Anote o campo que apareceu em **Normalização -> Drift Explorer**.
2. Vá ao menu **Normalização -> Mappings** e abra a regra de mapeamento do fornecedor correspondente.
3. No editor de mapeamento, inclua o campo desejado e salve. O CentralOps cria uma nova versão da regra.
4. Volte a **Normalização -> Drift Explorer** e use a ação de **marcar como mapeado** na linha do campo.
5. O status passa a **mapeado** e o campo sai da lista de pendências.

### Opção C: incluir o campo nos eventos normalizados

Use quando o campo é importante e você quer que ele passe a aparecer em todos os eventos daqui para frente. O fluxo é o mesmo da Opção B — o ponto central é, no editor de mapeamento (**Normalização -> Mappings**), criar a regra que leva o campo de origem para um campo do evento normalizado e salvar a nova versão.

Depois de salvar:

- Os eventos **novos** já passam a incluir o campo.
- Para recuperar eventos antigos que ficaram retidos por falta desse campo, reprocesse-os pela tela **Normalização -> Quarentena**. Veja [Quarentena](../operations/quarantine.md).

## Filtros disponíveis

A tela **Normalização -> Drift Explorer** permite filtrar a lista para focar no que importa:

- **Por fornecedor** — responde perguntas como "quais campos novos a Sophos começou a mandar?".
- **Por status** — **novo** (visto mas ainda não tratado), **ignorado** (você decidiu descartar) ou **mapeado** (você já incorporou).
- **Por tipo** — texto, número, verdadeiro/falso, lista ou estrutura aninhada.

## Ações em massa

Quando muitos campos novos aparecem de uma vez (por exemplo, após uma grande atualização do fornecedor), filtre por fornecedor e status **novo** e aplique a ação a todos os campos selecionados — ignorar todos de uma vez, ou marcar todos como mapeados caso você já tenha ajustado as regras de mapeamento correspondentes.

:::note
A disponibilidade das ações em massa pode variar conforme a versão da plataforma. Se você não encontrar esses controles na tela, trate os campos individualmente pela coluna **Ação**.
:::

## Exemplos de fluxo

### O fornecedor atualizou os campos enviados

1. Abra **Normalização -> Drift Explorer**.
2. Filtre por fornecedor (ex.: Sophos).
3. Revise os campos novos e identifique quais são relevantes.
4. Para os relevantes, ajuste a regra em **Normalização -> Mappings** e salve.
5. Marque esses campos como mapeados na tela Drift Explorer.
6. Se houver eventos retidos por falta desses campos, reprocesse-os em **Normalização -> Quarentena**.

### "Que dados estou perdendo?"

1. Abra **Normalização -> Drift Explorer**.
2. Ordene pela coluna **Contagem** (do mais frequente para o menos).
3. Os campos do topo são os candidatos mais fortes a serem incorporados.
4. Decida campo a campo: ignorar ou incluir no mapeamento.

### Auditoria de completude

1. Abra **Normalização -> Drift Explorer** e filtre por status **ignorado**.
2. Revise: esses campos são mesmo irrelevantes ou apenas faltou mapeá-los?
3. Para os que faltam, ajuste a regra em **Normalização -> Mappings**.

## Retenção dos registros

Os registros de campos novos têm um prazo de retenção padrão. Após esse período sem que o campo apareça de novo, o registro é removido automaticamente. Se o campo voltar a chegar depois, ele é detectado novamente como novo.

O prazo de retenção é definido pela equipe de infraestrutura no momento do deploy. Se precisar alterá-lo, fale com o administrador da plataforma.

## Próximos passos

- **Ajustar uma regra de mapeamento?** Veja [Mappings](../normalization/overview.md).
- **Muitos eventos retidos?** Veja [Solução de problemas de normalização](../normalization/troubleshooting.md).
- **Reprocessar eventos retidos?** Veja [Quarentena](../operations/quarantine.md).
