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
| Campo | Caminho COMPLETO do campo recebido, incluindo o aninhamento (ex.: `data.win.eventdata.logonType`, `rule.mitre.id`). A detecção compara caminho a caminho, então campos novos dentro de estruturas já mapeadas também aparecem. |
| Fornecedor | Plataforma de origem (Sophos, Defender, etc.). |
| Tipo | Tipo inferido do valor (texto, número, verdadeiro/falso, lista ou estrutura aninhada). |
| Contagem | Quantas vezes o campo foi visto desde a primeira observação (acumulado, sem janela). Como a detecção amostra uma fração dos eventos, esta contagem reflete os eventos amostrados — o volume real é proporcionalmente maior. |
| Visto por último | Data e hora do evento mais recente que trouxe esse campo. |
| Valor de amostra | Uma descrição do FORMATO do valor (`<ipv4>`, `<email>`, `<timestamp>`, `<string len=42>`), não o valor em si. É o suficiente para escolher o campo OCSF de destino sem guardar dado do cliente. |
| Status | Novo, ignorado ou já mapeado. |
| Ação | Ignorar ou marcar como mapeado. |

## O que é guardado como amostra

:::info[A amostra é mascarada por padrão]
A coluna **Valor de amostra** não guarda o valor recebido: ela guarda um **classificador de formato** — `<ipv4>`, `<email>`, `<uuid>`, `<timestamp>`, `<path_win>`, `<sha256>`, `<string len=42>`. Isso é o bastante para você decidir para qual campo do evento normalizado apontar aquele dado, sem armazenar o dado do cliente.

Por que a diferença importa: campo **não mapeado** é justamente onde caem nome de usuário, host, endereço IP, caminho de arquivo e linha de comando. E o registro não é passageiro — a tela é visível para o perfil **Viewer** e o registro dura o prazo de retenção (90 dias por padrão).

Se a sua organização tiver base legal para ver o conteúdo real, peça ao administrador da plataforma para ajustar `DRIFT_SAMPLE_VALUE_MODE`: `raw` guarda o valor real truncado em 200 caracteres, e `none` não guarda amostra nenhuma. O padrão é `masked`, e o ajuste vale para a plataforma inteira, não por organização.
:::

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

O prazo padrão é de **90 dias por organização**. Passado esse período **sem que o campo apareça de novo**, o registro é removido pela limpeza automática diária. Se o campo voltar a chegar depois, ele é detectado outra vez como novo.

Esse prazo **não é travado no deploy**: ele é por organização e vale qualquer valor entre 1 e 3650 dias. Ainda **não há tela** para editá-lo — hoje o ajuste é feito pelo administrador via API (`PATCH /api/organizations/{id}/retention`). Veja [Retenção de dados](../compliance/retention.md).

:::warning[Campo que continua chegando nunca expira]
A contagem do prazo parte da coluna **Visto por último**, e essa data é reescrita a cada nova ocorrência do campo. Ou seja: enquanto o fornecedor continuar mandando o campo, o registro é renovado indefinidamente e nunca vence. Ele só desaparece depois de o prazo inteiro passar **sem** o campo aparecer nenhuma vez.

Consequência prática: um campo recorrente mantém o que estiver na coluna **Valor de amostra** guardado por tempo indeterminado. É esse o motivo de a amostra vir mascarada por padrão.
:::

## Próximos passos

- **Ajustar uma regra de mapeamento?** Veja [Mappings](../normalization/overview.md).
- **Muitos eventos retidos?** Veja [Solução de problemas de normalização](../normalization/troubleshooting.md).
- **Reprocessar eventos retidos?** Veja [Quarentena](../operations/quarantine.md).
- **Mudar por quanto tempo os registros ficam guardados?** Veja [Retenção de dados](../compliance/retention.md).
