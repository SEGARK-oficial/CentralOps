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
| **Última coleta** | Há quanto tempo a plataforma **conversou com o fornecedor** pela última vez sem erro (`—` = nunca coletou). Responde "a coleta está rodando?" — e só isso. |
| **Atraso dos dados** | De quando é o evento **mais recente que a coleta já trouxe**. Responde "o que eu estou vendo é de agora ou de ontem?". **A linha não aparece** quando não é medível (veja abaixo). |
| **Backlog** | Etiqueta amarela ao lado do status. Significa que o último ciclo terminou no **teto de eventos por ciclo**, ou seja, sobrou fila para o ciclo seguinte. |
| **Campos novos (24h)** | Quantos CAMPOS o fornecedor enviou nas últimas 24h que o mapeamento ainda não aproveita — não é contagem de eventos com erro. Sinal de que há contexto disponível sendo descartado. Veja [Campos novos (drift)](../pipelines/drift.md). |
| **Quarentena (24h)** | Eventos retidos por falha de mapeamento, validação ou ocultação de dados sensíveis. |

### "Quando rodou" e "de quando é o dado" são coisas diferentes

Esses são os dois números mais confundidos da tela, e a diferença entre eles é a origem de um incidente real.

| | **Última coleta** | **Atraso dos dados** |
|---|---|---|
| Pergunta que responde | "A plataforma falou com o fornecedor agora há pouco?" | "Até que ponto do passado do fornecedor nós já chegamos?" |
| Zera quando… | Qualquer ciclo termina sem erro — **mesmo que esse ciclo tenha processado eventos de ontem** | Só quando a coleta realmente alcança o presente do fornecedor |
| Fica alto quando… | A coleta parou, falhou ou está pausada | A coleta está rodando, mas **não vence o volume** |

:::warning[Um coletor 15 horas atrasado já apareceu como "Saudável"]
Em julho de 2026, uma integração Wazuh estava processando eventos criados **15 horas antes** e o card marcava **atraso de 0 s** e status **Saudável** — porque cada ciclo terminava sem erro, e era só isso que o número media. O SOC só descobriu quando um analista estranhou o horário dos alertas no destino.

O **Atraso dos dados** existe exatamente para fechar esse ponto cego. Se ele estiver em horas enquanto a Última coleta está em segundos, a coleta **está funcionando e mesmo assim você está vendo o passado**. O roteiro completo está em [Eventos chegando horas depois](../runbooks/collection-lag-backlog.md).
:::

:::note[Linha ausente não quer dizer "em dia"]
A linha **Atraso dos dados** **some do card** quando a plataforma não consegue medi-la: o fornecedor usa um marcador de posição que não é uma data (paginação opaca), ou nenhum ciclo gravou posição ainda.

Ela some em vez de mostrar `0` ou `—` de propósito: os dois se leem como "está tudo em dia", que é justamente a mentira que causou o incidente acima. Ausência da linha significa **"não sei"**, e a etiqueta **Backlog** continua aparecendo mesmo nesses casos — é o único sinal disponível para essas fontes.
:::

### Integração com vários fluxos: vale o pior

Uma integração pode coletar mais de um fluxo (por exemplo, detecções e auditoria). O **Atraso dos dados** e o **Backlog** do card são sempre os do **pior fluxo** — o mais atrasado, e "sim" se qualquer um deles bateu o teto.

Não é média nem soma, e a razão é prática: numa média, um fluxo 15 horas atrasado desaparece atrás de três fluxos em dia — a forma exata do incidente. O card responde "o pipeline desta integração está em dia?", e a resposta é não se qualquer fluxo não estiver.

A **Última coleta**, por contraste, é o do fluxo que coletou **mais recentemente**. São perguntas diferentes: um pergunta "alguma coleta terminou agora?", o outro "até onde nós chegamos?".

### Como ler o status

O status é decidido **em ordem**: vale a primeira regra que se aplica, e as seguintes nem chegam a ser avaliadas.

| Ordem | Status | Quando aparece |
|---|---|---|
| 1 | **Aguardando primeira coleta (cinza)** | A integração nunca coletou com sucesso. Não há métrica para julgar. |
| 2 | **Indisponível (vermelho)** | A última **coleta bem-sucedida** foi há **mais de 5 minutos**, **ou** houve **3 ou mais falhas seguidas** de coleta. |
| 3 | **Degradado (amarelo)** | **Backlog confirmado** (as duas condições abaixo), **ou** existe um **erro registrado**. Em ambos os casos a coleta não parou — mas alguma coisa não está certa. |
| 4 | **Saudável (verde)** | Nenhum dos casos acima. |

Como a avaliação é nessa ordem, uma integração que tem erro registrado **e** atraso de coleta acima de 5 minutos aparece como **Indisponível**, nunca como Degradado.

### Backlog confirmado exige as DUAS condições

Para o card ficar amarelo por backlog, as duas coisas precisam ser verdade ao mesmo tempo:

