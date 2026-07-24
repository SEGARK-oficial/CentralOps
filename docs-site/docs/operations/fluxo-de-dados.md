---
sidebar_position: 11
title: Fluxo de dados
description: O mapa Fontes → Roteamento → Destinos, com vazão e saúde por destino, e o card de redução de volume e custo.
---

# Fluxo de dados

A tela **Fluxo de dados** é o mapa do caminho que os eventos percorrem: **Fontes → Roteamento → Destinos**, desenhado como um grafo, com a vazão de cada trecho e a saúde de cada nó. É onde você responde "por onde o volume está passando?" e "quanto disso a plataforma está conseguindo evitar?".

> Esta é uma tela **só de administrador**. Você a encontra no menu **Operação → Fluxo de dados**.

Ela é diferente do [Dashboard](./dashboard.md): o Dashboard resume entrada e saída em números; aqui você vê a **topologia** — qual fonte alimenta qual rota, qual rota entrega a qual destino, e onde o fluxo estreita.

## Quando usar

- **Antes de mexer no roteamento.** Ver quais destinos uma fonte alcança hoje, para não descobrir depois que uma regra nova cortou um caminho que alguém usava.
- **Depois de ligar uma alavanca de redução.** Conferir se o volume evitado subiu como você esperava e por qual causa.
- **Numa conversa sobre custo.** O card de redução dá a ordem de grandeza do que está sendo cortado antes de chegar ao SIEM.
- **Quando um destino some do radar.** O nó fica marcado como degradado ou indisponível no próprio desenho.

## A faixa de indicadores

Quatro números no topo, todos na mesma unidade (**eventos por minuto**) para serem comparáveis entre si:

| Indicador | O que conta |
|---|---|
| **Ingestão** | Eventos entrando por todas as fontes visíveis para você. |
| **Roteados** | Eventos que **bateram** numa regra com ação **Encaminhar**, somados entre as regras. É contagem de casamento, não do que saiu pela rede: um evento amostrado para fora, bloqueado por residência ou anti-loop, ou casado por uma regra sem destino configurado continua contado aqui. |
| **Descartados** | Eventos apagados por regras com ação **Descartar**. Fica em vermelho sempre que for maior que zero — é um lembrete, não um erro. |
| **Entregues** | Eventos que efetivamente saíram para os destinos. |

São médias móveis de uma janela curta definida na configuração da plataforma, e a tela se atualiza sozinha a cada 15 segundos.

:::note[Roteados e Entregues não formam um funil]
Cada um conta uma coisa diferente. **Roteados** conta pares evento × regra casada; **Entregues** conta pares evento × entrega.

- Um evento que casa **uma** regra com três destinos: 1 em Roteados, 3 em Entregues.
- Um evento que casa **três** regras não-exclusivas (o padrão de tiering — SIEM e data lake): 3 em Roteados.

Por isso "Entregues" acima de "Roteados" é esperado, e a diferença entre os dois não mede perda.
:::

## Redução de volume & custo

O card **Redução de volume & custo** mostra o que a plataforma cortou antes de o volume chegar aos destinos, na janela indicada ao lado do título (as últimas 3 horas), somando as organizações que você enxerga.

| Número | O que é | Como é medido |
|---|---|---|
| **Coletado** | O que o fornecedor mandou. | O evento **cru**, contado **uma vez por evento**, antes de a plataforma montar o envelope. |
| **Entregue** | O que saiu pela porta. | O **envelope completo** (rótulos + evento normalizado + evento bruto), contado **por entrega** — um evento que vai a três destinos é contado três vezes. |
| **Evitado** | O que as alavancas de redução impediram de sair. | **Bases mistas**: a poda do mapeamento é creditada em cima do evento cru, o descarte e a amostragem em cima do envelope por destino, a supressão em cima do envelope uma vez por evento. |
| **Redução %** | A fração do volume que deixou de sair. | Evitado ÷ (Entregue + Evitado) — o denominador é o volume que **teria** saído se nada tivesse sido cortado. |

Cada número traz a sua base de medição escrita embaixo, na própria tela.

:::warning[Os quatro números não fecham como um balanço]
Não tente conferir `Coletado − Evitado = Entregue`: essa conta **não fecha**, e não é defeito.

**Coletado** é o evento cru contado uma vez. **Entregue** e **Evitado** são o envelope contado **por entrega** — e o envelope é maior que o evento cru (ele carrega o cru dentro de si), enquanto o envio simultâneo a vários destinos multiplica cada entrega. Por isso **"Evitado" pode legitimamente ficar maior que "Coletado"** sem que nenhum evento tenha sido contado duas vezes.

Quando a tela detecta essa situação, ela **avisa no próprio card**, em vez de exibir o número em silêncio. Enquanto as bases não forem unificadas, trate os quatro valores como ordem de grandeza — bons para comparar "antes e depois de ligar uma alavanca", ruins para fechar uma fatura.
:::

