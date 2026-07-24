---
sidebar_position: 12
title: Roteamento por regra
description: Decida, por regra, para quais destinos cada evento vai — ou se é descartado — com prioridade, envio simultâneo e descarte de ruído.
---

# Roteamento por regra

O **Roteamento** decide, para cada evento que passa pelo CentralOps, **qual ou quais destinos vão recebê-lo** — ou se ele deve ser descartado. As decisões são tomadas por **regras** que você cria, com base em características do evento (fornecedor, severidade, tipo, organização, geografia etc.). Cada regra tem uma **prioridade**, e as regras são avaliadas em ordem.

Com o roteamento você consegue **enviar o mesmo evento para vários destinos ao mesmo tempo**, **migrar tráfego aos poucos** de um destino para outro, e **descartar ruído** para reduzir custo no SIEM.

> Esta é uma tela **só de administrador**. Você a encontra no menu **Operação → Roteamento**. Analistas e operadores podem consultar e simular regras; criar, editar e reordenar regras exige perfil de administrador.

---

## Quando usar

- **Controle de custo no SIEM.** Você quer mandar só os eventos de alta severidade para um SIEM caro (ex.: Microsoft Sentinel) e jogar os eventos verbosos e de baixo valor para um data lake barato — ou descartá-los de vez.
- **Migração gradual entre destinos.** O SOC vai trocar de SIEM e não quer um corte "big bang". Você começa enviando 10% dos eventos críticos para o destino novo, observa por alguns dias e vai aumentando até 100%.
- **Isolamento por cliente ou residência de dados.** Em um cenário MSSP/multi-tenant, cada cliente precisa que seus eventos vão só para o destino dele; ou eventos de origem na União Europeia precisam ficar em um destino na UE (GDPR/LGPD).

---

## Conceitos-chave

### Regra de roteamento

Uma **regra** mapeia eventos para um ou mais destinos. Ao criar ou editar uma regra na tela de **Operação → Roteamento**, você define:

| Campo | O que significa |
|-------|-----------------|
| **Nome** | Rótulo legível para a regra (ex.: "Sophos crítico → Sentinel"). |
| **Prioridade** | Ordem de avaliação — quanto menor o número, mais cedo a regra é avaliada. Deixe espaços entre os números (10, 20, 30…) para conseguir encaixar regras novas depois. |
| **Condição** | As características que o evento precisa ter para "bater" na regra. Uma condição vazia bate em todos os eventos (regra "pega-tudo"). |
| **Ação** | **Enviar** (manda o evento para os destinos escolhidos) ou **Descartar** (apaga o evento). |
| **Destinos** | Os destinos que recebem o evento quando a ação é Enviar. |
| **Exclusiva** | Quando ligada, a avaliação **para** assim que a regra bate (o evento vai só para esta regra). Quando desligada, o evento também segue para as próximas regras — é assim que se faz o **envio simultâneo a vários destinos**. |
| **Percentual gradual** | De 0 a 100. A regra se aplica só a essa fração dos eventos que batem nela — o resto segue para as próximas regras. Use para migração aos poucos. 100 = regra aplicada a todos. |
| **Ativa** | Liga ou desliga a regra sem precisar apagá-la (útil para testar). |
| **Redação de PII** | Remove ou mascara campos sensíveis (ex.: nome de usuário, IP de origem) **antes** de enviar o evento aos destinos desta regra. |
| **Proteger detecção** | **Ligada por padrão.** Enquanto estiver ligada, esta regra **nunca** é amostrada, suprimida nem tem o evento bruto descartado — as alavancas de economia são ignoradas nela. Desligue apenas nas regras onde reduzir volume é seguro. |
| **Amostragem** | De 0 a 100. Entrega só essa fração dos eventos aos destinos da regra; o resto é economizado. Ignorada enquanto **Proteger detecção** estiver ligada. |
| **Supressão** | Rate-limit por assinatura: deixa passar N eventos "iguais" por janela de tempo e economiza o excedente. Veja **Reduzir ruído repetitivo** abaixo. |
| **Descartar o evento bruto** | Remove o payload original do fornecedor da entrega **desta** regra, preservando o evento normalizado (OCSF). Deixe desligado na regra do data lake, onde o bruto é necessário para perícia, e ligue no SIEM cobrado por volume — costuma ser a maior economia isolada. |

