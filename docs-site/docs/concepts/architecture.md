---
sidebar_position: 1
title: Como o CentralOps funciona
description: Os três estágios do CentralOps (coletar, normalizar, entregar) e onde você acompanha cada um pela interface
---

# Como o CentralOps funciona

O CentralOps recebe eventos de segurança dos seus fornecedores, padroniza esses eventos para um formato único e os entrega aos destinos que você escolhe. Toda a operação é feita pela interface web — as partes de infraestrutura rodam automaticamente nos bastidores, mantidas pela equipe da plataforma.

## Quando usar

- **Entender de onde vem um atraso.** Os alertas estão chegando devagar e você quer saber se o problema é na coleta junto ao fornecedor, na padronização ou na entrega ao destino.
- **Explicar o fluxo para um colega novo.** Você precisa mostrar, em linguagem de produto, por onde um evento passa desde a origem até o Wazuh, Splunk ou outro destino.
- **Saber onde olhar quando algo falha.** Antes de abrir um chamado, você quer identificar em qual estágio o evento parou (coleta, fila de reenvio, quarentena ou campos novos detectados).

## Os três estágios

Todo evento percorre três momentos. Cada estágio tem uma tela própria onde você acompanha o que está acontecendo.

| Estágio | O que acontece | Onde acompanhar na interface |
|---------|----------------|------------------------------|
| **1. Coletar** | O CentralOps busca os eventos nas APIs dos fornecedores (por exemplo, Sophos Central) de forma contínua, sem reprocessar o que já foi visto. | Menu **Visão geral -> Integrações** e **Operação -> Collectors** |
| **2. Normalizar** | Cada evento bruto é convertido para o formato padrão da plataforma, aplicando as regras de mapeamento que você definiu. | Menu **Normalização -> Mappings** |
| **3. Entregar** | O evento padronizado é roteado e enviado aos destinos configurados. Um mesmo evento pode ir para vários destinos ao mesmo tempo, ser descartado de propósito ou cair na rota padrão. | Menu **Operação -> Fluxo de dados** e **Operação -> Destinos** (somente administrador) |

Você gerencia tudo isso pela interface. O processamento em segundo plano que move os eventos entre os estágios é dimensionado e mantido pela equipe de infraestrutura — você não precisa configurá-lo.

## Estágio 1 — Coletar

O CentralOps conecta-se às APIs dos seus fornecedores e busca novos eventos em ciclos curtos (a cada poucos minutos), de forma automática. Quando você cria uma integração nova, a coleta passa a ser agendada sozinha em segundos, sem nenhuma ação extra.

Pontos importantes:

- A coleta continua de onde parou: o CentralOps lembra o último evento já recebido de cada integração e não reprocessa o que já foi coletado.
- Eventos em tempo real (como alertas) são priorizados sobre coletas mais pesadas (como auditoria e recoleta histórica), para que um trabalho grande não atrase os alertas urgentes.
- Duplicados são removidos automaticamente: se o mesmo evento for buscado mais de uma vez, ele entra uma única vez.

**Onde acompanhar:** menu **Visão geral -> Integrações** mostra cada integração com status (saudável, atenção, erro) e a data da última coleta. O menu **Operação -> Collectors** detalha a atividade de coleta por fornecedor.

## Estágio 2 — Normalizar

Cada evento bruto chega em um formato diferente, próprio de cada fornecedor. A normalização converte todos para um formato padrão único, usando as regras de mapeamento (o editor de mapeamento) que você mantém. Assim, um alerta da Sophos e um do Microsoft Defender ficam comparáveis e pesquisáveis da mesma forma.

Quando a normalização encontra um problema, o CentralOps nunca descarta o evento em silêncio. Ele sinaliza em uma de duas telas:

- **Quarentena** (menu **Normalização -> Quarentena**) — eventos que não passaram na validação, por exemplo por falta de um campo obrigatório. Você revisa, corrige a regra e reprocessa.
- **Campos novos detectados** (menu **Normalização -> Drift Explorer**) — campos que o fornecedor passou a enviar e que as regras atuais ainda não cobrem. O evento continua sendo entregue, mas com menos contexto até você ajustar o mapeamento.

