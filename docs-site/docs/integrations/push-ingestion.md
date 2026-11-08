---
sidebar_position: 8
title: Ingestão push (edge-collector)
description: Configure uma coletor de borda para enviar eventos em tempo real ao CentralOps via HTTPS
---

# Ingestão push (edge-collector)

Algumas fontes de segurança não têm uma API de polling — elas **empurram eventos** para você em tempo real. O CentralOps recebe esses eventos através de um **edge-collector** (Vector ou Fluent Bit) que você executa perto da fonte e que os encaminha via HTTPS para o CentralOps.

## Quando usar

- **Eventos em tempo real do FortiGate ou Windows Event Log.** Em vez de aguardar coletas agendadas, você recebe os eventos conforme acontecem.
- **Reduzir latência de detecção.** Os eventos chegam ao CentralOps em menos de 20 segundos após serem gerados, permitindo resposta mais rápida.
- **Coletar de fontes sem API.** Produtos como FortiGate (syslog) e Windows Event Log (WEC/WEF) só enviam dados via push nativo.

Se o produto não está no catálogo mas exporta JSON, use a [fonte genérica](./custom-json.md): você cria o stream, o CentralOps cria o mapping, e o transporte é este mesmo.

Aparelho que só fala syslog e nenhum lugar para rodar um agente? O [receptor syslog nativo](./syslog-receiver.md) escuta 514/6514 direto e escreve no mesmo buffer desta integração.

## Quem pode fazer o quê

- **Administrador da plataforma:** cria e edita a integração push (menu **Coleta -> Integrações**), emite e rotaciona tokens.
- **Operador de infraestrutura:** configura o edge-collector (Vector ou Fluent Bit) perto da fonte, usando o endpoint e o token fornecidos.
- **Demais perfis:** acompanham a coleta em **Visão geral -> Saúde do pipeline** e pesquisam os eventos no destino, como com qualquer outra fonte.

## Passo 1: Criar a integração no CentralOps

1. No menu lateral, vá em **Coleta -> Integrações**.
2. Clique para adicionar uma nova integração.
3. Escolha **Fortinet FortiGate** ou **Windows Event Log (WEC)** na lista.
4. Preencha os campos:

   | Campo | O que informar |
   |---|---|
   | **Nome** | Um nome para identificar esta conexão (ex.: "FortiGate - Filial SP"). |
   | **Organização** | Selecione a organização para a qual a integração vale. |

5. Salve a integração. Não é necessário fornecer credenciais de polling — apenas nome e organização.

## Passo 2: Gerar o token de ingestão

1. Na tela da integração, abra a aba **Visão geral** e localize o painel **Ingestão push**.
2. Clique em **Emitir token** para gerar um novo token.
3. O token é mostrado **uma única vez**. Copie e guarde em local seguro (você vai precisar configurar no edge-collector).

   :::warning[Copie o token na hora]

   Após fechar o painel, o token não será exibido novamente. Se precisar dele depois, use **Rotacionar token** (que invalida o anterior e emite um novo).

   :::

## Passo 3: Configurar o edge-collector

O painel **Ingestão push** mostra um snippet de configuração pronto para copiar no seu edge-collector.

**Exemplo com Vector:**

```toml
[sources.fortinet_syslog]
type = "syslog"
address = "0.0.0.0:514"
protocol = "udp"

[sinks.centralops_push]
type = "http"
uri = "https://sua-url/api/ingest/traffic"
encoding.codec = "json"
method = "post"
headers.Authorization = "Bearer coi_3_xxxxxxxxxxxxx"
```

Substitua:
- `sua-url` pela URL do seu CentralOps.
- `coi_3_xxxxxxxxxxxxx` pelo token que você copiou no Passo 2.
- O stream (`fortinet-fortigate`, `windows-event-log`, etc.) deve corresponder à integração.

Para instruções de instalação do Vector ou Fluent Bit, veja a documentação específica:

- [Fortinet FortiGate](./fortinet-fortigate.md)
- [Windows Event Log (WEC)](./windows-event-log.md)

## Passo 4: Verificar a ingestão

Assim que o edge-collector começar a enviar eventos:

