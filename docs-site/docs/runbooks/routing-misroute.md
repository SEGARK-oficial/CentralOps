---
sidebar_position: 7
title: "Evento foi para o destino errado (ou nenhum)"
description: "Quando um evento cai no destino-padrão, vai para o destino errado, é descartado sem querer ou some — como diagnosticar e corrigir pela tela de Roteamento."
---

# Evento foi para o destino errado (ou nenhum)

O roteamento decide, regra a regra, para quais destinos cada evento vai — ou se ele é descartado. Quando um evento chega ao **destino-padrão de segurança** em vez do destino que você esperava, vai parar no lugar errado, é descartado sem querer ou simplesmente some, quase sempre a causa está em uma **regra de roteamento**: a regra está inativa, a condição não bate com o evento, ou a ordem de prioridade está atrapalhando.

Esta página é um guia de diagnóstico por sintoma. Para entender como o roteamento funciona como um todo (prioridade, regras exclusivas, descarte, percentual gradual), veja antes **[Roteamento por regra](../outputs/routing.md)**.

:::info Tela só de administrador
As telas de **Roteamento**, **Destinos** e **Fluxo de dados** ficam no grupo **Operação** do menu e só aparecem para administradores da plataforma. Se você não as vê, peça a um administrador para conduzir os passos abaixo.
:::

## Quando usar

- **Alerta sumiu do SIEM caro.** Você espera que eventos críticos da Sophos cheguem ao Sentinel, mas a equipe de plantão não os encontra lá — eles estão caindo no destino-padrão de segurança e ninguém os vê na ferramenta certa.
- **Custo do data lake disparou.** Uma regra que deveria descartar fluxos de rede ruidosos parou de bater, ou uma regra nova manda eventos demais a um destino que cobra por volume.
- **Criei uma regra e ela nunca dispara.** Você montou uma regra para um caso específico, mas o contador "Bateram" fica em zero, ou a tela marca a regra como **inalcançável**.
- **Eventos chegam a destinos a mais do que o esperado.** Um mesmo evento aparece duplicado em vários destinos porque mais de uma regra o está pegando.

## Antes de começar: o que significa cada situação

| Situação | O que está acontecendo na prática |
|----------|-----------------------------------|
| Evento cai no **destino-padrão de segurança** | O evento foi processado e não se perdeu, mas não passou por nenhuma regra específica. Ele não recebeu o enriquecimento, o mascaramento nem o destino que você configurou — o que pode causar alertas perdidos ou dados incompletos no SIEM de destino. |
| Evento vai para o **destino errado** | Uma regra está batendo no evento quando não deveria, ou a ordem de prioridade fez outra regra "pegar" o evento primeiro. |
| Evento foi **descartado** | Uma regra com ação **Descartar** bateu no evento e o apagou. Pode ser intencional (corte de ruído) ou um efeito colateral de uma condição ampla demais. |
| Regra aparece como **inalcançável** | Uma regra de prioridade maior já cobre todos os casos dessa regra, que por isso nunca chega a ser avaliada. |

Em todos os casos, a ferramenta principal de diagnóstico é a **simulação** ("dry-run") na tela de Roteamento: ela testa as regras contra eventos recentes e mostra, sem salvar nada, para onde cada evento iria.

---

## Sintoma 1: evento cai no destino-padrão em vez da regra específica

### Diagnóstico

Vá a **Operação -> Roteamento** e siga, na ordem:

1. **A regra está ativa?** Localize a regra na lista e confira o status. Se estiver inativa, regras inativas são puladas na avaliação — esse já é o motivo. Pule para a ação **1A**.
2. **A regra está batendo no evento?** Use a **simulação** ("dry-run") com eventos recentes. Para o evento que você esperava rotear, veja o resultado:
   - Se a simulação mostra o evento indo para o seu destino, a regra está OK — o problema pode ser no destino (veja [Próximos passos](#próximos-passos)).
   - Se a simulação mostra o evento caindo no destino-padrão, a condição **não casou**. Continue no passo 3.
3. **A condição corresponde ao evento real?** Abra a regra e compare a condição com as características do evento que a simulação mostrou. Os enganos mais comuns:
   - A condição espera um **fornecedor** (por exemplo, Sophos) e o evento é de outro fornecedor.
   - A condição exige uma **severidade mínima** (por exemplo, ≥ 4) e o evento tem severidade menor, então não passa.
   - A condição usa um campo com valor escrito diferente do que o evento traz.

### Ações

#### 1A. Reativar a regra

Na lista de **Operação -> Roteamento**, mude o status da regra para **ativa**. Depois, rode a **simulação** novamente com eventos recentes e confirme que o evento agora vai para o destino certo antes de considerar resolvido.

#### 1B. Corrigir a condição

Abra a regra e ajuste a condição para que ela bata no evento que você quer rotear — por exemplo, incluir o fornecedor correto, baixar o limite de severidade, ou aceitar mais de uma geografia de dados. A condição é **validada enquanto você digita**, então campos ou operadores inválidos são apontados na hora.

Depois de salvar, rode a **simulação** outra vez para confirmar que o evento passou a ser roteado como esperado. Toda alteração fica registrada no histórico da regra.

---

## Sintoma 2: a regra existe mas nunca é usada (inalcançável)

### Diagnóstico

As regras são avaliadas **da menor prioridade para a maior**. Se uma regra anterior é **exclusiva** (faz a avaliação parar no primeiro acerto) e a condição dela já cobre todos os casos da sua regra, a sua regra nunca chega a ser avaliada.

1. Em **Operação -> Roteamento**, veja se a sua regra está marcada como **inalcançável** na lista.
2. Olhe a lista na ordem de prioridade e procure uma regra anterior, **exclusiva**, cuja condição seja mais ampla e "engula" os eventos da sua regra. Exemplo: uma regra exclusiva de prioridade alta que pega "todo evento da Sophos" vai sempre cobrir uma regra posterior só para "Sophos com severidade 5".

### Ações

#### 2A. Reordenar as regras

Na lista de **Operação -> Roteamento**, **arraste** a sua regra para uma posição anterior à da regra que a estava bloqueando. As prioridades são reajustadas automaticamente conforme você reordena. Em seguida, confirme que a marca de **inalcançável** desapareceu.

> O **catch-all (destino-padrão de segurança) é configurado por você**: uma regra pega-tudo (condição vazia, menor prioridade) ou um destino marcado como padrão. Se não houver catch-all, eventos não-roteados não somem — vão à DLQ com o motivo `unrouted` (visíveis e reprocessáveis). Se existir uma regra pega-tudo, mantenha-a como a última (menor prioridade) para não sombrear as demais.

#### 2B. Tornar a regra bloqueadora não exclusiva

Se não fizer sentido reordenar, abra a regra que estava bloqueando e marque-a como **não exclusiva**. Assim, depois de enviar o evento ao destino dela, a avaliação **continua** para as próximas regras — incluindo a sua. Use isso quando você quer que o mesmo evento chegue a mais de um destino (envio simultâneo).

---

## Sintoma 3: o evento é enviado a destinos demais (duplicado)

### Diagnóstico

Um evento chega a vários destinos quando mais de uma regra bate nele e nenhuma dessas regras é **exclusiva** — ou seja, cada uma envia para o seu destino e deixa a avaliação seguir.

1. Em **Operação -> Roteamento**, abra a regra suspeita e confira se ela está marcada como **não exclusiva**.
2. Rode a **simulação** com eventos recentes e veja, para o evento em questão, **quantos destinos** ele recebe. Se forem mais do que você quer, há regras sobrepostas batendo no mesmo evento.

### Ações

#### 3A. Tornar a regra exclusiva

Se apenas **uma** regra deve processar o evento, abra-a e marque-a como **exclusiva**. A avaliação passa a parar nela, e o evento vai para um único destino. Confirme com a **simulação** que o evento agora recebe só o destino esperado.

#### 3B. Afinar as condições para evitar sobreposição

Se o envio simultâneo é desejado, mas algumas regras erradas estão batendo, mantenha as regras **não exclusivas** e restrinja a condição de cada uma para que não se sobreponham. Por exemplo, uma regra só para um fornecedor e outra só para outro fornecedor nunca batem no mesmo evento; já duas regras por faixa de severidade que se cruzam (uma "≥ 4" e outra "≥ 3") vão pegar o mesmo evento ao mesmo tempo.

---

## Sintoma 4: o evento é descartado sem querer

### Diagnóstico

Eventos descartados são apagados por uma regra com ação **Descartar**. Eles são contados à parte nas métricas da regra, então você consegue ver quanto está sendo cortado.

1. Em **Operação -> Roteamento**, procure regras com ação **Descartar** e confira a condição de cada uma.
2. Rode a **simulação** com eventos recentes e veja **quantos eventos** estão sendo descartados. Se for mais do que o esperado, identifique qual regra de descarte está batendo nos eventos que você quer preservar comparando a condição dela com as características desses eventos.
3. Para ter o panorama de quais regras mais descartam, use **Operação -> Fluxo de dados**.

### Ações

- **Desativar a regra de descarte** se ela não deveria estar agindo: mude o status para inativa na lista de **Operação -> Roteamento**.
- **Afinar a condição** se o descarte é válido, mas amplo demais: abra a regra e restrinja a condição para que ela pegue só o ruído que você realmente quer cortar (por exemplo, apenas a fonte ruidosa, com a severidade mais baixa).

Em ambos os casos, rode a **simulação** depois e confirme que a contagem de descartados voltou ao esperado.

---

## Sintoma 5: só parte das cópias do evento chega (migração gradual)

### Diagnóstico

Uma regra pode estar configurada para se aplicar a **só uma parte dos eventos** (percentual gradual). Nesse caso, apenas essa fração dos eventos que batem na regra vai para os destinos dela; o restante "cai" para a próxima regra ou para o destino-padrão. Isso é o esperado durante uma migração de destino feita com segurança.

Em **Operação -> Roteamento**, abra a regra e veja o percentual gradual configurado. Se estiver, por exemplo, em 10%, só 10% dos eventos seguem por essa regra — o que é normal no começo de uma migração.

### Ação

Se a migração já deve estar completa, suba o percentual gradualmente até **100%**:

1. Comece em **10%** e acompanhe por 1–2 dias.
2. Suba para **50%** e observe.
3. Conclua em **100%**.

Como o tratamento é determinístico, um mesmo evento sempre recebe a mesma decisão, então a transição é estável. Acompanhe o gráfico da regra (contagens **Bateram / Enviados / Descartados**) para confirmar que os eventos estão fluindo.

---

## Prevenção

Antes de salvar uma regra nova, confira na própria tela de **Operação -> Roteamento**:

- **A condição é válida?** A tela valida enquanto você digita. Use a **simulação** com eventos recentes antes de salvar.
- **O destino existe?** Selecione o destino na lista de destinos disponíveis da regra, em vez de digitar nomes à mão.
- **A ordem de prioridade está clara?** A lista mostra as regras na ordem de avaliação; arraste para ajustar.
- **Você não quer envio simultâneo?** Marque a regra como **exclusiva**.
- **Está migrando aos poucos?** Comece com o percentual gradual em **10%**.

### Monitoramento contínuo

- Acompanhe o gráfico de cada regra (contagens **Bateram / Enviados / Descartados**) na tela de **Operação -> Roteamento**.
- Para a visão de ponta a ponta — vazão por destino, regras que mais descartam e o desenho do fluxo entre integrações e destinos — use **Operação -> Fluxo de dados**.
- Toda alteração de regra fica **registrada no histórico** (quem, quando e o quê). Se uma mudança recente causou o problema, você pode abrir o histórico da regra e **restaurar uma versão anterior**. Ao restaurar, o sistema revalida a regra — por exemplo, um destino pode ter sido apagado nesse intervalo.

## Quando escalar

Se o problema continuar depois dos passos acima, acione o administrador da plataforma com estas evidências, todas obtidas na UI:

- O resultado da **simulação** ("dry-run") com eventos recentes, mostrando para onde os eventos estão indo.
- O **histórico** da regra envolvida.
- A descrição do que você esperava versus o que observou (destino esperado x destino real).

O **catch-all (destino-padrão de segurança)** é configurável na própria UI: uma regra pega-tudo (condição vazia) ou um destino marcado como padrão — qualquer destino pode assumir esse papel. Sem catch-all configurado, eventos não-roteados vão à DLQ com o motivo `unrouted` (visíveis e reprocessáveis — veja [Destino com problemas de entrega](./dlq-and-destination-delivery.md)). Outros ajustes de base do encaminhamento são definidos pela infraestrutura no deploy; se precisar alterá-los, fale com o administrador da plataforma.

## Próximos passos

- **O evento nem está sendo coletado?** Veja [Collectors](../pipelines/collectors.md).
- **O evento foi roteado mas não chegou ao destino?** Veja [Destino com problemas de entrega](./dlq-and-destination-delivery.md).
- **Quer entender o modelo de roteamento por completo?** Veja [Roteamento por regra](../outputs/routing.md).