**Onde acompanhar:** menu **Normalização -> Mappings** para as regras, e a tela **Saúde do Pipeline** (também em **Normalização**) para a taxa de processamento.

## Estágio 3 — Entregar

Depois de padronizado, o evento é avaliado por regras de roteamento e enviado aos destinos configurados. Esse é o estágio com mais flexibilidade:

- **Envio simultâneo a vários destinos.** Um mesmo evento pode ser entregue, por exemplo, ao Wazuh e ao Splunk ao mesmo tempo.
- **Descarte intencional.** Eventos de ruído podem ser descartados de propósito para controlar custo e volume, conforme as regras que você definir.
- **Rota padrão (catch-all).** Todo evento que não se encaixa em nenhuma regra específica é enviado ao destino padrão (o Wazuh). Nenhum evento se perde por falta de uma regra.

Se um destino fica indisponível, os eventos não são perdidos: eles entram em uma **fila de reenvio** e são reenviados automaticamente quando o destino volta. Há também uma proteção contra destino instável, que reduz a pressão sobre um destino com falhas repetidas.

**Onde acompanhar:**

- Menu **Operação -> Fluxo de dados** (somente administrador) — visão completa do caminho dos eventos, dos fornecedores até cada destino, com volume ao vivo.
- Menu **Operação -> Roteamento** (somente administrador) — as regras que decidem para onde cada evento vai.
- Menu **Operação -> Destinos** (somente administrador) — os destinos configurados e o status de cada um, incluindo a fila de reenvio.

### Destinos suportados

O CentralOps entrega para vários tipos de destino. A configuração de cada um é feita na tela **Operação -> Destinos** (somente administrador):

- Wazuh Manager e outros receptores Syslog (este é o destino padrão)
- Splunk
- Elasticsearch
- Microsoft Sentinel
- Amazon S3 (data lake)
- Apache Kafka
- Arquivo / fluxo JSONL
- OpenTelemetry (observabilidade)

Para os detalhes de roteamento e de cada destino, veja [Roteamento](../outputs/routing.md) e [Destinos](../outputs/destinations.md).

## Além da coleta: análise em tempo real e correlação

Além de coletar, normalizar e entregar eventos, o CentralOps oferece ferramentas para buscar e correlacionar dados sem precisar reingerir:

### Busca federada (Query)

Você pode buscar eventos ao vivo em múltiplas fontes de segurança ao mesmo tempo, sem recoleta. A plataforma fala diretamente com as APIs das integrações (Sophos, CrowdStrike, Microsoft Defender, Wazuh) e com o seu data lake S3 (se configurado), coletando resultados de cada uma em um diálogo estruturado.

Quando usar:

- Investigar um incidente urgente sem esperar pela coleta agendada.
- Correlacionar eventos de múltiplas fontes durante o mesmo período.
- Consultar dados históricos guardados no data lake sem reimportar.

Onde usar: menu **Operação -> Busca federada**.

**Detecções:** resultados interessantes da busca federada (ou triggers automáticos de regras de correlação) viram detecções — elementos que você triou como relevantes (aberto, confirmado, fechado). Cada detecção rastreia de qual fonte veio, a severidade, quantas vezes o padrão apareceu e por quanto tempo deve ser suprimida.

Onde ver: menu **Operação -> Detecções**.

### Correlação cross-source (Regras de correlação)

Você pode definir regras que procuram padrões em múltiplas fontes — por exemplo, "alertar se a mesma máquina aparecer em mais de 5 eventos do Sophos em 5 minutos". A correlação roda automática, em segundo plano, sobre os eventos coletados. Quando uma regra bate, uma detecção é gerada.

Hoje há suporte a correlação de **limite** (threshold): "X eventos com campo Y em janela de tempo Z". Correlações mais complexas (sequência, agregação) estão no roadmap.

Onde usar: menu **Conhecimento -> Correlação**.

