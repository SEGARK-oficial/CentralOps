---
sidebar_position: 2
title: Filtro de coleta
description: Empurre o descarte para a consulta feita ao fornecedor, em vez de trazer tudo e jogar fora depois — o que isso economiza, o que isso custa em fidelidade e como configurar.
---

# Filtro de coleta

O **filtro de coleta** faz o CentralOps pedir menos ao fornecedor. Em vez de buscar todos os eventos e descartar os indesejados depois, a própria consulta ao fornecedor já sai restrita — e o que não veio nunca ocupou transporte, processamento nem fila.

Ele **nasce desligado** em toda integração. Se você nunca abrir esta tela, a coleta continua exatamente como sempre foi.

## Por que ele existe

Em julho de 2026, uma integração Wazuh em produção entregava ao SIEM eventos criados **horas antes**. A coleta rodava sem um único erro; o painel marcava tudo verde.

Os números do diagnóstico:

| | |
|---|---|
| Eventos acumulados esperando coleta | **2.906.255** |
| Desses, quantos as regras de roteamento descartavam | **97,6%** (só 69.859 seriam entregues) |
| Onde o cursor de coleta estava | **15 horas** atrás do presente |
| Velocidade de recuperação | **0,47×** o relógio — o atraso crescia ~32 min a cada hora |

A coleta gastava o orçamento inteiro de cada ciclo transportando ruído. Cada ciclo trazia o teto de eventos, esse lote passava por normalização e roteamento, e a regra de roteamento jogava fora **97 de cada 100**. O trabalho útil era 2,4% do trabalho feito — e por isso o coletor nunca alcançava o presente.

:::danger[Descartar no roteamento não economiza coleta]
Uma regra de roteamento que descarta severidade baixa **não faz o coletor buscar menos**. O evento já foi consultado no fornecedor, transferido pela rede, contado, normalizado e avaliado — o descarte acontece no fim dessa fila, não antes dela.

Se o seu roteamento já joga fora a maior parte do que entra, o descarte está no lugar errado: ele está economizando **entrega**, quando o gargalo é **coleta**.
:::

## Filtrar na coleta ou descartar no roteamento?

As duas coisas parecem iguais no resultado (o evento não chega ao destino) e são completamente diferentes no custo e na reversibilidade.

| | **Filtro de coleta** | **Descarte no roteamento** |
|---|---|---|
| Onde age | Na consulta feita ao fornecedor | Depois de coletar e normalizar |
| O que economiza | **Transporte**: chamadas ao fornecedor, rede, tempo de ciclo, fila | **Entrega**: volume e custo no destino |
| Alcance | A integração inteira — vale para todos os destinos | Só a rota onde a regra está |
| O evento entra na plataforma? | **Não** | Sim (aparece na captura ao vivo, no drift, e pode ir para outra rota) |
| Serve para acelerar uma coleta atrasada? | **Sim** — é a única alavanca que faz isso | Não |
| Serve para cortar custo de um SIEM específico? | Não com precisão — corta para todos os destinos | **Sim** |

**Use o filtro de coleta quando** a coleta não vence o volume: o card de Saúde do Pipeline mostra **Atraso dos dados** em horas e a etiqueta **Backlog**, e você sabe que boa parte do que chega é descartada logo em seguida.

**Use o descarte no roteamento quando** o problema é custo ou ruído em um destino específico, e você quer manter o evento disponível para outro destino, para investigação ou para uma regra futura. As seis alavancas dessa família estão em [Redução de volume](../outputs/reducao-de-volume.md).

**Use os dois** quando fizer sentido: o filtro de coleta corta o que ninguém quer em lugar nenhum; o roteamento decide o que cada destino recebe do que sobrou.

:::warning[O que é filtrado na origem NUNCA entra na plataforma]
Esta é a diferença que custa caro se for descoberta depois. O evento não filtrado não é "descartado mais cedo" — ele simplesmente **não existe** para o CentralOps:

