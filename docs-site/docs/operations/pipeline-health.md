---
sidebar_position: 6
title: Saúde do Pipeline
description: Acompanhe em tempo real a saúde da coleta de eventos e da entrega aos destinos
---

# Saúde do Pipeline

A tela **Saúde do Pipeline** mostra, em um único painel, se os eventos estão sendo **coletados** das suas integrações e **entregues** aos destinos configurados — para você detectar problemas antes que virem perda de dados.

Você a encontra no menu **Normalização → Saúde do Pipeline**.

## Quando usar

- **Ronda matinal do SOC**: abrir a tela no início do turno para confirmar que toda coleta e toda entrega estão saudáveis antes de começar o dia.
- **"Parei de ver eventos de um fabricante"**: um analista nota que alertas de um produto sumiram e precisa saber se o problema está na coleta (integração parada) ou na entrega (destino fora do ar).
- **Risco de perda de dados**: a fila de reenvio de um destino está crescendo e a equipe precisa decidir se age agora ou escala para o administrador.

## Visão geral

O painel tem **duas seções**:

1. **Saúde por integração** — um card por integração com eventos por minuto, atraso, campos novos detectados, eventos em quarentena e status.
2. **Saúde por destino** — uma lista com o estado e as métricas de cada saída configurada.

As duas seções refletem a coleta do último minuto e o histórico das últimas 24 horas.

## Saúde por integração

### O que cada card mostra

| Campo | Descrição |
|-------|-----------|
| **Nome** | Nome da integração (ex.: "Sophos - Acme"). |
| **Status** | Saudável, Degradado, Indisponível ou Aguardando primeira coleta. |
| **Eventos/min** | Taxa média de eventos coletados nos últimos 5 minutos (vazio = ainda não houve coleta). |
| **Atraso** | Tempo desde a última **coleta bem-sucedida** (vazio = nunca coletou). Não é o tempo desde o último evento: um ciclo que consulta o fornecedor e não traz nada zera o atraso do mesmo jeito. |
| **Campos novos (24h)** | Quantos CAMPOS o fornecedor enviou nas últimas 24h que o mapeamento ainda não aproveita — não é contagem de eventos com erro. Sinal de que há contexto disponível sendo descartado. Veja [Campos novos (drift)](../pipelines/drift.md). |
| **Quarentena (24h)** | Eventos retidos por falha de mapeamento, validação ou ocultação de dados sensíveis. |

### Como ler o status

O status é decidido **em ordem**: vale a primeira regra que se aplica, e as seguintes nem chegam a ser avaliadas.

| Ordem | Status | Quando aparece |
|---|---|---|
| 1 | **Aguardando primeira coleta (cinza)** | A integração nunca coletou com sucesso. Não há métrica para julgar. |
| 2 | **Indisponível (vermelho)** | A última **coleta bem-sucedida** foi há **mais de 5 minutos**, **ou** houve **3 ou mais falhas seguidas** de coleta. |
| 3 | **Degradado (amarelo)** | Existe um **erro registrado** e o atraso ainda está **dentro dos 5 minutos**: a coleta não parou, mas alguma coisa falhou no caminho. |
| 4 | **Saudável (verde)** | Nenhum dos casos acima — coletando, sem erro registrado e com atraso de até 5 minutos. |

Como a avaliação é nessa ordem, uma integração que tem erro registrado **e** atraso acima de 5 minutos aparece como **Indisponível**, nunca como Degradado. Só existe esse único limite de tempo: 5 minutos. Não há faixa intermediária de atraso.

:::info[Quarentena e campos novos não mudam a cor do card]
Os contadores **Drift (24h)** — os campos novos — e **Quarentena (24h)** aparecem no card, mas são **informativos**: nenhum dos dois entra no cálculo do status. Um evento em quarentena, sozinho, não deixa o card amarelo; mil campos novos não deixam o card vermelho.