### Características que você pode usar na condição

A condição de uma regra compara características do evento já normalizado. As principais:

| Característica | O que é | Exemplos |
|---------------|---------|----------|
| **Fornecedor** | O fornecedor da integração de origem. | `sophos`, `wazuh`, `microsoft_defender` |
| **Plataforma** | A plataforma de origem (quando difere do fornecedor). | `sophos`, `microsoft_defender` |
| **Organização** | A organização/tenant a que o evento pertence. | uma das suas organizações |
| **Severidade** | A severidade normalizada do evento, em escala crescente. | `5`, `8`, `10` |
| **Fluxo** | O fluxo de dados do fornecedor. | `alerts`, `cases`, `sysmon` |
| **Tipo de evento** | A categoria do evento. | `process_activity`, `network_activity`, `file_activity` |
| **Cliente** | Identificador do cliente/tenant no sistema. | usado em cenários multi-tenant |
| **Geografia** | A região de origem do evento. | `US`, `EU`, `APAC`, `global` |

---

## Como a condição funciona

Ao montar a condição, você escolhe uma característica e como ela deve ser comparada. Quando você usa mais de uma característica na mesma regra, **todas precisam ser verdadeiras ao mesmo tempo** para o evento bater na regra.

As comparações disponíveis incluem:

| Comparação | O que faz |
|------------|-----------|
| **Igual a** | A característica é exatamente o valor informado. |
| **Diferente de** | A característica não é o valor informado. |
| **Maior que / Maior ou igual** | Para valores numéricos, como severidade. |
| **Menor que / Menor ou igual** | Para valores numéricos. |
| **Está na lista** | A característica é um dos valores informados. |
| **Não está na lista** | A característica não é nenhum dos valores informados. |
| **Existe / Não existe** | A característica está presente (ou ausente) no evento. |

A tela **valida a condição enquanto você digita**: se você escolher uma característica inexistente ou uma comparação inválida, recebe um aviso na hora e a regra não pode ser salva até corrigir. Os destinos também são checados — você só consegue apontar para destinos que existam e estejam visíveis para você.

---

## Como as regras são avaliadas

Para cada evento, o CentralOps percorre as regras na ordem:

1. **Por prioridade.** As regras são avaliadas da menor prioridade para a maior. Regras **desativadas** são puladas.
2. **Percentual gradual.** Se a regra está configurada para se aplicar a só uma parte dos eventos, o evento pode "cair" para a próxima regra. Eventos iguais sempre recebem o mesmo tratamento, então o comportamento é estável e previsível.
3. **Ação.** Se a regra **descarta**, o evento é apagado e a avaliação termina ali. Se a regra **envia**, o evento é encaminhado aos destinos da regra.
4. **Exclusiva ou não.** Se a regra for **exclusiva**, a avaliação para. Se não for, o evento também segue para as próximas regras — permitindo que ele chegue a vários destinos.
5. **Rede de segurança (catch-all que você configura).** Um evento que não bate em nenhuma regra (ou bate numa regra sem destinos) vai para o **catch-all** — e o catch-all é definido por você, não preso a nenhum produto. Há duas formas de configurá-lo, e qualquer destino (Elastic, S3, Splunk, Sentinel, syslog, Wazuh…) pode ser o catch-all:
   - uma **regra pega-tudo** (condição vazia, menor prioridade, exclusiva); ou
   - marcar um **destino como padrão** (`is_default`) — o padrão da sua organização tem precedência sobre um padrão global compartilhado.

   Se um evento não bate em regra nenhuma **e não há catch-all configurado**, ele **não some nem vai para um destino oculto**: é enviado à fila de retentativa/quarentena (DLQ) com o motivo **`unrouted`**, onde fica visível e pode ser **reprocessado** depois que você criar a regra pega-tudo ou marcar um destino como padrão. Eventos de **fonte Wazuh** são a exceção: são suprimidos para evitar loop infinito (veja Anti-loop: Wazuh como fonte e destino em [Wazuh](../integrations/wazuh.md)).

