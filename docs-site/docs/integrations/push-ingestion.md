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

## Quem pode fazer o quê

- **Administrador da plataforma:** cria e edita a integração push (menu **Visão geral -> Integrações**), emite e rotaciona tokens.
- **Operador de infraestrutura:** configura o edge-collector (Vector ou Fluent Bit) perto da fonte, usando o endpoint e o token fornecidos.
- **Demais perfis:** visualizam e investigam os eventos coletados em **Operação -> Investigações**, como com qualquer outra fonte.

## Passo 1: Criar a integração no CentralOps

1. No menu lateral, vá em **Visão geral -> Integrações**.
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

1. Abra **Visão geral -> Integrações** e selecione a integração.
2. No painel **Ingestão push**, você verá:
   - **Buffer atual** — quantos eventos estão na fila aguardando processamento.
   - **Taxa de ingestão** — eventos recebidos nos últimos minutos.
   - **Drops** (se houver) — quantos eventos foram descartados por limite de fila.

3. Após ~20 segundos, os eventos devem ficar pesquisáveis em **Operação -> Investigações**.

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
- Do CentralOps até os destinos: ~15 segundos (fila de drenagem).
- **Total: ~20 segundos** em condições normais.

## Proteção contra sobrecarga

Se o edge-collector enviar eventos mais rápido do que o CentralOps consegue processar:

- Os eventos são colocados numa **fila de buffer** (capacidade padrão: ~10k eventos).
- Quando o buffer atinge o limite, os **eventos mais antigos são descartados** (FIFO) para proteger a plataforma.
- Cada resposta HTTP inclui `"buffer_depth"` e `"dropped"` — você pode monitorar isso no seu edge-collector.

Exemplo de resposta:

```json
{
  "accepted": 100,
  "dropped": 5,
  "buffer_depth": 2500
}
```

Se `dropped` for maior que zero com frequência, significa que o edge-collector está enviando mais rápido do que o CentralOps processa. Nesse caso:
- Reduza a taxa de ingestão no edge-collector (ajuste batch size ou flush interval).
- Verifique se há gargalos nos destinos (Splunk, S3, etc. podem estar lentos).
- Fale com o administrador da plataforma sobre aumentar a capacidade.

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

Os eventos são normalizados para o formato padrão do CentralOps, de modo que podem ser pesquisados e correlacionados com as demais fontes. Se um campo não puder ser convertido, o evento vai para a **Quarentena** (menu **Normalização -> Quarentena**), onde você pode revisar e reprocessar.

## Solução de problemas

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| **Edge-collector não consegue conectar** | URL incorreta ou firewall bloqueando HTTPS para o CentralOps | Confirme que a URL está correta e completa (com `https://`). Verifique se há firewall bloqueando a porta 443 para o CentralOps. Teste: `curl -v https://sua-url/api/ingest/traffic`. |
| **Erro 401 ou 403 ao enviar** | Token inválido, expirado ou já rotacionado | Confirme o token na integração. Se foi rotacionado recentemente, atualize o edge-collector com o novo token. |
| **Buffer muito alto (muitos drops)** | Edge-collector enviando mais rápido que o CentralOps processa | Reduza a taxa de ingestão no edge-collector (batch size, flush interval). Verifique se os destinos (Splunk, S3, Kafka) estão lentos em **Operação -> Destinos**. |
| **Nenhum evento aparecendo** | Edge-collector não está enviando ou eventos estão na quarentena | Confirme que o edge-collector está rodando e enviando (`curl` do teste acima). Se o teste retorna `"accepted":1`, mas os eventos não aparecem em **Operação -> Investigações**, verifique **Normalização -> Quarentena**. |
| **Muitos eventos na quarentena** | Problema de normalização ou mapeamento de campos | Abra **Normalização -> Quarentena**, selecione um evento da integração push e veja o motivo do erro. Corrija o mapeamento de campos na integração se necessário. |

## Próximos passos

- **Configurar o edge-collector específico:** veja [Fortinet FortiGate](./fortinet-fortigate.md) ou [Windows Event Log](./windows-event-log.md).
- **Eventos aparecendo?** Veja a [Quarentena](../operations/quarantine.md) para entender como lidar com eventos não normalizados.
- **Monitorar a saúde da integração:** abra **Normalização -> Saúde do Pipeline** para métricas detalhadas.
- **Rotear eventos para destinos:** use a tela de [Roteamento](../outputs/routing.md) para definir para onde os eventos vão após normalizados.
