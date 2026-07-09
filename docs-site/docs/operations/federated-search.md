---
sidebar_position: 5
title: Busca federada
description: Consulte várias integrações ao vivo com um comando nativo, em dialetos como OpenSearch DSL, FQL ou KQL, e acompanhe os resultados por fonte
---

# Busca federada

A tela **Busca federada** permite enviar um comando de busca nativo (SQL, DSL, FQL, KQL ou filtros estruturados) para várias integrações ao mesmo tempo, sem reingerir dados. O job roda de forma **assíncrona** — você submete, acompanha o progresso por fonte e recebe o resultado parcial ou completo quando pronto.

Para acessar, use o menu **Operação → Busca federada**.

**Quando usar:** quando você precisa fazer uma busca sofisticada que exige sintaxe nativa de cada plataforma (por exemplo, uma agregação OpenSearch DSL no Wazuh e uma consulta FQL no CrowdStrike ao mesmo tempo), ou quando a janela de dados nas **Investigações** não é suficiente e você precisa buscar ao vivo.

**Quem pode ver:** perfis com permissão `query.run` (Operator ou superior), org-scoped (cada um vê só sua organização).

## Antes de começar

- **Qual integração você quer buscar?** Verifique que está ativa em **Operação → Integrações**.
- **Qual dialeto usa?** Cada plataforma tem seu próprio:
  - **Wazuh** — OpenSearch DSL
  - **Sophos** — XDR Data Lake (assíncrono, até 30 dias)
  - **CrowdStrike** — FQL (Falcon Query Language)
  - **Microsoft Defender** — KQL (Kusto Query Language)
  - **S3/Lake** — Filtros JSON estruturados (não SQL)
- **Qual é a janela de tempo?** Defina `De` e `Até` em ISO 8601 (ex.: `2026-06-22T00:00:00Z`).

## A tela

Três áreas principais:

1. **Seletor de integrações** — marque as que quer consultar. Todas devem estar na mesma organização.
2. **Caixa de diálogo** — escolha o dialeto (sistema detecta as capacidades de cada integração), defina a janela de tempo, digite o statement.
3. **Monitor de jobs** — lista o status geral (submitted/running/finished/partial/failed) e o progresso por integração.

## Passo a passo

### Submeter uma busca

1. Abra **Operação → Busca federada**.
2. Marque as integrações que quer consultar (ex.: Wazuh + CrowdStrike).
3. No seletor **Dialeto**, escolha (o sistema sugere com base nas integrações selecionadas).
4. Defina a janela:
   - **De** — data/hora inicial (ISO 8601)
   - **Até** — data/hora final
5. Na caixa de edição, digite o statement no dialeto escolhido.
6. Opcionalmente, marque **Permitir resultados parciais** se não quer esperar que todos retornem.
7. Clique **Executar**.
8. A tela mostra o `job_id` e muda para o modo de acompanhamento.

### Acompanhar o progresso

Enquanto o job roda:

- **Status geral** (no topo): `submitted` (na fila) → `running` (em andamento) → `finished` ou `partial`.
- **Por integração**: cada linha mostra:
  - Nome da integração
  - Estado (`submitted`, `running`, `finished`, `partial`, `error`)
  - Contagem de resultados encontrados
  - Erro (se houver)

Se selecionou **Permitir resultados parciais**, o sistema retorna assim que houver dados, mesmo que algumas fontes ainda estejam processando.

### Revisar resultados

Quando pronto, clique no resultado para expandir e ver:

- **Total de resultados** — soma de todas as integrações.
- **Detalhe por fonte** — quantos cada integração retornou, com a contagem e erros específicos.
- **Dados brutos** — os eventos/registros encontrados (formato depende do dialeto).

## Exemplos por dialeto

### OpenSearch DSL (Wazuh)

Busca eventos com campo `action: login` e severidade alta, últimas 24 horas:

```json
{
  "query": {
    "bool": {
      "must": [
        { "term": { "action": "login" } },
        { "range": { "timestamp": { "gte": "now-24h" } } }
      ]
    }
  }
}
```

### FQL (CrowdStrike)

Busca detecções com nome de arquivo contendo "ransomware":

```
event_type:detection AND filename:*ransomware*
```

### KQL (Microsoft Defender)

Busca logins falhados de um IP específico:

```
DeviceLogonEvents | where ActionType == "LogonFailed" | where RemoteIP == "1.2.3.4"
```

### XDR Data Lake (Sophos)

Busca eventos de processo em um host específico (assíncrono, até 30 dias):

```json
{
  "dataSource": "process",
  "host": "hostname-here",
  "limit": 100
}
```

### Filtros JSON (Lake/S3)

Busca estruturada sem SQL — útil para S3 ou data lake consolidado:

```json
{
  "filters": [
    { "field": "severity", "op": "gte", "value": 4 },
    { "field": "event_type", "op": "eq", "value": "detection" }
  ],
  "limit": 1000
}
```

## Resultados parciais

Se uma integração demorar ou houver erro em uma delas:

- **Permitir resultados parciais = ON**: o job termina assim que houver dados, você vê quantos vieram de cada uma e qual falhou.
- **Permitir resultados parciais = OFF**: o job espera todas. Se uma falhar, o resultado sai como `failed` com mensagem de erro específica.

Escolha a opção conforme sua urgência — triagem rápida prefere parcial; investigação completa prefere esperar tudo.

## Limites e quotas

- **Por minuto:** há um limite de queries por organização (verificado no submissão; excesso retorna 429).
- **Duração:** jobs são cancelados se ultrapassarem o tempo limite (geralmente 5 min para Wazuh/CrowdStrike, 30 min para Sophos XDR).
- **Resultados:** cada integração tem seu próprio limite (ex.: Wazuh retorna até 10 mil registros por query).

Se receber erro de quota ou timeout, revise o statement (torne mais específico) ou divida em duas buscas.

## Quem pode fazer o quê

| Ação | Permissão | Perfil |
|------|-----------|--------|
| Enviar query ao vivo | `query.run` | Operator+ |
| Salvar query como regra de correlação | `query.save` | Engineer+ |
| Ver resultados | — | Qualquer um na organização |

## Casos de prático

| Cenário | Como fazer |
|---------|-----------|
| **IOC em múltiplas plataformas** | Selecione Wazuh + CrowdStrike, use o dialeto de cada uma e busque pelo hash/IP/domínio |
| **Investigar lateral movement** | Busque eventos de autenticação e processo no Defender, eventos de rede no Wazuh, dados de endpoint no CrowdStrike |
| **Auditar acesso a recurso crítico** | Busque por nome de arquivo (S3) + eventos de acesso (Wazuh) + atividade de conta (Defender) |
| **Confirmar campanha** | Busque o mesmo indicador em Sophos (XDR) + CrowdStrike (FQL) — se aparecer em ambas, é atacante real, não falso positivo |

## Próximos passos

- **Quer salvar essa busca para reutilizar?** Salve-a como uma **Regra de Correlação** em **Conhecimento → Correlação** (requer `query.save`).
- **Precisa revisar alertas de detecção?** Vá em **Operação → Detecções**.
- **Quer buscar dados já entregues (não ao vivo)?** Use **Operação → Investigações** (mais rápido, sem custo).
- **Dados mais antigos?** Consulte diretamente o destino (ex.: Kibana para Elastic, Splunk para Splunk).