- **não aparece na captura ao vivo** — nem se você abrir a captura no minuto seguinte;
- **não gera campo novo no Drift Explorer** — se o fornecedor passar a mandar um campo só nos eventos filtrados, você não fica sabendo;
- **não fica disponível para uma rota futura** — criar amanhã uma regra que precisa desses eventos não os traz de volta;
- **não conta em nenhum relatório** de volume ou de redução da plataforma.

Em compensação, os eventos **continuam existindo no Wazuh de origem**, íntegros, sujeitos à retenção de lá. O CentralOps apenas deixa de transportá-los. Se você precisar deles depois, a fonte ainda está lá — mas o caminho é recoletar, não consultar a plataforma.
:::

## Ligar o filtro não muda o passado

O filtro age **na próxima consulta ao fornecedor**, e só nela:

- **Ligar** não reprocessa nem apaga nada do que já foi coletado. Os eventos que já estão na plataforma continuam lá, pesquisáveis e roteáveis.
- **Desligar** não recupera o que foi pulado enquanto o filtro esteve ligado. Aquele período simplesmente não foi coletado.

Para recuperar de propósito um intervalo que ficou de fora, o caminho é explícito e tem três passos: **desligue o filtro, rode um [backfill](./backfill.md) do período, religue o filtro**. Não há atalho — a recoleta histórica **honra o mesmo filtro** da integração, então um backfill com o filtro ligado voltaria a pular exatamente os mesmos eventos.

## Como configurar

1. Vá ao menu **Visão geral → Integrações**.
2. Abra a integração para **editar** e localize a seção **Filtros de coleta**.
3. Ajuste o filtro do fluxo desejado. Ao ligar um filtro, a tela pede uma **confirmação** explícita — é a última parada antes de a plataforma deixar de coletar algo.
4. Salve. Para voltar atrás, use **Remover filtro** (ou **Remover todos os filtros**).

A mudança vale a partir do próximo ciclo de coleta — não é preciso reiniciar nada nem pausar a integração.

Cada alteração fica registrada na auditoria com o valor **antes e depois**, porque ligar um filtro faz a plataforma parar de coletar evento que hoje ela coleta — e o que não foi coletado não se recupera sozinho.

:::note[Não dá para configurar na criação da integração]
A seção só aparece **depois que a integração existe**. Ao cadastrar uma origem nova, o resumo do que será coletado indica quantos filtros aquele fornecedor oferece, mas a configuração é feita numa edição posterior. Na prática: a integração nasce coletando tudo, e você decide filtrar depois de ver o volume real.

Se a seção sumir com uma mensagem de erro em vez de aparecer, os filtros **não** foram desligados — a tela apenas não conseguiu lê-los. O que estiver ligado continua valendo.
:::

### Exemplo: Wazuh, coletar só o que interessa

O coletor de detecções do Wazuh oferece o filtro **Nível mínimo da regra** (`rule.level`), de 0 a 16. O padrão é **0**, que coleta tudo.

O mapeamento padrão traduz o nível do Wazuh para a severidade normalizada assim:

| `rule.level` no Wazuh | Severidade normalizada |
|---|---|
| 0 – 3 | Informativo |
| 4 – 6 | Baixo |
| 7 – 11 | Médio |
| 12 – 14 | Alto |
| 15 – 16 | Crítico |

Com essa tabela, você traduz a regra de roteamento que já tem para o valor do filtro:

| Se o seu roteamento hoje descarta… | …configure o nível mínimo em |
|---|---|
| Severidade **Informativo** | **4** |
| Severidade **Informativo e Baixo** | **7** |
| Tudo abaixo de **Alto** | **12** |

No caso do incidente descrito acima, a regra descartava Informativo e Baixo — ou seja, **nível mínimo 7**. Os mesmos 97,6% continuam sendo descartados; a diferença é que passam a ser descartados **antes de serem transportados**, e o ciclo passa a gastar o teto de eventos em coisa útil.

:::tip[Comece pelo que o roteamento já descarta]
O filtro mais seguro de ligar é o que espelha uma decisão que você já tomou. Se uma regra de roteamento descarta aquela severidade há meses e ninguém sentiu falta, empurrar esse mesmo corte para a coleta não muda o que o SOC vê — muda só o custo de chegar lá.