Isso importa especialmente agora: a detecção de campos novos passou a comparar **caminho a caminho** dentro do evento, e o contador subiu de forma expressiva em várias integrações. Se um card ficou amarelo ou vermelho, a causa é atraso de coleta ou erro na coleta — não o número de campos novos.
:::

### Filtrar integrações

Use os botões acima dos cards:

- **Todos** — mostra todas (padrão).
- **Saudáveis** — apenas as verdes.
- **Com problema** — junta tudo que não está verde: Degradado, Indisponível e Aguardando primeira coleta.

### Investigar uma integração com problema

1. Clique no card da integração para abrir o painel de detalhes.
2. No painel você vê:
   - O último erro registrado (ex.: "401 Unauthorized", "429 Too Many Requests").
   - O gráfico de coleta das últimas 24 horas.
   - O histórico de campos novos e quarentena.
   - As ações de **pausar** ou **retomar** a coleta.
   - A ação de **testar a conexão** novamente.

**Exemplo: integração parada**

1. O card está vermelho: o atraso passou de 5 minutos, ou houve 3 falhas seguidas.
2. Abra os detalhes e leia o último erro.
3. Se for **"401 Unauthorized"**, as credenciais expiraram. Vá ao menu **Visão geral → Integrações** ([Integrações](../integrations/overview.md)), abra a integração e atualize as credenciais.
4. Se for **"429 Too Many Requests"**, o fabricante limitou a taxa de chamadas. A frequência de coleta é definida na configuração da integração — reduza-a se o erro persistir.
5. Use **testar a conexão**. Se passar, foi um erro passageiro: clique em **retomar**.

## Saúde por destino

### O que é um destino

Um destino é uma saída configurada para receber os eventos roteados: Wazuh, Splunk, Elastic, Syslog, S3, entre outros.

Cada destino é **independente** — a falha de um não afeta os outros. Enquanto um destino está fora do ar, os eventos ficam guardados na **fila de reenvio** e são entregues assim que ele volta, sem perda.

### O que a lista de destinos mostra

Abra a seção **Saúde por destino** para ver cada saída:

| Campo | Descrição |
|-------|-----------|
| **Nome** | Nome do destino (ex.: "Wazuh padrão", "Splunk Prod"). |
| **Status** | Saudável, degradado, com problema, ou desativado. |
| **Eventos/s** | Eventos enviados por segundo na última hora. |
| **Volume** | Throughput em bytes por minuto na última hora. |
| **Fila de reenvio** | Quantidade de eventos aguardando reentrega após falha. |
| **Proteção do destino** | Indica se a proteção automática contra destino instável está ativa. |

### Como ler o status

- **Saudável (verde)**: destino conectado, taxa de envio normal, sem falhas recentes.
- **Degradado (amarelo)**: latência alta, fila de reenvio crescendo, ou desconexões esporádicas.
- **Com problema (vermelho)**: destino fora do ar, falhas persistentes, ou muitos eventos na fila de reenvio.
- **Desativado (cinza)**: destino desabilitado — não recebe eventos.

### Proteção contra destino instável e fila de reenvio

Quando um destino começa a falhar repetidamente, o CentralOps ativa automaticamente uma **proteção contra destino instável**: em vez de insistir em um destino que está fora do ar, os novos eventos são desviados para a **fila de reenvio**.

- Enquanto a proteção está ativa, os eventos seguem para a fila de reenvio em segurança, sem se perderem.
- O CentralOps testa o destino periodicamente por conta própria. Quando ele volta a responder, a proteção é desligada e os eventos da fila de reenvio são entregues automaticamente.
- Você não precisa ligar ou desligar essa proteção — ela é automática. Sua função é corrigir a causa raiz (credencial, certificado, rede) para que o destino volte.

### Investigar um destino com problema