1. Abra **Coleta -> Integrações** e selecione a integração.
2. No painel **Ingestão push**, você verá:
   - **Buffer atual** — eventos aguardando na fila do Redis. Com o data-plane Kafka (o padrão), fica em 0: os eventos esperam no log, e o atraso aparece nas métricas do processamento.
   - **Taxa de ingestão** — eventos recebidos nos últimos minutos.
   - **Drops** (se houver) — quantos eventos foram recusados com `503` por fila cheia. O edge-collector re-envia esses eventos; eles não se perdem enquanto o buffer de disco dele tiver espaço.

3. Após ~20 segundos, os eventos devem aparecer no destino para onde suas rotas os enviam.

Se nada aparecer após alguns minutos, verifique a seção [Solução de problemas](#solução-de-problemas).

## Segurança do token

- O token é a **única credencial necessária** para o edge-collector enviar eventos — não é necessária sessão de usuário.
- O CentralOps armazena apenas um **hash** do token, nunca o valor legível.
- Cada token é **único por integração** — comprometer um token só afeta aquela integração.
- Use **Rotacionar token** para invalidar um token antigo e emitir um novo sem downtime (o edge-collector pode estar usando o antigo enquanto você faz essa operação).

## Fluxo de dados e latência

Os eventos seguem este caminho:

```
Fonte (FortiGate/Windows Event Log)
  → Edge-collector (Vector/Fluent Bit)
    → POST HTTPS para {sua-url}/api/ingest/<stream>
      → CentralOps normaliza e roteiza (mesma pipeline das fontes de polling)
        → Eventos normalizados (ou Quarentena se houver erro de normalização)
          → Destinos configurados (Splunk, S3, Kafka, etc.)
```

**Latência típica:**

- Do evento gerado até o edge-collector: depende do protocolo (syslog é praticamente imediato).
- Do edge-collector até o CentralOps: ~2–5 segundos (HTTPS + TLS handshake).
- Do CentralOps até os destinos: alguns segundos (o processamento lê o log continuamente).
- **Total: ~20 segundos** em condições normais.

## Proteção contra sobrecarga

Um evento push só é confirmado depois de gravado de forma durável:

- A resposta `200` (ou `207`) sai **depois** que o lote foi gravado no log do data-plane (Kafka), com confirmação das réplicas. Um `200` quer dizer que o evento está guardado, mesmo que o processamento esteja parado.
- Se o data-plane estiver indisponível, a requisição é **recusada com `503`** e um cabeçalho `Retry-After`. Nada daquele lote é gravado.
- O edge-collector re-tenta sozinho: Vector e Fluent Bit tratam `503` como falha temporária e guardam os eventos no buffer de disco deles até o CentralOps voltar a aceitar.
- O processamento lê o log no próprio ritmo. Se ele atrasar, os eventos esperam no log (24 horas por padrão) em vez de serem recusados. Aumentar o número de processadores aumenta a vazão de push.

Exemplo de resposta aceita:

```json
{
  "accepted": 100,
  "dropped": 0,
  "buffer_depth": 0
}
```

Se o edge-collector recebe `503` com frequência, o data-plane está fora ou instável. Nesse caso:
- Confirme que o buffer de disco do edge-collector tem espaço para segurar os eventos durante a espera.
- Fale com o administrador da plataforma: a saúde do broker e o atraso do processamento aparecem nas métricas do data-plane.

:::note[Instalação sem broker]
Em ambiente de desenvolvimento sem Kafka (`EVENT_DATAPLANE=celery`), os eventos ficam numa fila no Redis com capacidade de 100 mil por integração e stream. Com a fila cheia, a requisição é recusada com `503` e nenhum evento já aceito é apagado.
:::

## Outros formatos: Splunk HEC e OpenTelemetry

Além do JSON/NDJSON, o mesmo token de ingestão aceita dois formatos que muitos agentes já falam. As garantias são as mesmas: resposta de sucesso só depois que o lote está gravado, `503` com `Retry-After` quando não está, e corpo com `Content-Encoding: gzip` aceito.

| Formato | URL a configurar no agente | Autenticação |
| --- | --- | --- |
| Splunk HEC (eventos) | `https://<host>/api/ingest/hec/<stream>` (o agente acrescenta `/services/collector/event`) | `Authorization: Splunk <token de ingestão>` |
| Splunk HEC (raw, uma linha por evento) | `https://<host>/api/ingest/hec/<stream>/services/collector/raw` | `Authorization: Splunk <token de ingestão>` |
| OTLP/HTTP logs (JSON ou protobuf) | `https://<host>/api/ingest/otlp/<stream>` (o exporter acrescenta `/v1/logs`) | `Authorization: Bearer <token de ingestão>` |

- **HEC:** o campo `event` vira o evento (objeto) ou `message` (texto). `host`, `source`, `sourcetype`, `index`, `time` e `fields` ficam em `_hec`. O healthcheck `/services/collector/health` responde como o do Splunk.
- **OTLP:** cada log record vira um evento. Corpo em mapa vira os campos do evento; corpo em texto vira `message`. Atributos, recurso, escopo, severidade, tempos e trace ficam em `_otlp`.

## Teste rápido com curl

Para confirmar que o token funciona:

```bash
curl -XPOST "https://sua-url/api/ingest/traffic" \
  -H "Authorization: Bearer coi_3_xxxxxxxxxxxxx" \
  -H "Content-Type: application/x-ndjson" \
  --data-binary '{"srcip":"10.0.0.1","action":"accept"}'
```

Resposta esperada:

```json
{
  "accepted": 1,
  "dropped": 0,
  "buffer_depth": 1
}
```

Se receber `401` ou `403`, o token está inválido ou expirado — rotacione para gerar um novo.

## O que é coletado

Cada integração push coleta tipos específicos de eventos:

| Integração | Tipos de evento | Referência |
|---|---|---|
| **Fortinet FortiGate** | Logs de firewall (tráfego, ameaças, sessões) | [Fortinet FortiGate](./fortinet-fortigate.md) |
| **Windows Event Log (WEC)** | Eventos do Windows (segurança, sistema, aplicação) | [Windows Event Log](./windows-event-log.md) |

Os eventos são normalizados para o formato padrão do CentralOps, de modo que podem ser pesquisados e correlacionados com as demais fontes. Se um campo não puder ser convertido, o evento vai para a **Quarentena** (menu **Normaliza -> Quarentena**), onde você pode revisar e reprocessar.

## Solução de problemas

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| **Edge-collector não consegue conectar** | URL incorreta ou firewall bloqueando HTTPS para o CentralOps | Confirme que a URL está correta e completa (com `https://`). Verifique se há firewall bloqueando a porta 443 para o CentralOps. Teste: `curl -v https://sua-url/api/ingest/traffic`. |
| **Erro 401 ou 403 ao enviar** | Token inválido, expirado ou já rotacionado | Confirme o token na integração. Se foi rotacionado recentemente, atualize o edge-collector com o novo token. |
| **Buffer muito alto (muitos drops)** | Edge-collector enviando mais rápido que o CentralOps processa | Reduza a taxa de ingestão no edge-collector (batch size, flush interval). Verifique se os destinos (Splunk, S3, Kafka) estão lentos em **Roteia -> Destinos**. |
| **Nenhum evento aparecendo** | Edge-collector não está enviando ou eventos estão na quarentena | Confirme que o edge-collector está rodando e enviando (`curl` do teste acima). Se o teste retorna `"accepted":1`, mas os eventos não aparecem em **Detecta -> Queries salvas**, verifique **Normaliza -> Quarentena**. |
| **Muitos eventos na quarentena** | Problema de normalização ou mapeamento de campos | Abra **Normaliza -> Quarentena**, selecione um evento da integração push e veja o motivo do erro. Corrija o mapeamento de campos na integração se necessário. |

## Próximos passos

- **Configurar o edge-collector específico:** veja [Fortinet FortiGate](./fortinet-fortigate.md) ou [Windows Event Log](./windows-event-log.md).
- **Eventos aparecendo?** Veja a [Quarentena](../operations/quarantine.md) para entender como lidar com eventos não normalizados.
- **Monitorar a saúde da integração:** abra **Visão geral -> Saúde do pipeline** para métricas detalhadas.
- **Rotear eventos para destinos:** use a tela de [Roteamento](../outputs/routing.md) para definir para onde os eventos vão após normalizados.