## Onde olhar quando algo está lento ou falhando

A tela **Normalização -> Saúde do Pipeline** é o primeiro lugar para diagnosticar problemas de fluxo. Ela mostra, em um só painel:

- O status de cada integração (saudável, atenção, erro), a última coleta e a taxa de eventos por minuto.
- O estado do processamento em segundo plano (online ou com atraso).
- O acúmulo de eventos aguardando — quando algo cresce continuamente, é sinal de gargalo.

Use o quadro abaixo para identificar onde está o problema:

| Sintoma | Estágio provável | Onde investigar |
|---------|------------------|-----------------|
| Eventos demoram a aparecer; acúmulo crescente na coleta | Coleta | **Saúde do Pipeline** e **Integrações** |
| Eventos caindo em quarentena | Normalização | **Normalização -> Quarentena** |
| Muitos campos novos sinalizados | Normalização | **Normalização -> Drift Explorer** |
| Um destino não recebe; fila de reenvio crescendo | Entrega | **Operação -> Destinos** e **Roteamento** |

> Se o acúmulo continua crescendo mesmo sem nenhum erro nas telas acima, pode ser falta de capacidade no processamento em segundo plano. Esse dimensionamento é gerenciado pela equipe de infraestrutura — registre o que você viu na tela de **Saúde do Pipeline** e fale com o administrador da plataforma.

## Acompanhamento e observabilidade

O CentralOps mostra o que está acontecendo de duas formas:

- **Dentro do produto.** A tela **Normalização -> Saúde do Pipeline** reúne a vazão por destino, as tentativas de entrega (sucesso e falha) e o estado geral do pipeline. É o que você usa no dia a dia.
- **Integração com sua plataforma de observabilidade.** O CentralOps também pode enviar métricas e rastreamento de eventos (do início ao fim do fluxo) para uma ferramenta externa, como Datadog, Grafana ou Honeycomb. Essa conexão é definida pela equipe de infraestrutura no momento do deploy. Se precisar habilitá-la ou alterá-la, fale com o administrador da plataforma.

## Garantias do pipeline

- **Coletar duas vezes é seguro.** Cada evento é identificado de forma única; coletas repetidas não geram duplicados.
- **Ordem preservada por integração.** Dentro de uma mesma integração, os eventos mantêm a ordem temporal. Não há ordenação entre fornecedores diferentes.
- **Nenhuma perda silenciosa.** Um evento que falha sempre aparece em uma tela: **Quarentena** (falha de validação), **Drift Explorer** (campos novos) ou na fila de reenvio (destino indisponível). Você sempre tem onde olhar.
- **Entrega confiável aos destinos.** Em caso de falha de rede, um evento pode ser reenviado; os destinos eliminam eventuais duplicatas para que cada evento conte uma vez só.

## Próximos passos

- **Acompanhar a saúde do fluxo** -> [Saúde do Pipeline](../operations/pipeline-health.md)
- **Definir para onde os eventos vão** -> [Roteamento](../outputs/routing.md)
- **Configurar destinos** -> [Destinos](../outputs/destinations.md)
- **Buscar eventos ao vivo em múltiplas fontes** -> [Busca federada](../operations/federated-search.md)
- **Consultar dados históricos do data lake** -> [Busca no lake (search-in-place)](../operations/federated-search.md)
- **Definir regras de correlação** -> [Regras de correlação](../operations/correlation-rules.md)
- **Recoleta histórica de eventos** -> [Recoleta histórica](../pipelines/backfill.md)
- **Tratar campos novos detectados** -> [Drift Explorer](../pipelines/drift.md)

---

## Nota: versão 1.7 (legado)

Em versões anteriores à 2.0, o CentralOps enviava eventos apenas para o Wazuh Manager. O caminho `Sophos -> Normalizar -> Wazuh` funcionava assim. A partir da 2.0, esse passou a ser apenas um dos fluxos possíveis: a rota padrão continua enviando tudo para o Wazuh, mas você pode adicionar outros destinos e regras de roteamento.
