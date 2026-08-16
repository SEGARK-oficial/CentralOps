---
sidebar_position: 4
title: Entregar OCSF 1.8 puro
description: Como apontar destinos para um consumidor que espera OCSF puro sem o envelope do CentralOps, em duas topologias distintas.
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

## Duas topologias de consumidor

O CentralOps suporta **dois jeitos diferentes** de entregar OCSF, dependendo de como a tabela do consumidor foi modelada.

### Topologia 1: Uma coluna por campo OCSF

A tabela tem uma coluna para cada campo do evento normalizado.

**Configuração:**
- `payload`: `ocsf`
- `row_shape`: `flat` (padrão)

**Linha emitida:**

```json
{"class_uid": 1006, "time": 1786000000000, "metadata": {...}, "device": {...}}
```

**Exemplo de DDL para ClickHouse:**

```sql
CREATE TABLE events_ocsf (
    class_uid UInt16,
    time UInt64,
    metadata String,
    device String,
    actor String,
    ...mais campos OCSF...
) ENGINE = MergeTree
ORDER BY (time, class_uid);
```

### Topologia 2: Uma coluna de evento mais rótulos

A tabela tem **uma coluna que recebe o evento inteiro** mais colunas de rótulo (ex.: `source_type`, `collector_id`).

**Configuração:**
- `payload`: `ocsf`
- `row_shape`: `wrapped`
- `event_key`: nome da coluna que recebe o evento (ex: `event`)
- `row_fields`: pares coluna=valor para os rótulos (ex: `source_type=sophos_central`)

**Linha emitida:**

```json
{"event": {"class_uid": 1006, "time": 1786000000000, "...": "..."}, "source_type": "sophos_central"}
```

**Exemplo de DDL para ClickHouse:**

```sql
CREATE TABLE events_wrapped (
    event String,
    source_type String
) ENGINE = MergeTree
ORDER BY tuple();
```

:::tip[Por que duas topologias]
A primeira (flat) é o padrão para quem quer buscar por campos OCSF como `class_uid` ou `device.hostname`.

A segunda (wrapped) é comum em SIEMs que recebem evento serializado (como String/JSON) e usam a coluna de rótulo para escopo de detecção e painéis. Exemplo: nano SIEM, Tenzir no Tenzir Lake, Cribl com transformação pré-aplicada.
:::

## Modo de falha: entrega silenciosa de linhas vazias

Se você escolher a forma errada para a tabela, o resultado é particularmente silencioso:

- A tabela espera `flat` (coluna por campo).
- Você configura `wrapped` (evento aninhado + rótulos).
- As chaves emitidas são `event` e `source_type`.
- A tabela não tem essas colunas, tem `class_uid`, `time`, `metadata`, etc.
- Com `skip_unknown_fields=1` (padrão), o ClickHouse descarta as chaves desconhecidas.
- HTTP responde **200**, `written_rows=1`.
- Você vê entregue, o consumidor vê recebido, mas a linha está vazia.

**O teste de conexão do CentralOps pega esse erro**: ele compara as colunas emitidas com as da tabela real e falha explicitamente.

## Onde configurar

O campo **Payload** existe em três tipos de destino:

| Destino | Como configurar |
|---------|-----------------|
| **HTTP (webhook genérico)** | Campo `Payload` no formulário. Afeta o corpo do POST (array ou NDJSON). |
| **Splunk HEC** | Campo `Payload` no formulário. Afeta o conteúdo do campo `event` dentro do wrapper HEC. |
| **ClickHouse** | Campo `Payload` no formulário. Afeta cada linha do `INSERT ... FORMAT JSONEachRow`. |

As opções são duas:

- **`envelope`** (padrão): o canônico, com `_centralops` e `raw`. É o que serve para investigar e correlacionar dentro da plataforma.
- **`ocsf`**: só o evento OCSF 1.8.

Com `row_shape=wrapped`, os campos adicionais são:

- **`Forma da linha`**: escolha `wrapped`.
- **`Coluna do evento`**: nome da coluna que recebe o evento inteiro (ex: `event`).
- **`Colunas de rótulo`**: pares `coluna=valor` literais (ex: `source_type=sophos`, um por linha).

Nada mais muda. Autenticação, lote, retentativa e disjuntor continuam iguais.

:::tip[Não há conversão no meio do caminho]
O "OCSF puro" é literalmente o bloco que o pipeline já normalizou e validou contra o manifesto OCSF 1.8. Não existe uma segunda transformação aqui, de propósito: ela poderia divergir do normalizador e produzir dois OCSF diferentes para o mesmo evento.

Isso também quer dizer que a qualidade do que sai depende do mapping do fornecedor. Se um campo não está no OCSF entregue, ele não está no mapping.
:::

## Exemplo: ClickHouse com tabela flat (coluna por campo)

Você criou uma tabela modelada a partir do schema OCSF, com coluna para cada campo.

| Campo | Valor |
|---|---|
| URL | `https://198.51.100.10:8443` |
| Banco | `siem_database` |
| Tabela | `events_ocsf_native` |
| Usuário | `ocsf_ingest` |
| **Payload** | **`ocsf`** |
| **Forma da linha** | **`flat`** |

Coisas que evitam investigação longa:

**O CentralOps fala com o ClickHouse pela interface HTTP** (porta 8123 por padrão), não pelo protocolo nativo (9000). Se a documentação do seu destino só cita a porta nativa, procure a HTTP: ela costuma estar habilitada no mesmo serviço. Um destino que só publique a 9000 não é alcançável por este sink.

**`Ignorar campos desconhecidos` continua ligado por padrão.** Com `payload: ocsf` isso vira uma rede de proteção útil, porque a tabela pode não ter coluna para todo campo OCSF. Mas ele também esconde erro de modelagem: se você suspeita que está perdendo campo, desligue temporariamente e veja o que o servidor recusa.

## Exemplo: ClickHouse com tabela wrapped (evento aninhado + rótulos)

Sua tabela tem uma coluna JSON para o evento e colunas para rótulos.

| Campo | Valor |
|---|---|
| URL | `https://198.51.100.10:8443` |
| Banco | `siem_database` |
| Tabela | `events_wrapped` |
| Usuário | `ocsf_ingest` |
| **Payload** | **`ocsf`** |
| **Forma da linha** | **`wrapped`** |
| **Coluna do evento** | **`event`** |
| **Colunas de rótulo** | **`source_type=sophos_central`** (um por linha) |

Com essa configuração, a linha emitida é:

```json
{"event": {"class_uid": 1006, ...}, "source_type": "sophos_central"}
```

As colunas `event` e `source_type` precisam existir na tabela. O teste de conexão valida isso.

## Exemplo: HTTP com Bearer

| Campo | Valor |
|---|---|
| URL | `https://198.51.100.1/api/ingest` |
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
| URL | `https://198.51.100.1:8088` |
| Sourcetype | um sourcetype que descreva OCSF na sua convenção |
| Credencial | o token HEC |
| **Payload** | **`ocsf`** |

O identificador do evento continua indo em `fields`, fora do evento. Ele é metadado de transporte, serve para deduplicação no indexador, e não contamina o OCSF entregue.

## Conferindo que funcionou

Depois de salvar, use o **Testar** na página de detalhes do destino. Ele abre conexão real e reporta o que o outro lado respondeu.

Para conferir o formato, use o botão **Simular** (ícone de olho), ele mostra a linha exata que será emitida, sem entregar.

Se o consumidor aceita o lote mas os eventos aparecerem vazios ou sem classificação, o suspeito costuma ser o mapping do fornecedor, não o destino. Um evento que não passou pela normalização é entregue vazio de propósito em modo `ocsf`, justamente para não mandar o formato errado em silêncio.

## Compatibilidade

O destino HTTP tinha antes um campo `body` com os valores `envelope` e `normalized`. Ele continua funcionando: `normalized` equivale a `ocsf`. Configuração já gravada não precisa de mudança, e o campo novo é o que aparece no formulário.
