---
sidebar_position: 5
title: Fila de reenvio e entrega a destinos
description: Diagnostique e reprocesse eventos que falharam ao serem entregues a um destino, direto pela interface.
---

# Fila de reenvio e entrega a destinos

Quando um evento já normalizado não consegue ser entregue a um destino (por exemplo Splunk, Amazon S3 ou Microsoft Sentinel), o CentralOps não o descarta: ele guarda o evento na **fila de reenvio** daquele destino. Esta página mostra como inspecionar essa fila, descobrir por que a entrega falhou e reprocessar os eventos pela interface, sem perder nada.

:::info[Quem tem acesso]
As telas de **Destinos**, **Roteamento** e **Fluxo de dados** só aparecem para administradores da plataforma. Se você não as vê no menu, peça ao administrador para conduzir os passos abaixo.
:::

## Quando usar

Recorra a esta página quando, na operação do SOC, você perceber alguma destas situações:

- **Um destino ficou fora do ar e voltou.** O Sentinel (ou o coletor Splunk) passou horas indisponível durante a madrugada. Os eventos não se perderam, ficaram na fila de reenvio. Depois que o destino volta, você reprocessa a fila para entregar o que ficou represado.
- **Faltam eventos em uma ferramenta de busca externa.** Um analista relata que alertas de um conector pararam de aparecer no Splunk a partir de certo horário. Você verifica a saúde do destino e descobre rejeições acumuladas na fila de reenvio.
- **Trocou-se uma credencial ou um certificado.** Um token de envio expirou e as entregas começaram a ser recusadas por erro de autenticação. Após o administrador atualizar a credencial, você reprocessa a fila para reenviar o que falhou nesse intervalo.

## Onde fica na interface

Todo o trabalho desta página acontece no menu **Operação -> Destinos**. Abra a tela, localize o card do destino em questão e use as abas de **Saúde** e de **fila de reenvio** desse card.

> A fila de reenvio é a fase final do caminho de um evento (a entrega). Ela é diferente da **Quarentena**, que captura problemas em uma fase anterior (a normalização). Veja a comparação no fim desta página.

## Sintoma 1: "Eventos não chegaram a um destino"

### Como diagnosticar

1. Vá a **Operação -> Destinos** e abra o card do destino.
2. Na aba **Saúde**, confira o status:

| Status | O que significa |
|---|---|
| Saudável | Conectado e entregando, sem rejeições recentes. |
| Degradado | Conectado, mas com rejeições nas últimas 24 horas. |
| Instável | A proteção contra destino instável foi acionada — o destino está sendo tratado como offline. |
| Desligado | O destino está desativado e não recebe eventos. |

3. Olhe o indicador de **rejeições nas últimas 24h**:
   - Se for maior que zero, vá para o **Sintoma 2** para descobrir o motivo.
   - Se for zero, mas mesmo assim você suspeita que eventos não chegaram, verifique os itens abaixo.

### Quando o contador está zerado mas faltam eventos

- **O destino está ligado?** Na aba **Saúde**, o status não pode estar como Desligado. Se estiver, reative o destino (próximo bloco).
- **Existe uma rota apontando para este destino?** Vá a **Operação -> Roteamento** e confirme que há pelo menos uma regra que envia eventos a este destino. Sem rota, nenhum evento é encaminhado e nada chega à fila de reenvio.
- **Os eventos estão sendo coletados?** Vá a **Visão geral -> Dashboard** e confira o volume de eventos normalizados recentes. Se a coleta na origem parou, o problema é anterior à entrega.

### Como resolver

- **Destino desligado:** no card do destino em **Operação -> Destinos**, reative o destino pelo controle de ativação do próprio card.
- **Sem rota:** crie uma regra em **Operação -> Roteamento** que direcione os eventos desejados para este destino.

## Sintoma 2: "Rejeições com um erro específico"

### Como diagnosticar

1. Vá a **Operação -> Destinos** e abra o card do destino.
2. Abra a aba da **fila de reenvio**.
3. Observe o agrupamento por **tipo de erro** — o CentralOps classifica cada falha em uma categoria. Use a tabela abaixo para entender a causa e o caminho de solução:

| Tipo de erro | Causa | O que fazer |
|---|---|---|
| Evento grande demais | O evento ultrapassa o tamanho máximo aceito pelo destino — quase sempre porque o payload bruto do fornecedor é enorme. | Corte o payload bruto no mapeamento (bloco `raw_reduction`) **ou** ligue **Descartar o evento bruto** na rota daquele destino. Veja o aviso logo abaixo da tabela. |
| Formato recusado | O destino rejeitou o formato do evento (campo com tipo inválido, estrutura inesperada). | Revise o mapeamento e o formato esperado pelo destino. Antes de reativar o tráfego, use o teste de pré-visualização descrito na seção **Prevenção**. |
| Autenticação | Credencial inválida ou expirada. | Peça ao administrador para atualizar a credencial do destino. Confira também a configuração de certificado/TLS. |
| Transporte | Problema de rede: tempo esgotado ou conexão recusada. | Valide a conectividade até o destino e use o **teste de conexão** do card. |
| Proteção acionada | A proteção contra destino instável está aberta porque o destino ficou indisponível por muito tempo. | Aguarde a recuperação automática (em geral poucos minutos) ou faça um novo teste de conexão. |
| Tentativas esgotadas | O CentralOps tentou reenviar várias vezes e a falha persistiu. | Investigue o lado do destino ou da rede. Quando o problema for resolvido, reprocesse a fila (Sintoma 5). |
| Sem rota (`unrouted`) | O evento não bateu em nenhuma regra de roteamento e não há catch-all configurado, então não tinha para onde ir. | Em **Operação -> Roteamento**, crie uma regra pega-tudo (condição vazia) ou marque um destino como padrão — qualquer destino pode assumir esse papel. Depois reprocesse a fila (Sintoma 5). Veja [Evento foi para o destino errado (ou nenhum)](./routing-misroute.md). |
| Não classificado | Erro fora das categorias acima. | Abra o evento para ver o detalhe do erro e ganhar contexto. |

4. Clique em um evento da fila para ver o conteúdo (já com dados sensíveis ocultos) e o detalhe específico do erro.

:::warning[Não use a redação de PII para diminuir o tamanho do evento]

A redação de PII existe para **conformidade**, não para controle de tamanho, e é *fail-closed*: se a regra de mascaramento não puder ser aplicada, a entrega ao destino real é bloqueada. Ligá-la para "encolher" o evento troca um problema de tamanho por uma parada de entrega.

Para reduzir o tamanho de verdade, há duas alavancas:

- **No mapeamento — vale para todos os destinos.** O bloco `raw_reduction` poda o payload bruto: `max_bytes` para blobs longos, `drop` / `keep_only` para subárvores que já foram extraídas para o evento normalizado, `drop_nulls` para chaves vazias. Veja [Especificação da DSL](../normalization/dsl-spec.md).
- **Na rota — vale para a entrega daquela regra (todos os destinos dela).** Marque **Descartar o evento bruto**: a entrega daquela rota vai sem o bloco bruto e com o evento normalizado (OCSF) preservado. Não faz efeito enquanto **Proteger detecção** estiver ligada na rota (é o padrão) — desligue a proteção primeiro, conscientemente.

O caso agudo é o **Wazuh**: o `analysisd` **trunca silenciosamente** eventos acima de ~64 KiB (`OS_MAXSTR`) — o evento chega, mas cortado, sem erro nenhum para você notar.

Panorama das alavancas em [Redução de volume e custo](../outputs/reducao-de-volume.md).

:::

## Sintoma 3: "Muitos eventos na fila de reenvio"

### Como diagnosticar

1. Em **Operação -> Destinos**, abra o card do destino.
2. Na aba **Saúde**, veja o total acumulado e o total das últimas 24 horas na fila de reenvio.
3. Na aba da **fila de reenvio**, observe a distribuição por **tipo de erro**. Normalmente um único tipo concentra a maior parte das falhas — esse é o problema a atacar primeiro.

**Próximo passo:** identifique o tipo de erro predominante e aplique a ação correspondente da tabela do **Sintoma 2**.

## Sintoma 4: "Destino perdeu a conectividade"

### Como diagnosticar

1. Em **Operação -> Destinos**, abra o card do destino e vá à aba **Saúde**.
2. Verifique o estado da proteção contra destino instável:
   - **Aberta:** o destino está sendo tratado como offline e as entregas estão sendo seguradas.
   - **Em teste:** o CentralOps está sondando o destino para ver se já voltou.
3. Use o **teste de conexão** do card do destino. Um resultado positivo mostra que a conexão e a latência estão saudáveis. Se o teste falhar, verifique:
   - O endereço (host/URL) configurado está correto?
   - A rede consegue alcançar o destino?
   - A credencial ou token ainda é válida?
   - O certificado (TLS) do destino é válido?

### Como resolver

- **Credencial expirada:** peça ao administrador para atualizar a credencial do destino. A configuração de credenciais e certificados é tratada na própria tela do destino pelo administrador.
- **Problema de rede:** acione a equipe responsável pela rede ou pelo destino.
- **Certificado inválido:** o pacote de certificados (CA) usado para validar o destino é definido pela equipe de infraestrutura no momento do deploy. Se precisar alterá-lo, fale com o administrador da plataforma.

