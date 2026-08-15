---
sidebar_position: 4
title: Entregar OCSF 1.8 puro
description: Como apontar HTTP, Splunk HEC ou ClickHouse para um consumidor que espera OCSF e nada mais, sem o envelope do CentralOps.
---

# Entregar OCSF 1.8 puro

Alguns destinos esperam **um evento OCSF e nada mais**. É o caso de um SIEM nativo de OCSF, de uma tabela cujo DDL foi escrito a partir do schema OCSF, ou de uma esteira montada com Cribl ou Tenzir.

Para esses, o envelope canônico do CentralOps atrapalha: ele traz o namespace `_centralops` e o `raw` do fornecedor ao lado do evento normalizado.

## O que muda na prática

Todo destino do CentralOps entrega, por padrão, o **envelope canônico**:

```json
{
  "_centralops": { "event_id": "...", "organization_id": 7, "integration_id": 42 },
  "normalized": { "class_uid": 4001, "time": 1786000000000, "...": "..." },
  "raw": { "campo_do_fornecedor": "..." }
}
```

Com o formato **`ocsf`**, o que sai é só o miolo:

```json
{ "class_uid": 4001, "time": 1786000000000, "...": "..." }
```

:::danger[Mandar o envelope a quem espera OCSF falha dos dois jeitos, e os dois são ruins]
Se o consumidor for tolerante, ele **descarta em silêncio** o que não reconhece. Você vê "entregue" nos dois lados e o dado chega mutilado, sem ninguém perceber.

Se for estrito, **um campo a mais derruba o lote inteiro**. A entrega para, e o erro aponta para o consumidor, não para a origem, então a investigação começa no lugar errado.
:::

## Onde configurar

O campo chama-se **Payload** no formulário do destino, e existe em três tipos:

| Destino | Onde o formato se aplica |
|---|---|
| **HTTP (webhook genérico)** | O corpo do POST, em array ou NDJSON |
| **Splunk HEC** | O conteúdo do campo `event` dentro do wrapper HEC |
| **ClickHouse** | Cada linha do `INSERT ... FORMAT JSONEachRow` |

As opções são duas:

- **`envelope`** (padrão): o canônico, com `_centralops` e `raw`. É o que serve para investigar e correlacionar dentro da plataforma.
- **`ocsf`**: só o evento OCSF 1.8.

Nada mais muda. Autenticação, lote, retentativa e disjuntor continuam iguais.

:::tip[Não há conversão no meio do caminho]
O "OCSF puro" é literalmente o bloco que o pipeline já normalizou e validou contra o manifesto OCSF 1.8. Não existe uma segunda transformação aqui, de propósito: ela poderia divergir do normalizador e produzir dois OCSF diferentes para o mesmo evento.

Isso também quer dizer que a qualidade do que sai depende do mapping do fornecedor. Se um campo não está no OCSF entregue, ele não está no mapping.
:::

## Exemplo: ClickHouse com tabela modelada em OCSF

Um consumidor comum expõe uma tabela própria para ingestão OCSF. A configuração fica assim:

| Campo | Valor |
|---|---|
| URL | `https://SEU-HOST:8123` |
| Banco | `nome_do_banco` |
| Tabela | `nome_da_tabela_ocsf` |
| Usuário | `usuario_de_ingestao` |
| Credencial | a senha do usuário (fica cifrada no cofre) |
| **Payload** | **`ocsf`** |

Duas coisas que evitam uma investigação longa:

**O CentralOps fala com o ClickHouse pela interface HTTP** (porta 8123 por padrão), não pelo protocolo nativo (9000). Se a documentação do seu destino só cita a porta nativa, procure a HTTP: ela costuma estar habilitada no mesmo serviço. Um destino que só publique a 9000 não é alcançável por este sink.

**`Ignorar campos desconhecidos` continua ligado por padrão.** Com `payload: ocsf` isso vira uma rede de proteção útil, porque a tabela pode não ter coluna para todo campo OCSF. Mas ele também esconde erro de modelagem: se você suspeita que está perdendo campo, desligue temporariamente e veja o que o servidor recusa.

## Exemplo: HTTP com Bearer

| Campo | Valor |
|---|---|
| URL | `https://SEU-HOST/ingest` |
| Método | `POST` |
| Modo de autenticação | `bearer` |
| Credencial | o token (fica cifrado no cofre) |
| Formato do lote | `array` ou `ndjson`, conforme o destino aceitar |
| **Payload** | **`ocsf`** |
| Headers | pares extras que o destino exija, por exemplo um identificador de origem |

O campo **Headers** é um editor de pares. Use-o quando o destino pedir um cabeçalho próprio além da autenticação.

:::caution[O modo de autenticação é uma lista, e isso é proposital]
Escolha `bearer` ou `basic` na lista. Antes o campo aceitava texto livre, e um valor escrito errado não dava erro: o CentralOps simplesmente não mandava cabeçalho de autenticação nenhum, e o destino respondia `401` sem explicar por quê.
:::

## Exemplo: Splunk HEC

| Campo | Valor |
|---|---|
| URL | `https://SEU-HOST:8088` |
| Sourcetype | um sourcetype que descreva OCSF na sua convenção |
| Credencial | o token HEC |
| **Payload** | **`ocsf`** |

O identificador do evento continua indo em `fields`, fora do evento. Ele é metadado de transporte, serve para deduplicação no indexador, e não contamina o OCSF entregue.

## Conferindo que funcionou

Depois de salvar, use o **testar conexão** do destino. Ele abre conexão real e reporta o que o outro lado respondeu.

Para conferir o formato, e não só a conectividade, o caminho mais direto é a **linhagem do evento**: escolha um evento recente e veja o que foi entregue àquele destino.

Se o consumidor aceitar o lote mas os eventos aparecerem vazios ou sem classificação, o suspeito costuma ser o mapping do fornecedor, não o destino. Um evento que não passou pela normalização é entregue vazio de propósito em modo `ocsf`, justamente para não mandar o formato errado em silêncio.

## Compatibilidade

O destino HTTP tinha antes um campo `body` com os valores `envelope` e `normalized`. Ele continua funcionando: `normalized` equivale a `ocsf`. Configuração já gravada não precisa de mudança, e o campo novo é o que aparece no formulário.