### Exemplo de fluxo

Imagine três regras, nesta ordem de prioridade:

1. **Severidade ≥ 8 → enviar para "Sentinel"** (não exclusiva)
2. **Tipo de evento = file_activity → descartar** (exclusiva)
3. **Pega-tudo → enviar para o destino-padrão** (exclusiva)

| Evento | Regra 1 | Regra 2 | Regra 3 | Destinos finais |
|--------|---------|---------|---------|-----------------|
| Severidade 9, process_activity | bate → Sentinel, continua | não bate | bate → destino-padrão | Sentinel **e** destino-padrão |
| Severidade 4, file_activity | não bate | bate → descartado | — | nenhum (evento apagado) |
| Severidade 4, network_activity | não bate | não bate | bate → destino-padrão | destino-padrão |

---

## Migração gradual de destino

Quando você precisa migrar eventos de um destino para outro sem corte abrupto, use o **percentual gradual** de uma regra:

1. **Comece pequeno.** Crie a regra apontando para o destino novo com percentual em **10%**. Só 10% dos eventos que batem na regra vão para o destino novo; os outros 90% seguem para as regras seguintes (e continuam indo para o destino antigo). Acompanhe por 1–2 dias.
2. **Aumente.** Suba o percentual para **50%** — metade dos eventos em cada destino. Observe.
3. **Conclua.** Suba para **100%** — migração completa.

Como o tratamento é determinístico, um mesmo evento sempre recebe a mesma decisão, então a transição é estável e auditável.

---

## Descarte de ruído (controle de custo)

Uma regra com ação **Descartar** apaga os eventos que batem nela sem enviá-los a nenhum destino. Use para cortar eventos verbosos e de baixo valor que só encarecem o SIEM (por exemplo, fluxos de rede em massa ou resumos de processo).

Os eventos descartados são **contados à parte** nas métricas da regra, então você sempre enxerga quanto está sendo cortado e pode confirmar que nenhum descarte está amplo demais.

---

## Reduzir ruído repetitivo (supressão)

Enquanto o **Descartar** apaga tudo que bate na condição, a **supressão** deixa passar uma amostra e economiza o excesso: ela agrupa eventos "iguais" por uma assinatura e entrega apenas os N primeiros de cada grupo por janela de tempo.

Três campos definem o comportamento:

| Campo | O que faz |
|---|---|
| **Chave de supressão** | Quais características formam a assinatura do grupo. |
| **Quantos passam** | Quantos eventos de cada assinatura são entregues por janela. `0` desliga a supressão. |
| **Janela (segundos)** | De quanto em quanto tempo a contagem reinicia. |

### A chave de supressão só aceita características de roteamento

Esta é a parte que mais gera engano. A assinatura é montada a partir das **mesmas características usadas na condição da regra** — aquelas da tabela em "Características que você pode usar na condição": fornecedor, plataforma, organização, severidade, fluxo, tipo de evento, cliente e geografia.

**Campos do log não valem.** `src_ip`, `agent.name`, `rule.id` e afins **não** existem nesse vocabulário. Se você tentar usá-los, a plataforma recusa a configuração com erro — antes, ela aceitava em silêncio e o resultado era desastroso: todos os eventos caíam na mesma assinatura e praticamente **todo o tráfego era descartado**.

:::warning[Escolha a chave com cuidado]
Uma chave **grossa demais** (por exemplo, só `fornecedor`) agrupa todo o tráfego da integração num único balde — e você economiza quase tudo, inclusive o que precisava ver. Prefira combinações que distingam de verdade, como `fornecedor + severidade`, e confirme o efeito na **Captura ao vivo** antes de aumentar a janela.
:::