1. Clique no card do destino para abrir os detalhes.
2. No painel você vê:
   - As últimas falhas, com mensagem de erro e horário.
   - A taxa de reentrega.
   - O histórico de envio das últimas 24 horas.
   - A ação de **reprocessar a fila de reenvio**.
   - As ações de **desabilitar** ou **habilitar** o destino.

**Exemplo: destino fora do ar**

1. O destino está vermelho e marcado como desconectado.
2. Abra os detalhes e leia o erro (ex.: "Connection refused", "TLS certificate expired").
3. Se for um destino tipo Splunk, confirme com o responsável o token e o endereço do destino.
4. Se for um problema de rede, certificado ou firewall entre o CentralOps e o destino, fale com o administrador da plataforma ou com a equipe de rede — esses ajustes ficam fora da interface do CentralOps.
5. Depois de corrigido, a proteção é desligada automaticamente em poucos segundos. Use **reprocessar a fila de reenvio** para entregar os eventos que ficaram acumulados.

## Casos de uso passo a passo

### Ronda matinal

1. Abra **Normalização → Saúde do Pipeline**.
2. Todas as integrações em verde? A coleta está em dia.
3. Todos os destinos em verde? Não há risco de perda de eventos.
4. Qualquer amarelo ou vermelho: clique no card para investigar.

### "Não vejo eventos no Wazuh"

1. Confira se a integração de origem está em verde (coletando).
2. **Se está coletando, mas o Wazuh não recebe:**
   - Verifique se o destino Wazuh está em verde.
   - Se estiver vermelho, provavelmente a proteção está ativa, a fila de reenvio está cheia, ou há problema de certificado.
   - Corrija a causa e use **reprocessar a fila de reenvio**.
3. **Se a integração está em vermelho (não coleta):**
   - Veja o último erro (401? 429? tempo esgotado?).
   - Teste a conexão. Se falhar por credencial, atualize-a em **Visão geral → Integrações**.

### "Eventos em quarentena"

1. O card de integração mostra "Quarentena (24h)" maior que zero.
2. Abra os detalhes para ver os eventos recentes.
3. Causa comum: um campo obrigatório do padrão de normalização ficou faltando, ou o fabricante enviou um valor inválido.
4. Vá ao menu **Normalização → Quarentena** para revisar, reprocessar ou descartar esses eventos. Veja [Quarentena](./quarantine.md).

### "A fila de reenvio de um destino está crescendo"

1. O destino está amarelo ou vermelho e a fila de reenvio passou de 100 eventos.
2. Abra os detalhes para ver as últimas falhas.
3. Se a proteção contra destino instável estiver ativa: corrija a causa no destino (token expirado? endereço errado?) e aguarde a proteção se desligar sozinha.
4. Se a proteção não estiver ativa, mas a fila continua crescendo, o destino pode estar lento ou limitando a taxa de recebimento. Esse tipo de ajuste de tempo de espera e limite de envio é definido pela equipe de infraestrutura no momento do deploy. Se precisar alterá-lo, fale com o administrador da plataforma.
5. Use **reprocessar a fila de reenvio** quando o destino estiver saudável de novo.

## Limitações

- **Atualização**: as métricas atualizam a cada 1 a 2 minutos — não é tempo real.
- **Histórico**: o card mostra apenas as últimas 24 horas. Para tendências de longo prazo, exporte os eventos para um destino de análise.
- **Acesso**: operadores veem todas as métricas. Administradores também veem os erros detalhados e as ações de correção.

## Próximos passos

- **Integração com problema?** Vá a **Visão geral → Integrações** ([Integrações](../integrations/overview.md)).
- **Evento em quarentena?** Vá a **Normalização → Quarentena** ([Quarentena](./quarantine.md)).
- **Quer rotear eventos seletivamente?** Vá a **Operação → Roteamento** ([Roteamento](../outputs/routing.md)).
- **Quer configurar novos destinos?** Vá a **Operação → Destinos** ([Destinos](../outputs/destinations.md)).
