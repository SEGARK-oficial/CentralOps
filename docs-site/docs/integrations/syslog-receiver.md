---
sidebar_position: 12
title: Receptor syslog nativo
description: Aponte o firewall, o servidor Linux ou o WEC direto para o CentralOps em UDP/TCP 514 ou TLS 6514, sem edge-collector — a rede é a credencial, o conteúdo decide o stream
---

# Receptor syslog nativo

A [ingestão push](./push-ingestion.md) recebe JSON por HTTPS de um edge-collector. O **receptor syslog** é o caminho sem agente: o aparelho fala syslog direto com o CentralOps, o receptor parseia (RFC 3164, RFC 5424 com structured-data, CEF e LEEF) e escreve no **mesmo buffer** da integração push. Dali em diante o pipeline não sabe por onde o evento entrou.

:::tip[Quando usar cada um]
**Receptor nativo**: POC, aparelhos que só falam syslog (FortiGate, ASA, PAN-OS, pfSense, Linux `rsyslog`), ambientes sem lugar para rodar um agente. **Edge-collector (Vector)**: escala (muitas fontes, muitos GB/dia), buffer em disco na borda, ou quando você já tem um coletor na rede. Os dois convivem — a integração é a mesma.
:::

## Como funciona

1. O receptor escuta **UDP 514**, **TCP 514** e **TLS 6514** (portas configuráveis).
2. Cada mensagem é atribuída a uma **fonte** pelo IP de origem: a fonte é um CIDR cadastrado numa integração push (`fortinet_fortigate`, `windows_event_log` ou a [fonte genérica](./custom-json.md)). O CIDR mais específico vence; porta e transporte podem restringir mais.
3. Sem fonte para o IP, a mensagem é **descartada e contada** (`collector_syslog_received_total{outcome="unknown_source"}`). Porta 514 não tem cabeçalho de autenticação: a rede é a credencial, e aceitar de qualquer IP seria aceitar da internet inteira. Por isso `0.0.0.0/0` é recusado no cadastro.
4. O **classificador** da fonte decide o `stream` pelo conteúdo. Sem regra que case, vale o stream padrão da fonte.
5. O evento parseado ganha o carimbo `_ingest` (id por conteúdo, `received_at`, `transport: "syslog"`, `peer`) e vai ao buffer da integração; o collector virtual drena a cada ~20 s.

## Subir o receptor

No `docker-compose`, o serviço é opcional e vive no perfil `syslog`:

```bash
docker compose --profile syslog up -d syslog-receiver
```

Variáveis:

| Variável | Padrão | O que faz |
|---|---|---|
| `SYSLOG_UDP_PORT` / `SYSLOG_TCP_PORT` | `514` | Porta publicada; `0` desliga o listener |
| `SYSLOG_TLS_PORT` | `6514` | Só abre com `SYSLOG_TLS_CERT` e `SYSLOG_TLS_KEY` |
| `SYSLOG_TLS_CA` | vazio | Se definido, liga **mTLS**: só quem apresenta certificado assinado por essa CA fala com a porta |
| `SYSLOG_SOURCE_REFRESH_S` | `15` | Em quanto tempo uma fonte nova/alterada passa a valer |
| `SYSLOG_MAX_LINE_BYTES` | `65536` | Linhas maiores são truncadas e contadas |

O receptor é **uma réplica**: a porta UDP não é compartilhável entre processos. Para escalar, ponha um balanceador L4 na frente ou use o edge-collector.

## Cadastrar uma fonte

Na página da integração push, o painel **Fontes syslog** pede:

| Campo | O que é |
|---|---|
| CIDR de origem | `203.0.113.7/32` para um aparelho, `10.0.5.0/24` para uma rede. Obrigatório e nunca aberto. |
| Porta / transporte | Opcionais. Servem para separar duas fontes que compartilham a rede (ex.: firewall em UDP 514, servidores em TCP 1514). |
| Stream padrão | Para onde vai o que nenhuma regra classifica. Para a fonte genérica, crie os streams antes. |
| Regras de classificação | Lista ordenada de `when → stream`. A primeira que casa vence. |