Exemplo: com chave `fornecedor, severidade`, "quantos passam" = 10 e janela de 60 segundos, cada combinação de fornecedor e severidade entrega no máximo 10 eventos por minuto; o excedente é economizado e contabilizado como volume evitado.

A supressão **preserva sempre a primeira ocorrência** de cada grupo na janela — você nunca perde o primeiro sinal de um evento novo.

### O fail-safe de detecção

Supressão, amostragem e descarte do evento bruto são **ignorados** em regras com **Proteger detecção** ligada (o padrão). Isso é proposital: uma regra que alimenta detecção não perde evento por decisão de economia sem que alguém desligue a proteção conscientemente.

---

## Redação de PII por regra

Uma regra pode **remover ou mascarar campos sensíveis** antes de enviar o evento aos destinos dela. Por exemplo, uma regra que manda eventos da UE para um data lake pode mascarar o nome do usuário e remover o IP de origem.

O efeito é **isolado àquela regra**: o evento chega ao destino daquela regra já sem os campos sensíveis, enquanto os outros destinos (de outras regras) continuam recebendo o evento completo.

---

## Operações na tela de Roteamento

Tudo é feito na tela **Operação → Roteamento**:

| O que você quer fazer | Como fazer na tela |
|-----------------------|--------------------|
| **Ver as regras** | A tela lista todas as regras, com prioridade, status (ativa/inativa) e indicador de saúde. |
| **Criar uma regra** | Use a opção de nova regra e preencha nome, prioridade, condição, ação e destinos. A condição é validada enquanto você digita. |
| **Editar uma regra** | Abra a regra na lista e altere os campos. Toda alteração fica registrada no histórico. |
| **Reordenar prioridades** | Arraste as regras na lista para mudar a ordem de avaliação. |
| **Simular antes de salvar** | Use a opção de simulação ("dry-run") para testar regras candidatas contra eventos recentes e ver para onde cada um iria, **sem salvar nada**. |
| **Ver métricas de uma regra** | Abra a regra para ver o gráfico de eventos que bateram, foram enviados e foram descartados, ao longo do tempo. |
| **Ver histórico e desfazer** | Cada regra tem um histórico de alterações; você pode restaurar uma versão anterior a partir dele. |

> O **catch-all é configurado por você**, não imposto pelo sistema: crie uma regra pega-tudo (condição vazia, menor prioridade) ou marque um destino como padrão. Sem catch-all configurado, eventos não-roteados não somem — vão à DLQ com o motivo `unrouted` (visíveis e reprocessáveis). A garantia de zero perda vem dessa rede DLQ, não de uma regra de sistema oculta.

---

## Quem pode fazer o quê

- **Criar, editar, apagar e reordenar regras** exige perfil de **administrador**.
- Cada administrador trabalha sobre as regras **da sua organização** mais as regras globais do sistema.
- **Tudo fica auditado**: cada alteração registra quem fez, quando, e a versão anterior — e você pode restaurá-la.
- Ao restaurar uma versão antiga, o sistema **revalida** a regra (por exemplo, um destino pode ter sido apagado no intervalo).

---

## Acompanhar o roteamento

Cada regra acompanha três contagens ao longo do tempo:

- **Bateram**: eventos que satisfizeram a condição.
- **Enviados**: eventos que efetivamente saíram da regra para os destinos.
- **Descartados**: eventos apagados pela regra.

Além desses, um evento pode terminar em outros desfechos — todos visíveis na **Captura ao vivo** (**Configurações → Captura ao vivo**), que é a tela que mostra o destino final de cada evento:

- **Amostrado para fora**: descartado pela amostragem da rota (`sample_percent` abaixo de 100).
- **Suprimido**: descartado pelo rate-limit por assinatura da rota (ver **Reduzir ruído repetitivo** abaixo).
- **Sem rota**: nenhuma regra casou e não há destino padrão — vai para a fila de reenvio.
- **Bloqueado por residência**: o par evento/destino foi excluído por conflito de residência de dados.
- **Loop bloqueado** (exclusivo do Wazuh): evento de fonte Wazuh que seria entregue de volta ao próprio manager, o que criaria um laço infinito (veja a nota em [Wazuh](../integrations/wazuh.md)). É uma supressão intencional, não uma perda.

Esses números aparecem no gráfico da própria regra na tela de **Operação → Roteamento**. Para ver o panorama completo do caminho dos eventos entre integrações e destinos — com vazão por destino e desenho visual do fluxo — use a tela **Operação → Fluxo de dados** (também só de administrador).

---

## Solução de problemas

### Um evento não está chegando ao destino esperado

1. Em **Operação → Roteamento**, abra a regra que deveria encaminhar o evento e confira a condição — ela está realmente batendo no tipo de evento que você espera?
2. Use a **simulação** ("dry-run") com eventos recentes para ver, passo a passo, para onde o evento iria.
3. Veja o indicador de **saúde** e o gráfico da regra: ela está recebendo eventos?
4. Em **Operação → Destinos**, verifique a saúde do destino-alvo (fila e status). O problema pode estar no destino, não na regra.
5. Confira o **histórico** da regra: ela foi alterada recentemente?

### Muitos eventos estão sendo descartados

1. Na tela **Operação → Fluxo de dados**, identifique quais regras estão descartando mais eventos.
2. Abra essas regras em **Operação → Roteamento** e veja se a condição está ampla demais.
3. Use a **simulação** com eventos recentes para confirmar o que cada regra faria.
4. Se o descarte for intencional (controle de custo), acompanhe as métricas para garantir que ele não está pegando mais do que deveria.

### Uma regra aparece como "inalcançável"

A tela marca uma regra como **inalcançável** quando outra regra de prioridade maior já "cobre" todos os casos dela — ou seja, a regra nunca chega a ser avaliada. Por exemplo, uma regra exclusiva que envia tudo de "fornecedor A ou B" sempre vai cobrir uma regra posterior só para "fornecedor A".

Não é um erro, mas é desperdício. **Ação**: reordene as regras ou revise a lógica da condição para que a regra volte a ser alcançável.

---

## Casos de uso completos

### Tiering econômico

Mande o que é crítico para o SIEM caro, descarte o ruído e jogue o resto no data lake barato:

1. **Severidade ≥ 7 → enviar para o SIEM** (não exclusiva).
2. **Tipo de evento na lista [network_flow, process_summary] → descartar**.
3. **Pega-tudo → enviar para o data lake** (exclusiva).

Resultado: eventos críticos vão para o SIEM, eventos verbosos são descartados, e todo o restante é arquivado no data lake.

### Isolamento por tenant

Garanta que os eventos de cada cliente vão só para o destino dele:

1. **Organização = Cliente A → enviar para o destino do Cliente A** (exclusiva).
2. **Organização = Cliente B → enviar para o destino do Cliente B** (exclusiva).

Resultado: nenhum evento de um cliente vaza para o destino de outro.

### Residência de dados (GDPR/LGPD)

Mantenha os eventos na região onde precisam ficar:

1. **Geografia = EU → enviar para o destino na UE** (exclusiva).
2. **Geografia = US → enviar para o destino nos EUA** (exclusiva).

Como reforço, o roteamento **bloqueia automaticamente** o envio quando a região do destino não bate com a geografia do evento — e é conservador: nunca bloqueia se não tiver a informação de geografia.

---

## Próximos passos

- **Criar sua primeira regra?** Vá a **Operação → Roteamento** e adicione uma nova regra.
- **Entender os destinos?** Veja a [visão geral de destinos](./overview.md).
- **Ver o caminho dos eventos de ponta a ponta?** Vá a **Operação → Fluxo de dados**.
- **Investigar eventos que ficaram retidos?** Veja [Quarentena e fila de reenvio](../operations/quarantine.md).
