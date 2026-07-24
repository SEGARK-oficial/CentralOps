---
sidebar_position: 15
title: Redução de volume
description: As seis alavancas que reduzem o volume entregue — o que cada uma corta, onde se configura, se respeita a proteção de detecção e onde a economia aparece.
---

# Redução de volume

Todo evento entregue carrega, além do evento normalizado (OCSF), uma cópia do payload original do fornecedor. Isso é ótimo para perícia e caro no SIEM. A plataforma tem **seis alavancas** para reduzir esse volume, e elas agem em pontos diferentes do caminho — o que muda bastante o efeito de cada uma.

Esta página as apresenta **na ordem em que agem**. Nenhuma delas nasce cortando: por padrão, tudo passa.

## As seis alavancas, em ordem

| # | Alavanca | Onde se configura | O que corta | Respeita **Proteger detecção**? | Etiqueta no card |
|---|---|---|---|---|---|
| 1 | **Poda do bruto** | Definição JSON do mapeamento | Pedaços do payload do fornecedor, em **todos** os destinos | Não se aplica (não é config de rota) | **poda** |
| 2 | **Supressão** | Regra de rota | Repetições da mesma assinatura dentro de uma janela | Sim | **supressão** |
| 3 | **Descartar** | Regra de rota (ação) | O evento inteiro, em todos os destinos da regra | Não — é decisão explícita sua | **descarte por rota** |
| 4 | **Amostragem %** | Regra de rota | Uma fração dos eventos daquela regra | Sim | **amostragem** |
| 5 | **Descartar o evento bruto** | Regra de rota | O bloco bruto, **só na entrega daquela regra** | Sim | **descarte do bruto** |
| 6 | **Agregação** | Política de entrega do destino | Eventos repetidos, colapsados em um só | Por configuração (veja abaixo) | **agregação** |

A economia de cada uma aparece decomposta por causa no card **Redução de volume & custo**, em **Operação → Fluxo de dados**. Veja [Fluxo de dados](../operations/fluxo-de-dados.md).

:::note[Existe uma alavanca ANTES destas seis]
As seis agem sobre eventos que a plataforma **já coletou** — elas reduzem o que é **entregue**. Se o problema é a coleta não vencer o volume da origem, nenhuma delas ajuda: o evento já foi consultado, transferido e normalizado antes de ser descartado.

Para esse caso existe o **[filtro de coleta](../pipelines/collection-filters.md)**, que restringe a própria consulta feita ao fornecedor. Ele economiza **transporte**, não entrega — e, ao contrário destas seis, o que ele corta **nunca entra na plataforma**.
:::

---

## 1. Poda do bruto (no mapeamento)

**O que corta:** partes do payload original do fornecedor — a cópia bruta que viaja junto do evento normalizado. As operações disponíveis são:

| Operação | Efeito |
|---|---|
| `drop` | Remove o campo inteiro. Use no que virou lixo depois da extração. |
| `keep_only` | Mantém só os filhos que você listar e remove os demais — inclusive os que o fornecedor inventar no futuro. |
| `drop_nulls` | Remove todos os campos de valor nulo, em qualquer profundidade. |
| `max_bytes` | Encurta um texto longo. |
| `max_items` | Mantém só os primeiros itens de uma lista. |

**Onde se configura:** na definição JSON do mapeamento, no bloco de redução do bruto. Ele **não aparece no editor visual** — quem administra a plataforma edita o JSON. Veja [Especificação da DSL](../normalization/dsl-spec.md).

**Alcance:** vale para **todos os destinos**, porque é conhecimento sobre o fornecedor ("este campo é duplicata do que já extraímos"), não uma decisão sobre destino.

**Fidelidade:** a poda roda **depois** que as regras de mapeamento já leram o payload completo. A normalização nunca perde informação por causa dela.

**Proteção de detecção:** não se aplica — a poda não descarta eventos, só encolhe a cópia bruta, e não é configurada por rota.

:::note[A poda acontece mesmo com a contabilidade desligada]
Se o mapeamento declara a poda, ela é aplicada. A chave de ambiente correspondente controla apenas se a economia é **contabilizada** no card — não se o corte acontece. Se um campo sumiu do payload bruto e você não sabe por quê, o mapeamento é o primeiro lugar a checar.
:::

---

## 2. Supressão por assinatura

**O que corta:** repetições. A regra monta uma **assinatura** de cada evento e entrega apenas os N primeiros de cada assinatura por janela de tempo; o excedente é economizado.