Depois que a conectividade for restabelecida, siga para o **Sintoma 5** para reprocessar o que ficou retido.

## Sintoma 5: "Preciso reprocessar os eventos da fila de reenvio"

### Antes de reprocessar, confirme

1. **O problema de fundo já foi resolvido** (credencial atualizada, destino online de novo, rede restabelecida). Reprocessar antes disso só vai gerar novas rejeições.
2. **O reenvio é seguro.** O CentralOps identifica cada evento de forma única, então reprocessar não duplica eventos que já tinham sido entregues.

### Como reprocessar

1. Vá a **Operação -> Destinos** e abra o card do destino.
2. Abra a aba da **fila de reenvio**.
3. Escolha o escopo:
   - **Todos os eventos da fila:** use a ação de reprocessar a fila inteira do destino.
   - **Eventos selecionados:** marque os eventos desejados na lista e reprocesse apenas eles.
4. Confirme. O reprocessamento roda em segundo plano — você não precisa manter a tela aberta.

### Como acompanhar o resultado

- Permaneça na aba da **fila de reenvio**: o contador de eventos pendentes diminui conforme o reprocessamento avança.
- Quando o contador zera, todos os eventos foram reentregues com sucesso.
- Se ainda restarem eventos na fila ou novas rejeições aparecerem, o problema de fundo não foi totalmente resolvido. Volte aos sintomas 2 e 4 para investigar o tipo de erro remanescente.

## Prevenção

### 1. Valide o formato antes de ligar um destino novo

Antes de enviar tráfego real, use o teste de pré-visualização disponível no card do destino (em **Operação -> Destinos**). Ele mostra como o evento ficará ao ser enviado, permitindo confirmar formato e tamanho sem arriscar uma enxurrada de rejeições.

### 2. Teste a conexão

Use o **teste de conexão** do card do destino sempre **antes** de direcionar tráfego de produção a ele.

### 3. Acompanhe a saúde dos destinos

- Use a tela **Operação -> Destinos** para ver o status de cada destino de forma contínua.
- Para uma visão de ponta a ponta do caminho dos eventos até os destinos, use **Operação -> [Fluxo de dados](../operations/fluxo-de-dados.md)**.
- Defina alertas para situações como rejeições acima de um limite diário, proteção contra destino instável acionada, ou destino sem fluxo de entrada. A criação e o ajuste desses alertas é feito junto à equipe de infraestrutura.

### 4. Política de retenção da fila de reenvio

Os eventos na fila de reenvio são preservados para fins de auditoria e investigação. Periodicamente:

- Revise os tipos de erro e os detalhes dos eventos retidos na aba da **fila de reenvio**.
- Reprocesse ou descarte os eventos depois de confirmar que a causa foi resolvida.

## Fila de reenvio x Quarentena

São duas filas diferentes, em fases diferentes do caminho do evento. Saber qual usar evita procurar o evento no lugar errado.

| Aspecto | Fila de reenvio | Quarentena |
|---|---|---|
| Fase | Entrega ao destino (etapa final) | Normalização (etapa inicial) |
| Origem do problema | Destino recusou ou não recebeu o evento | Falha ao interpretar/normalizar o evento de entrada |
| Exemplos de erro | Evento grande demais, autenticação, formato recusado, transporte | Evento bruto inválido, campo obrigatório ausente |
| Onde inspecionar | **Operação -> Destinos**, aba da fila de reenvio do destino | **Normalização -> Quarentena** |
| Quem costuma tratar | Administrador do destino e equipe de operação | Quem cuida de integrações e mapeamentos |

## Se o problema persistir

Se, mesmo após os passos acima, o destino continuar falhando:

1. **Reúna as evidências pela interface:** anote o status na aba **Saúde**, o tipo de erro predominante e o detalhe de alguns eventos na aba da **fila de reenvio**, além de quando as falhas começaram e o que já foi tentado.
2. **Acione o suporte da plataforma** pelo canal de suporte da sua organização, anexando as informações acima.
3. **Verifique o lado do destino:** se as evidências apontam para o próprio destino (Splunk, Elasticsearch, Sentinel, etc.), acione também o administrador desse sistema.

## Próximos passos

- **Problema na normalização?** Veja [Quarentena e normalização](../operations/quarantine.md).
- **Configurar para onde os eventos vão?** Veja [Roteamento de eventos](../outputs/routing.md).
- **Visão geral da saúde dos destinos?** Veja [Saúde do Pipeline](../operations/pipeline-health.md).