### De onde veio a economia

Logo abaixo dos quatro números, o card decompõe o **Evitado** por causa. Só aparecem as causas que de fato economizaram algo na janela — uma causa zerada é omitida.

| Etiqueta no card | Quem gerou | Disponível por padrão? |
|---|---|---|
| **poda** | O bloco de poda do payload bruto declarado no **mapeamento** — vale para todos os destinos. | Sim |
| **descarte por rota** | Regras de roteamento com ação **Descartar**. | Sim — é configuração de rota, não tem chave de ambiente que a desligue |
| **amostragem** | O campo **Amostragem %** da rota, quando abaixo de 100. | Sim |
| **supressão** | O rate-limit por assinatura da rota (**Permitidos por janela** acima de 0). | Sim |
| **descarte do bruto** | A opção **Descartar o evento bruto** da rota. Costuma ser a maior economia isolada. | Sim — também é configuração de rota |
| **agregação** | O rollup por destino, que colapsa eventos repetidos em um só. | **Não — é a única alavanca desligada por padrão.** |
| **redação** | Reservado para a redação de PII. Nenhuma parte da plataforma credita volume nessa causa hoje, então ela não aparece no card. | — |

"Disponível" quer dizer que a alavanca **pode** agir, não que ela esteja cortando alguma coisa. O que de fato liga cada corte é a configuração **da rota** — que nasce conservadora: a amostragem começa em 100% (entrega tudo) e a supressão começa em zero (desligada). Enquanto ninguém mexer nesses campos, nenhum evento é economizado. Veja [Redução de volume](../outputs/reducao-de-volume.md) para o que cada alavanca corta e onde se configura.

### Economia estimada, em dinheiro

O bloco final traduz o volume evitado em valor por dia. Ele depende de duas coisas:

1. **Preço por GB configurado no destino.** No cadastro de cada destino, na seção **Custo (FinOps)**, há os campos **Preço por GB** e **Moeda**. Esse preço serve **só** para essa conversão — ele não muda nada na entrega. Configurá-lo é recurso da edição Community.
2. **Edição Enterprise.** Converter bytes em moeda é recurso Enterprise. Na Community o card mostra volume e percentual, e diz isso abertamente em vez de exibir um valor inventado.

Se o preço não estiver preenchido, o card informa que **o preço por GB não está configurado** — e não mostra "US$ 0,00", que seria indistinguível de "não economizou nada".

O valor exibido é **por dia**: a plataforma projeta para 24 horas o que mediu na janela de 3 horas.

:::note[O card não apareceu]
O card se esconde sozinho em dois casos: a medição de custo está desligada na plataforma, ou não houve tráfego nenhum na janela. Nos dois casos ele some inteiro em vez de mostrar uma fileira de zeros.
:::

## Topologia do fluxo

O desenho abaixo do card é interativo:

- **Largura e velocidade das faixas** são proporcionais à vazão daquele trecho — um caminho fino é um caminho com pouco tráfego.
- **Role para dar zoom, arraste para deslocar.**
- **Cada nó tem uma bolinha de saúde**: verde (saudável), amarelo (degradado), vermelho (indisponível). Para as fontes, o critério é o mesmo da [Saúde do pipeline](./pipeline-health.md); para os destinos, o mesmo de [Operar destinos](../outputs/destination-operations.md).
- **Clique em um nó** para abrir o painel lateral com a vazão e o estado daquele ponto. Em destinos, o painel também traz o volume em bytes por minuto e uma amostra dos últimos eventos entregues.

### Feed ao vivo

O botão **Feed ao vivo**, no topo, abre um painel que acompanha os destinos de maior vazão e lista os eventos mais recentes que chegaram neles, atualizando sozinho. Serve para confirmar em segundos que algo continua saindo — não substitui a [Captura ao vivo](./live-capture.md), que é a ferramenta para seguir o destino final de **cada** evento, inclusive dos que não foram entregues.

## Limitações

- A tela mostra **taxa**, não histórico: ela responde "como está agora", não "o que aconteceu ontem às 3h".
- Um evento que morreu antes do roteamento (em quarentena, ou suprimido) não aparece como uma faixa no desenho. Para segui-lo, use a [Captura ao vivo](./live-capture.md).
- Os números de redução são **aproximações** com bases de medição diferentes — veja o aviso acima.

## Próximos passos

- **Quer cortar volume?** [Redução de volume](../outputs/reducao-de-volume.md) — as alavancas, em ordem, e o que cada uma corta.
- **Quer mudar para onde os eventos vão?** [Roteamento por regra](../outputs/routing.md).
- **Um destino aparece degradado?** [Operar destinos](../outputs/destination-operations.md).
- **Um evento específico não chegou?** [Captura ao vivo](./live-capture.md).
