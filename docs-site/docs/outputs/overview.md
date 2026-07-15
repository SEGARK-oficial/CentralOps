---
sidebar_position: 1
title: Saídas e Roteamento (visão geral)
description: Como o CentralOps entrega o mesmo evento a vários destinos (SIEMs, data lakes, brokers) com roteamento, redação de PII e fila de reenvio por destino.
---

# Saídas e Roteamento (visão geral)

O CentralOps é um pipeline de dados de segurança que coleta eventos de várias ferramentas, padroniza esses eventos e os entrega a um ou mais **destinos** (SIEMs, data lakes, brokers de mensagem). Em vez de mandar tudo para um único lugar fixo, você decide — por regra — qual evento vai para qual destino.

O caminho de um evento é sempre o mesmo: **Coleta → Normalização → Roteamento → Destinos**.

**Quem faz o quê:**

| Papel | O que pode fazer |
|-------|------------------|
| Administrador | Criar e editar destinos e regras de roteamento. |
| Operador | Pausar e retomar destinos, acompanhar a saúde. |
| Demais perfis | Consultar a saúde e o histórico de entrega. |

## Quando usar

- **Reduzir custo de SIEM.** Você paga caro por volume no SIEM, então quer mandar só os alertas críticos para lá e jogar os eventos verbosos (atividade de arquivo, fluxo de rede) num data lake mais barato, mantendo tudo retido para investigação posterior.
- **Trocar de SIEM sem risco.** Você está migrando do SIEM atual para um novo e quer começar enviando uma fração pequena dos eventos para o novo destino, validar, e ir aumentando aos poucos até cortar 100%.
- **Atender vários clientes (MSSP).** Cada cliente tem seu próprio destino e suas próprias credenciais, e um cliente nunca pode ver os dados ou os destinos de outro.

## As quatro etapas do pipeline

### 1. Coleta

Os coletores buscam os eventos nas ferramentas de origem (por exemplo, Sophos Central e Wazuh). Você acompanha e configura os coletores no menu **Operação → Collectors** e cadastra as origens em **Visão geral → Integrações**.

### 2. Normalização

Eventos de qualquer origem são convertidos para um formato único e padronizado. Junto do evento vão **metadados internos** (plataforma, severidade, tipo de evento, origem) que o roteamento usa depois para decidir o caminho. Você gerencia as regras de conversão no menu **Normalização → Mappings**.

### 3. Roteamento

Para cada evento, o sistema decide:

- **Quais destinos** vão recebê-lo (pode ser mais de um — o mesmo evento é enviado simultaneamente a vários destinos).
- **Qual redação de PII** aplicar antes de enviar (pode ser diferente por destino).
- Se o evento deve ser **descartado** (para cortar ruído ou economizar).

As regras ficam no menu **Operação → Roteamento** (só admin).

### 4. Destinos

Cada destino é independente:

- Tem a **própria configuração** (endereço, credenciais, formato).
- Tem a **própria fila de entrega**, com novas tentativas automáticas e uma **fila de reenvio** para o que não passou na primeira vez.
- Reporta a **própria saúde** (status, vazão, latência, erros).
- Se um destino fica instável, isso **não afeta os outros**.

Os destinos ficam no menu **Operação → Destinos** (só admin).

## Tipos de destino disponíveis

O CentralOps entrega para os tipos de destino abaixo. Ao criar um destino, você escolhe o tipo e o formulário pede os campos certos para ele.

| Tipo de destino | Para que serve |
|-----------------|----------------|
| Splunk (HEC) | Enviar para um índice do Splunk. |
| Elasticsearch | Enviar para um índice do Elastic. |
| Amazon S3 | Arquivar em um bucket S3 (ótimo para data lake / retenção longa). |
| Microsoft Sentinel | Enviar para o Sentinel via tabela de ingestão. |
| Apache Kafka | Publicar em um tópico Kafka. |
| OpenTelemetry (OTLP) | Enviar a um coletor de telemetria compatível. |
| Syslog | Enviar via Syslog (formatos RFC 3164 e RFC 5424). |
| Arquivo JSONL | Gravar em arquivo local, um evento por linha. |
| Webhook genérico | Integrar com SOAR, automação ou endpoints HTTP customizados. |
| Datadog (Logs) | Centralizar observabilidade de segurança no Datadog. |
| Google SecOps (Chronicle) | Enviar para o SIEM do Google. |
| Amazon Security Lake | Arquivar em formato OCSF padronizado na AWS. |

:::note[Destino Wazuh padrão]
O destino **Wazuh** já vem pronto e entrega exatamente como na versão anterior do produto, sem mudança de comportamento. Ele aparece na lista de destinos como qualquer outro. Veja [Destino Wazuh](./destination-wazuh-syslog.md).
:::

Para a lista completa com os detalhes de cada tipo, veja o [Catálogo de Destinos](./destinations.md).