**Onde se configura:** na regra, em **Operação → Roteamento**, seção **Redução de volume**:

| Campo | O que faz |
|---|---|
| **Chave de supressão** | Quais características formam a assinatura. |
| **Permitidos por janela** | Quantos eventos de cada assinatura passam. **0 = desligada** (é o padrão). |
| **Janela (s)** | De quanto em quanto tempo a contagem reinicia. Padrão: 30 segundos. |

**A chave só aceita as nove características de roteamento** (fornecedor, plataforma, organização, severidade, fluxo, tipo de evento, integração, cliente e geografia). Um campo do log — `src_ip`, `user`, `dst_ip` — é **recusado com erro** na hora de salvar. Também são recusadas características que são únicas por evento, como o identificador do evento e o horário de coleta: uma assinatura única por evento nunca suprimiria nada.

**Proteção de detecção:** sim. Uma regra protegida nunca suprime, mesmo que os campos estejam preenchidos.

**Garantia:** a **primeira ocorrência de cada assinatura sempre passa**. Você nunca perde o primeiro sinal de algo novo.

:::warning[A supressão é invisível nos contadores da regra]
Ela roda **antes** do roteamento. Por isso o evento suprimido não entra em **Bateram**, **Enviados** nem **Descartados** — ele nunca chegou lá. E não existe contador de supressão em nenhuma tela: o que existe é o desfecho **Suprimido**, por evento, na [Captura ao vivo](../operations/live-capture.md), e — para quem coleta métricas OpenTelemetry/Prometheus — o contador `collector_suppressed_total`, fora do produto. Em bytes, o volume evitado aparece agregado no card de redução.

Pelo mesmo motivo, a **simulação de regras** ("dry-run") não ajuda aqui: ela testa apenas se a condição casa. Ela **não** simula supressão, amostragem nem descarte do bruto.
:::

Uma chave grossa demais (só `vendor`, por exemplo) joga todo o tráfego da integração num único balde e economiza quase tudo, inclusive o que você precisava ver. Se **nenhuma** característica da chave existir no evento, a plataforma não suprime — mas o aviso fica só no log do servidor, sem nada na tela. Confirme o efeito na Captura ao vivo antes de aumentar a janela.

---

## 3. Descartar (ação da regra)

**O que corta:** o evento inteiro. A avaliação termina ali e ele não vai a destino nenhum.

**Onde se configura:** na regra, em **Operação → Roteamento**, escolhendo a ação **Descartar** em vez de **Enviar**.

**Proteção de detecção:** não. Descartar é uma decisão explícita e consciente sua, expressa na própria regra — a proteção existe para impedir que alavancas *estatísticas* comam eventos sem que ninguém tenha pedido, não para vetar um descarte deliberado.

**Não existe chave de ambiente que desligue o descarte.** Ele é configuração de rota, sempre ativa. Quem controla o corte é a condição da regra — por isso mantenha a condição estreita e acompanhe o contador **Descartados**.

---

## 4. Amostragem %

**O que corta:** uma fração dos eventos que batem na regra. `100` (o padrão) entrega tudo; `10` entrega um em cada dez.

**Onde se configura:** campo **Amostragem %** da regra.

**Proteção de detecção:** sim. Enquanto a proteção estiver ligada, o campo fica desabilitado e a regra nunca é amostrada.

**Estabilidade:** a escolha é determinística por evento — o mesmo evento sempre toma o mesmo caminho, inclusive em nova tentativa. Não há sorteio a cada passagem.