`when` aceita um **detector de fábrica** por nome ou uma expressão [JMESPath](https://jmespath.org) sobre a linha parseada:

| Detector | Casa |
|---|---|
| `@fortigate` | syslog key=value do FortiGate (`devname=`, `logid=`) |
| `@paloalto` | CSV do PAN-OS (`,TRAFFIC,`, `,THREAT,`, `,SYSTEM,`, `,CONFIG,`) |
| `@cisco_asa` | `%ASA-n-nnnnnn` / `%FTD-` |
| `@pfsense` | `filterlog` do pfSense/OPNsense |
| `@linux_auth` | `sshd`, `sudo`, `su`, `login`, facility `auth`/`authpriv` |
| `@windows_vector` | Windows Event Log reenviado por Vector/NXLog (`EventID`, `Microsoft-Windows-Security-Auditing`) |

Os campos disponíveis para JMESPath são os da saída do parser: `format`, `pri`, `facility`, `facility_name`, `severity`, `severity_name`, `timestamp`, `host`, `app`, `procid`, `msgid`, `sd` (structured-data, por id), `msg`, `cef`/`leef` (quando presentes) e `raw`. Exemplos: `app == 'haproxy'`, `contains(msg, 'DENY')`, `sd."meta@1234".env == 'prod'`.

Antes de salvar, use **Testar uma linha**: cole exatamente o que o aparelho manda e veja o formato reconhecido, os campos extraídos e o stream escolhido (`POST /api/syslog/classify-test`).

Via API: `GET/POST /api/syslog/sources`, `PATCH/DELETE /api/syslog/sources/{id}`, `GET /api/syslog/classifiers` (detectores com a expressão real).

## Configurar o aparelho

- **Linux (rsyslog)**: `*.* @@centralops.example.com:514` (TCP) ou `@` para UDP. Para TLS, `omfwd` com `StreamDriver="gtls"` na 6514.
- **FortiGate**: `config log syslogd setting` → `set server <ip>`, `set mode udp` (ou `reliable` para TCP), formato `default`. Crie a fonte com `@fortigate → traffic`.
- **Windows**: o receptor não fala WEF; use Vector/NXLog no coletor WEC com output syslog, ou o caminho HTTPS da [integração WEC](./windows-event-log.md).

## O que chega ao mapping

Um evento RFC 5424 vira, por exemplo:

```json
{
  "format": "rfc5424", "pri": 165, "facility": 20, "facility_name": "local4",
  "severity": 5, "severity_name": "notice", "version": 1,
  "timestamp": "2026-09-06T01:02:03.123Z", "host": "fw01", "app": "evntslog",
  "procid": "1234", "msgid": "ID47",
  "sd": {"exampleSDID@32473": {"iut": "3", "eventSource": "App"}},
  "msg": "An application event", "raw": "<165>1 2026-09-06T01:02:03.123Z fw01 ...",
  "_ingest": {"id": "…", "received_at": "…", "stream": "traffic", "transport": "udp", "peer": "10.0.5.7"}
}
```

No RFC 3164 não há ano nem fuso: o receptor assume o ano corrente e UTC e marca `ts_inferred: true`. Aponte `normalized.time` para `timestamp` e, se o aparelho tiver relógio confiável, prefira o campo dele (`date`/`time` do FortiGate, por exemplo). Linha que não casa formato nenhum **não é descartada**: chega como `format: "unknown"` com `msg` e `raw` intactos, para você ver o que o aparelho manda.

## Solução de problemas

| Sintoma | Causa provável |
|---|---|
| `unknown_source` crescendo na métrica | Fonte não cadastrada, CIDR errado, ou NAT trocou o IP de origem. O log do receptor diz o IP (1 aviso por IP por minuto). |
| `unclassified` crescendo | A regra aponta para um stream que foi apagado. Corrija a fonte. |
| Chega em UDP mas não em TCP | Firewall entre o aparelho e o receptor; ou o aparelho manda octet-count e a porta está atrás de um proxy que não repassa TCP cru. |
| TLS não abre | Faltou `SYSLOG_TLS_CERT`/`KEY` no serviço; o log diz "porta TLS NÃO aberta". |