## Conceitos principais

### Destino

Um destino é onde os eventos chegam. Ao cadastrá-lo, você informa:

| Item | O que é |
|------|---------|
| Tipo | O tipo de destino (Splunk, Elastic, S3, etc.). |
| Configuração | Endereço, credenciais e formato. As credenciais ficam guardadas de forma cifrada e nunca aparecem em texto na tela. |
| Entrega | Como o destino agrupa e reenvia os eventos (veja abaixo). |
| Teste de conexão | Um botão que valida as credenciais e confirma que o destino está acessível antes de salvar. |

#### Opções de entrega

Cada destino tem ajustes de entrega no próprio formulário. Em linguagem de produto, eles controlam:

| Ajuste | O que faz |
|--------|-----------|
| Tamanho do lote | Quantos eventos juntar antes de enviar de uma vez. |
| Tentativas automáticas | Quantas vezes reenviar quando há falha temporária. |
| Tempo limite | Quanto esperar pela resposta do destino antes de considerar falha. |
| Envios simultâneos | Quantos lotes podem estar em trânsito ao mesmo tempo (aumentar acelera, dentro do que o destino aguenta). |
| Proteção contra destino instável | Se o destino começa a falhar muito, o sistema pausa os envios por um tempo e tenta de novo depois, em vez de insistir e piorar. |

:::tip
Cada destino tem a **própria fila de reenvio**. Se um destino rejeita um evento, ele vai para a fila de reenvio **daquele destino** — não é perdido e não atrapalha os outros destinos.
:::

### Regra de roteamento

Uma regra diz quais eventos vão para quais destinos. Você monta as regras no menu **Operação → Roteamento** (só admin). Cada regra tem:

| Campo | O que faz |
|-------|-----------|
| Prioridade | Ordem em que as regras são avaliadas (a de número menor é avaliada primeiro). |
| Condição | O filtro que define a quais eventos a regra se aplica (por exemplo, eventos do Sophos com severidade alta). |
| Ação | Enviar o evento aos destinos escolhidos, ou descartá-lo. |
| Destinos | Quais destinos recebem o evento. |
| Regra final | Se marcada, o evento para nesta regra. Se não, o evento é copiado e continua sendo avaliado pelas próximas (é assim que se manda o mesmo evento a vários destinos). |
| Percentual gradual (canary) | Aplica a regra só a uma fração dos eventos (por exemplo, 10%). Serve para liberar uma mudança aos poucos. |

**Exemplo de conjunto de regras:**

1. **Prioridade 10** — eventos com severidade alta vão para Wazuh **e** Sentinel ao mesmo tempo; não é regra final, então o evento continua.
2. **Prioridade 20** — eventos de atividade de arquivo são descartados (corte de ruído).
3. **Prioridade 30** — qualquer outro evento vai para o Wazuh (regra final de garantia, para nada se perder).

Para montar regras passo a passo, veja o [Guia de Roteamento](./routing.md). Para liberar uma mudança aos poucos, veja [Roteamento gradual (canary)](./routing-canary.md).

### Redação de PII por regra

Cada regra pode esconder ou remover campos sensíveis (como nome de usuário ou IP de origem) **antes** de enviar ao destino. Como a redação é por regra, o mesmo evento pode chegar:

- **Completo** ao data lake (S3), para você ter o dado íntegro guardado.
- **Mascarado** ao SIEM, onde você não precisa do dado sensível em texto claro.

Isso permite guardar o dado íntegro num destino barato e mandar uma versão reduzida para o destino caro. Veja [Redação de PII](./pii-redaction.md).

### Fila de reenvio por destino

Quando um destino rejeita um evento, ele não é descartado: vai para a **fila de reenvio** daquele destino. O tratamento depende do motivo:

| Situação | O que acontece |
|----------|----------------|
| Evento grande demais para o destino | É rejeitado e fica na fila de reenvio, para ser reprocessado depois de reduzir o tamanho. |
| Credencial inválida ou expirada | Não adianta repetir; o evento fica retido até você corrigir a credencial. |
| Falha temporária do destino (sobrecarga, indisponibilidade momentânea) | O sistema tenta de novo sozinho, esperando cada vez um pouco mais entre as tentativas. |

Você acompanha o que está retido no menu **Normalização → Quarentena**.

### Saúde de cada destino

Cada destino tem o próprio painel de saúde, com:

- **Status**: saudável, degradado ou inativo.
- **Vazão**: eventos por segundo e volume de dados.
- **Taxa de sucesso e erro**: percentual de eventos aceitos e rejeitados.
- **Fila**: tamanho da fila e se está acumulando.
- **Último sucesso e último erro**: quando cada um aconteceu.

Para ver o painel de um destino, vá a **Operação → Destinos**, abra o destino e veja a aba de saúde. Para a visão geral de todo o pipeline, use **Normalização → Saúde do Pipeline**. Veja também [Observabilidade](./observability.md).