O que **não** fazer é usar o filtro para experimentar um corte novo: aí o risco de perder algo que você ainda não sabe que precisa é real, e irreversível.
:::

### Confira se surtiu efeito

Depois de salvar, acompanhe em **Normalização → Saúde do Pipeline** o card daquela integração:

- **Eventos/min** cai — é o esperado, e é o filtro funcionando.
- **Backlog** deve deixar de aparecer. **É esta a prova de que o filtro resolveu**: a etiqueta some no primeiro ciclo que termina sem bater o teto de eventos, ou seja, quando a coleta passou a dar conta do volume da origem.
- **Atraso dos dados** cai enquanto ainda houver acúmulo a drenar. Depois que a coleta alcança o presente, ele **para de ser um indicador de velocidade** — veja o aviso abaixo antes de usá-lo como critério.

Se a etiqueta **Backlog** continuar aparecendo depois de alguns ciclos, o filtro não foi agressivo o bastante para o volume daquela fonte — ou o gargalo é outro. Veja [Eventos chegando horas depois](../runbooks/collection-lag-backlog.md).

:::warning[Com o filtro ligado, o Atraso dos dados sobe — e isso é normal]
A posição de coleta do Wazuh é o horário do **último evento que passou no filtro**. Um ciclo que consulta o fornecedor e não encontra nenhum evento acima do nível mínimo **mantém a posição parada**, de propósito: não houve nada para marcar como consumido.

O filtro cria exatamente esse regime. Quanto mais agressivo o corte, mais esparso o fluxo, e mais tempo a posição passa parada entre um evento e o seguinte. Numa fonte em que eventos de nível 12 ou mais aparecem de hora em hora, o **Atraso dos dados oscila até uma hora com a coleta perfeitamente em dia** — o número mede o silêncio da fonte, não a lentidão do coletor.

Por isso o critério de sucesso é a etiqueta **Backlog**, e não o Atraso dos dados. É também por isso que o card **não fica amarelo** só porque o atraso subiu: são precisas as **duas** condições ao mesmo tempo — atraso acima de 30 minutos **e** último ciclo terminado no teto. Atraso alto **sem** Backlog é fluxo esparso; atraso alto **com** Backlog é coleta que não vence o volume.
:::

:::warning[Se ligar o filtro fizer sumir os alertas graves, pare]
Sintoma raro e específico do Wazuh: você configura nível mínimo 7 e o que some são justamente os alertas **críticos**, em vez dos ruidosos.

Isso indica que o campo `rule.level` foi redefinido como texto no seu Indexer, em vez do tipo numérico do template oficial do Wazuh. Comparado como texto, "7" é maior que "16" — o filtro corta o contrário do pretendido, em silêncio.

Desligue o filtro imediatamente e peça ao administrador do Wazuh para conferir o tipo do campo `rule.level` no índice.
:::

## Cada fornecedor oferece o que sabe filtrar

A tela é montada a partir do que **cada integração declara**. Não existe uma lista fixa de filtros:

- Uma integração cujo fornecedor não permite restringir a consulta **não mostra a seção Filtros de coleta**. Não é limitação da sua instalação — é o fornecedor que não oferece esse corte na API.
- Um fluxo pode ter filtro e outro da mesma integração não ter. Os filtros são por **fluxo** (detecções, auditoria…), não por integração.
- Quando um fornecedor passa a oferecer um corte novo, ele aparece na tela na atualização seguinte, sem nada para configurar.

Hoje o **Wazuh (detecções)** é a integração com filtro de coleta declarado.

## Próximos passos

- **A coleta está atrasada?** Veja [Eventos chegando horas depois](../runbooks/collection-lag-backlog.md).
- **Quer entender os indicadores do card?** Veja [Saúde do Pipeline](../operations/pipeline-health.md).
- **Quer cortar custo no destino, sem deixar de coletar?** Veja [Redução de volume](../outputs/reducao-de-volume.md).
- **Precisa recuperar um período pulado?** Veja [Backfill](./backfill.md).