O que sai amostrado é **etiquetado**: veja [Rótulos que acompanham a entrega](#rótulos-que-acompanham-a-entrega).

---

## 5. Descartar o evento bruto

**O que corta:** o bloco bruto do envelope — o payload original do fornecedor — **só na entrega daquela regra**. O evento normalizado (OCSF) é preservado inteiro.

**Onde se configura:** caixa **Descartar o evento bruto** da regra.

**Proteção de detecção:** sim, e este é o ponto que mais confunde: numa regra **protegida**, marcar a caixa **não faz absolutamente nada**. Se você marcou e o volume não caiu, confira a proteção antes de procurar defeito em outro lugar.

**Alcance:** é a decisão por destino que o mapeamento não consegue tomar. O mesmo evento vai **íntegro** para o data lake por uma regra e **enxuto** para o SIEM por outra. Costuma ser a maior economia isolada, porque o bloco bruto é normalmente a maior parte do envelope.

---

## 6. Agregação (rollup por destino)

**O que corta:** eventos repetidos, colapsados num único evento que carrega a contagem do grupo.

**Onde se configura:** na **política de entrega do destino**, não na rota — o agrupamento é declarado no JSON de entrega pelo administrador da plataforma. Agrupamento vazio (o padrão) significa desligada.

**É a única alavanca desligada por padrão** também na plataforma: além da configuração do destino, ela exige que a chave de ambiente correspondente seja ligada.

**Proteção de detecção:** por configuração, não por bloqueio automático. Como a agregação é ligada **no destino**, a garantia é você não apontar para um destino agregador a rota que alimenta detecção — a cópia completa vai ao data lake por uma rota separada.

**Trava de segurança:** se a variedade de grupos explodir, o lote passa **intacto** em vez de agregar. A alavanca degrada para "não agregou", nunca para "derrubou o worker".

---

## Rótulos que acompanham a entrega

Quando a plataforma corta alguma coisa, ela **avisa no evento entregue**. Isso é a diferença entre o analista concluir "o fornecedor não mandou esse campo" e "nós cortamos aqui, de propósito".

| Rótulo | Quando aparece | Como o analista deve ler |
|---|---|---|
| `_centralops.raw_dropped: true` | Entrega de uma regra com **Descartar o evento bruto** ligado. | **Nós** removemos o payload bruto nesta entrega. Não é ausência do fornecedor — o bruto existe e chegou íntegro nas outras rotas. |
| `_centralops.sample_rate` | Entrega de uma regra amostrada. Fração entregue, de 0 a 1. | Este evento representa uma amostra. **Reescale as contagens a jusante** por esse fator antes de tirar conclusões de volume. |
| `_centralops.suppress_count` | Evento **liberado** por uma regra com supressão ligada. | A **posição** deste evento dentro da janela da sua assinatura (de 1 até *Permitidos por janela*). **Não** diz quantas repetições foram suprimidas depois dele — com o valor 1, o mais comum, o rótulo é sempre `1`, tenha a janela engolido dez eventos ou dez mil. Para dimensionar o que foi suprimido, use o desfecho **Suprimido** na Captura ao vivo. |
| `_aggregate` | Evento resultante de uma agregação, com a contagem e os campos do grupo. | Esta linha não é um evento, é um resumo de vários. |

---

## Tiering: o mesmo evento, dois destinos

O ganho real não vem de escolher **uma** alavanca, e sim de tratar cada destino conforme o que ele custa e para que serve:

- **Data lake / armazenamento barato — recebe íntegro.** Perícia precisa do payload bruto. Deixe **Descartar o evento bruto** desligado e não amostre esta rota.
- **SIEM cobrado por volume — recebe enxuto.** Ligue **Descartar o evento bruto**, considere amostragem para telemetria de baixo valor e suprima o ruído repetitivo.

**A decisão é por rota.** O mesmo evento sai completo para um destino e podado para o outro, na mesma passagem — a cópia entregue a cada rota é independente. Não é preciso escolher entre perícia e conta baixa.

Um desenho comum, com três regras:

1. Severidade ≥ 7 → **SIEM**, bruto descartado (não exclusiva).
2. Tipo de evento na lista de ruído conhecido → **Descartar**.
3. Pega-tudo → **data lake**, íntegro (exclusiva).

Veja [Roteamento por regra](./routing.md) para montar as regras e [Fluxo de dados](../operations/fluxo-de-dados.md) para conferir o resultado.

---

:::danger[Desligar a proteção de detecção é uma decisão de risco]
**Proteger detecção nasce ligada em toda regra** — inclusive nas regras antigas, criadas antes de a opção existir, que são tratadas como protegidas. Enquanto está ligada, a regra nunca é amostrada, suprimida nem tem o bruto descartado.

Ao desligar, você autoriza que eventos **daquela** regra deixem de ser entregues por decisão de economia — inclusive eventos que alimentariam uma detecção. A plataforma pede confirmação explícita, e com razão: uma detecção que não dispara não deixa rastro, e o buraco só aparece no incidente seguinte.

Desligue apenas em rotas onde reduzir volume é comprovadamente seguro (telemetria de baixo valor), nunca na rota que alimenta o SIEM de detecção.
:::

## Próximos passos

- **Ver quanto está sendo cortado?** [Fluxo de dados](../operations/fluxo-de-dados.md).
- **Conferir o efeito evento a evento?** [Captura ao vivo](../operations/live-capture.md).
- **Montar as regras?** [Roteamento por regra](./routing.md).
- **Podar o payload do fornecedor?** [Especificação da DSL](../normalization/dsl-spec.md).