1. O último ciclo terminou no **teto de eventos por ciclo** (sobrou fila), **e**
2. o **Atraso dos dados desse mesmo fluxo** passa de **30 minutos**.

Nenhuma das duas serve sozinha, e não é rigor gratuito:

- **Atraso dos dados alto, sem teto atingido** = fluxo sem eventos no período. O marcador fica parado de propósito, e isso é normal. Se isso pintasse o card de amarelo, metade da frota — todo fluxo silencioso — ficaria amarela e o indicador seria ignorado na primeira semana.
- **Teto atingido, sem atraso dos dados** = um pico que o teto absorveu dentro da janela, ou seja, o teto fazendo exatamente o que existe para fazer.

Os 30 minutos equivalem a cerca de **dez ciclos seguidos batendo o teto sem recuperar terreno**. Um pico que drena em um ou dois ciclos não acende nada. O coletor do incidente que originou o indicador estava 15 horas atrás — trinta vezes o limite.

:::info[Backlog é amarelo, não vermelho — e a diferença é a sua reação]
**Vermelho** quer dizer "parou de coletar", e a reação é credencial, rede, token.
**Amarelo por backlog** quer dizer "coleta, mas atrasada", e a reação é outra: reduzir o que se coleta com um [filtro de coleta](../pipelines/collection-filters.md), ou pedir mais capacidade ao administrador da plataforma.

Um fluxo que está **parado e atrasado** cai antes na regra 2 (falhas ou atraso de coleta) e aparece vermelho de qualquer jeito.
:::

Existem, portanto, **dois limites de tempo** na tela, e eles medem coisas diferentes: **5 minutos** de Última coleta ("parou de coletar") e **30 minutos** de Atraso dos dados ("está para trás") — este último só conta acompanhado do teto atingido.

:::info[Quarentena e campos novos não mudam a cor do card]
Os contadores **Drift (24h)** — os campos novos — e **Quarentena (24h)** aparecem no card, mas são **informativos**: nenhum dos dois entra no cálculo do status. Um evento em quarentena, sozinho, não deixa o card amarelo; mil campos novos não deixam o card vermelho.

Isso importa especialmente agora: a detecção de campos novos passou a comparar **caminho a caminho** dentro do evento, e o contador subiu de forma expressiva em várias integrações. Se um card ficou amarelo ou vermelho, a causa é atraso de coleta, erro na coleta ou backlog confirmado — não o número de campos novos.
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

1. O card está vermelho: a **Última coleta** passou de 5 minutos, ou houve 3 falhas seguidas.
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
2. Todas as integrações em verde? A coleta está rodando.
3. Passe os olhos no **Atraso dos dados** de cada card, mesmo nos verdes: é o número que diz se o que você está vendo é de agora. Minutos, tudo bem; horas **com a etiqueta Backlog**, investigue. Horas **sem** Backlog é fonte sem eventos no período — o normal em fluxos com [filtro de coleta](../pipelines/collection-filters.md) ligado.
4. Todos os destinos em verde? Não há risco de perda de eventos.
5. Qualquer amarelo ou vermelho: clique no card para investigar.

### "Os eventos estão chegando com horas de atraso"

1. Compare os dois atrasos no card: **Última coleta** em segundos e **Atraso dos dados** em horas é a assinatura clássica — a coleta roda, mas não vence o volume.
2. Veja se o card marca **Backlog**. Marcando, o coletor termina cada ciclo no teto e sobra fila.
3. Se sim, o caminho é reduzir o volume coletado na origem — veja [Filtro de coleta](../pipelines/collection-filters.md) — ou pedir mais capacidade ao administrador.
4. O diagnóstico completo, com o passo a passo e a alavanca de emergência, está em [Eventos chegando horas depois](../runbooks/collection-lag-backlog.md).

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
- **Atraso dos dados**: só existe para fontes cuja posição de coleta é uma data. Fontes com paginação opaca simplesmente não exibem essa linha; não é defeito.
- **Sem histórico de atraso**: o Atraso dos dados é o valor **agora**. A tela não guarda a curva dele — para acompanhar tendência, use a métrica exportada `collector_watermark_lag_seconds` (veja [Observabilidade](./observability.md)).
- **Acesso**: operadores veem todas as métricas. Administradores também veem os erros detalhados e as ações de correção.

## Próximos passos

- **Integração com problema?** Vá a **Visão geral → Integrações** ([Integrações](../integrations/overview.md)).
- **Eventos chegando com horas de atraso?** Veja [Eventos chegando horas depois](../runbooks/collection-lag-backlog.md) e [Filtro de coleta](../pipelines/collection-filters.md).
- **Evento em quarentena?** Vá a **Normalização → Quarentena** ([Quarentena](./quarantine.md)).
- **Quer rotear eventos seletivamente?** Vá a **Operação → Roteamento** ([Roteamento](../outputs/routing.md)).
- **Quer configurar novos destinos?** Vá a **Operação → Destinos** ([Destinos](../outputs/destinations.md)).
