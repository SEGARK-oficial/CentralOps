---
sidebar_position: 16
title: Observabilidade dos destinos
description: Onde acompanhar saúde, taxa de envio (EPS), latência, rejeições e fila de reenvio de cada destino pela interface do CentralOps.
---

# Observabilidade dos destinos

O CentralOps mostra, em tempo real, como cada destino está recebendo os seus eventos: quantos eventos por segundo saem, quanto tempo levam para chegar, quantos foram rejeitados e quantos estão aguardando reenvio. Tudo isso aparece direto na interface, sem precisar de ferramenta externa.

## Quando usar

- **Um destino parou de receber eventos.** Você percebe que o SIEM externo está "vazio" e quer confirmar se o problema é no CentralOps (envio caiu) ou no destino. Abra a saúde do destino e veja a taxa de envio e o último envio bem-sucedido.
- **Latência subindo / fila crescendo.** Durante um pico de alertas, você quer saber se os eventos estão atrasando ou empilhando na fila de reenvio antes que alguém reclame.
- **Validar uma mudança de roteamento.** Depois de alterar uma regra de roteamento, você confere quantos eventos estão de fato caindo em cada destino e se as rejeições aumentaram.

## Onde olhar na interface

| O que você quer ver | Onde encontrar |
|---|---|
| Visão consolidada de todos os destinos e do funil de eventos | menu **Visão geral -> Dashboard** |
| Saúde detalhada de um destino específico (EPS, latência, rejeições, fila) | menu **Operação -> Destinos** (apenas admin) |
| Como os eventos estão sendo distribuídos entre destinos | menu **Operação -> Fluxo de dados** (apenas admin) |
| Saúde do processamento que normaliza os eventos antes do envio | menu **Normalização -> Saúde do Pipeline** |

:::note
As telas **Destinos**, **Roteamento** e **Fluxo de dados** só aparecem para usuários administradores. Operadores de SOC acompanham a saúde geral pelo **Dashboard** e pela **Saúde do Pipeline**.
:::

---

## O que cada número significa

Ao abrir a saúde de um destino, você verá um conjunto de indicadores. Use esta tabela como referência rápida:

| Indicador | O que mede | Quando se preocupar |
|---|---|---|
| **EPS (eventos por segundo)** | Ritmo atual de envio ao destino (janelas de 1, 5 e 60 min) | Caiu para zero sem motivo, ou despencou em relação ao normal |
| **Enviados na última hora** | Total de eventos aceitos pelo destino | Muito abaixo do esperado para o volume da operação |
| **Rejeitados** | Eventos recusados pelo destino (autenticação, tamanho ou formato) | Subindo de forma contínua |
| **Latência média** | Tempo entre o evento sair do CentralOps e ser aceito pelo destino | Acima do normal por períodos longos |
| **Fila de reenvio** | Eventos que falharam e estão aguardando nova tentativa | Crescendo sem parar (indica destino com problema) |
| **Estado do destino** | Se o destino está saudável ou sob proteção contra destino instável | Sinalizado como instável / bloqueado |
| **Último envio bem-sucedido** | Quando o destino recebeu o último evento com sucesso | Muito tempo atrás enquanto há eventos chegando |

### Termos que aparecem na tela

- **EPS** — eventos por segundo. É a medida principal de "quanto está fluindo agora".
- **Fila de reenvio** — quando um destino recusa ou está indisponível, os eventos não são descartados: vão para uma fila e o CentralOps tenta enviar de novo automaticamente.
- **Proteção contra destino instável** — se um destino começa a falhar muito, o CentralOps para de bombardeá-lo por um tempo e tenta de novo mais tarde, para não perder eventos nem sobrecarregar o destino. Quando isso acontece, o destino aparece marcado nessa condição.
- **Rejeitados** — diferente de "fila de reenvio". Rejeitado é quando o destino respondeu recusando o evento (por exemplo, credencial inválida ou evento grande demais). Esses casos normalmente exigem ajuste de configuração do destino.

---

## Acompanhando os eventos recentes de um destino

Na tela de um destino, além dos números agregados, você pode inspecionar os **últimos eventos despachados** para conferir o conteúdo que está saindo. Os dados sensíveis (tokens, senhas) aparecem mascarados — você nunca vê uma credencial em texto puro.

Use isso para confirmar, por exemplo, que os eventos certos estão indo para o destino certo depois de uma mudança de roteamento.

---

## Quando agir

| Sintoma na tela | Provável causa | Próximo passo na interface |
|---|---|---|
| EPS em zero, mas há eventos chegando | Destino indisponível ou credencial expirada | Abra **Operação -> Destinos**, verifique o estado e o último erro do destino |
| Rejeições subindo | Formato/tamanho/credencial do destino | Reveja a configuração do destino em **Operação -> Destinos** |
| Fila de reenvio crescendo | Destino instável ou fora do ar | Aguarde a recuperação automática; se persistir, acione o destino. Você pode reprocessar a fila de reenvio na tela de **Destinos** quando o destino voltar |
| Latência alta de forma contínua | Destino lento ou rede congestionada | Confirme com o time responsável pelo destino |
| Eventos não normalizam (não chegam a sair) | Problema antes do envio | Verifique **Normalização -> Saúde do Pipeline** e **Normalização -> Quarentena** |

:::tip
Antes de concluir que o CentralOps deixou de enviar, confira o **Último envio bem-sucedido** e a **fila de reenvio**. Fila crescendo + EPS caindo quase sempre significa problema no **destino**, não no CentralOps.
:::

---

## Monitoramento avançado de infraestrutura

Além dos painéis de produto que você vê na interface, o CentralOps pode exportar métricas e registros técnicos para ferramentas de monitoramento de infraestrutura usadas pelo time de TI/SRE (por exemplo, painéis corporativos de observabilidade). Isso serve para a equipe de operações acompanhar a plataforma em nível de servidor, não para o uso diário do operador de SOC.

Essa exportação é definida pela equipe de infraestrutura no momento do deploy. Se precisar habilitá-la ou direcioná-la para uma ferramenta específica, fale com o administrador da plataforma. Para o trabalho do dia a dia, tudo que você precisa já está nas telas de **Dashboard**, **Destinos** e **Saúde do Pipeline**.

---

## Próximos passos

- **Conferir onde os eventos estão indo:** menu **Operação -> Fluxo de dados** (apenas admin).
- **Eventos que não normalizaram:** veja [Quarentena](../operations/quarantine.md).
- **Saúde geral da plataforma:** menu **Visão geral -> Dashboard**.