## Configuração rápida

### 1. Criar um destino

1. Vá a **Operação → Destinos** e inicie a criação de um novo destino.
2. Escolha o tipo na lista — a interface pede apenas os campos necessários para aquele tipo.
3. Preencha o endereço, as credenciais e o formato.
4. Use o **Teste de conexão** para confirmar que está acessível.
5. Salve.

### 2. Criar uma regra de roteamento

1. Vá a **Operação → Roteamento** e inicie uma nova regra.
2. Dê um nome claro (por exemplo, "Sophos crítico para Sentinel").
3. Defina a prioridade.
4. Defina a condição (por exemplo, eventos do Sophos com severidade alta).
5. Escolha os destinos.
6. Marque como regra final se ela for exclusiva.
7. Salve.

### 3. Conferir a saúde

1. Vá a **Normalização → Saúde do Pipeline** para ver o status de cada destino.
2. Para detalhes de um destino específico (vazão, fila, erros recentes), abra-o em **Operação → Destinos**.

## O que acontece quando um evento é processado

Em linguagem de produto, sem detalhes técnicos:

1. **Coleta** — o coletor busca o evento na ferramenta de origem.
2. **Normalização** — o evento é convertido para o formato padrão e ganha os metadados internos que o roteamento usa.
3. **Roteamento** — as regras são avaliadas por prioridade e definem para quais destinos o evento vai e qual redação aplicar a cada um.
4. **Entrega** — para cada destino, o evento é agrupado em lote, formatado no formato daquele destino e enviado em segundo plano, com novas tentativas em caso de falha temporária.
5. **Resultado** — eventos aceitos aparecem na vazão do destino; eventos rejeitados vão para a fila de reenvio e ficam visíveis em **Normalização → Quarentena**.

## Casos de uso detalhados

### Tiering econômico

- Alertas críticos (severidade alta) vão para o **SIEM** (Sentinel), com retenção curta e dados mascarados.
- Eventos verbosos (atividade de arquivo, fluxo de rede) vão para o **data lake** (S3), com retenção longa e dados íntegros.
- Todo o resto vai para o **Wazuh** padrão, como garantia.

Resultado: você reduz o gasto com o SIEM caro e mantém tudo retido no armazenamento barato.

### Migração gradual de SIEM

- Comece enviando uma fração pequena dos eventos ao novo SIEM (por exemplo, 10%), mantendo o restante no destino atual.
- Acompanhe a saúde do novo destino e aumente a fração aos poucos.
- Quando estiver confiante, corte 100% para o novo destino.

Tudo isso é feito ajustando o percentual gradual da regra — sem refazer a configuração. Veja [Roteamento gradual (canary)](./routing-canary.md).

### Isolamento por cliente (MSSP)

- Cada cliente tem seu próprio destino, com as próprias credenciais.
- Um cliente nunca vê os destinos ou as regras de outro.

## Resolução de problemas

### O evento não chega ao destino

1. Em **Operação → Roteamento**, revise a condição da regra — ela realmente bate com o evento que você espera?
2. Em **Operação → Destinos**, abra o destino e use o **Teste de conexão**.
3. Em **Normalização → Saúde do Pipeline**, verifique se o destino está com a fila acumulando.
4. Em **Normalização → Quarentena**, veja se o evento foi retido e por quê.

### A fila do destino está crescendo

A fila cresce quando a entrega está mais lenta do que a chegada de eventos.

1. Abra o destino em **Operação → Destinos** e veja o tamanho da fila na aba de saúde.
2. Aumente os **envios simultâneos** na configuração de entrega do destino, dentro do que o destino consegue aguentar.
3. Verifique se o destino está respondendo: use o **Teste de conexão** no próprio destino para conferir se ele está acessível e respondendo rápido.

### A fila de reenvio está crescendo

Muitos eventos estão sendo rejeitados.

1. Em **Normalização → Quarentena**, filtre pelo destino e veja o motivo da rejeição.
2. Se os eventos forem grandes demais, reduza o tamanho do envio — aplique mais redação de PII ou filtre eventos por regra.
3. Se for falha de credencial, use o **Teste de conexão** do destino e atualize a credencial.

## Próximos passos

- [Catálogo de Destinos](./destinations.md) — todos os tipos e seus campos.
- [Destino Wazuh](./destination-wazuh-syslog.md) — configurar o destino padrão.
- [Splunk HEC](./destination-splunk-hec.md) — configurar o Splunk.
- [Guia de Roteamento](./routing.md) — montar regras de roteamento.
- [Redação de PII](./pii-redaction.md) — esconder dados sensíveis por regra.
- [Observabilidade](./observability.md) — acompanhar a saúde da entrega.
- [Normalização](../normalization/overview.md) — como os eventos são padronizados.
