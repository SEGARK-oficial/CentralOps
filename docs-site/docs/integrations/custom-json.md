---
sidebar_position: 11
title: Fonte genérica (JSON)
description: Traga qualquer produto que exporte JSON — você cria o stream, o CentralOps cria o mapping OCSF, e o edge-collector faz POST no endpoint de ingestão
---

# Fonte genérica (JSON), `custom_json`

FortiGate e Windows Event Log chegam por [ingestão push](./push-ingestion.md) com um stream fixo cada. A **fonte genérica** usa o mesmo transporte para qualquer produto que exporte JSON — um firewall que não está no catálogo, um SaaS com webhook, um script interno. A diferença é que **os streams são seus**: você cria quantos quiser, cada um com o próprio mapping OCSF.

:::tip[O que muda em relação às outras fontes push]
Nada no transporte. O que muda é que o stream nasce **de uma definição de mapping** (`vendor = custom_json`, `event_type = custom_json.<stream>`), e não de código. Criar o stream cria o mapping; o endpoint passa a aceitá-lo na hora; workers e agendador descobrem o stream sozinhos.
:::

## Passo 1: Criar a integração

Em **Integrações → Nova integração**, escolha **Fonte genérica (JSON)**. Não há credenciais de API — a credencial é o token de ingestão, gerado no próximo passo. Uma integração por organização, como nas demais fontes push.

## Passo 2: Criar o primeiro stream

Na página da integração, o painel **Ingestão push** mostra a seção **Streams desta fonte**. Informe:

| Campo | O que é |
|---|---|
| Nome do stream | Slug curto (`a-z`, `0-9`, `_`, `-`; até 63 caracteres). Vira o caminho do endpoint: `/api/ingest/<stream>`. |
| Classe OCSF | A classe que os eventos desse stream representam. Em dúvida, use **Base Event (0)** e refine depois. |
| Descrição | Opcional. Aparece na lista de mappings. |

Ao criar, o CentralOps grava a definição de mapping e uma **versão 1** com o esqueleto OCSF válido para a classe: `class_uid`, `category_uid`, `severity_id` e `time` (do carimbo de recepção). Nada cai na quarentena por campo obrigatório ausente antes de você tocar no mapping.

Também dá para criar por API:

```bash
curl -X POST https://centralops.example.com/api/mappings/custom-streams \
  -H "Content-Type: application/json" \
  -d '{"stream": "firewall-x", "ocsf_class_uid": 4001, "description": "Firewall X"}'
```

Resposta `201` com `definition_id`, `endpoint` e `current_version_id`. Nome inválido devolve `422 mapping.invalid_stream_name`; nome repetido, `409 mapping.stream_exists`; classe fora do catálogo, `422 mapping.invalid_class_uid`.

## Passo 3: Gerar o token e enviar eventos

Gere o token no mesmo painel (ver [Ingestão push](./push-ingestion.md#passo-2-gerar-o-token-de-ingestão)). O envio é NDJSON, um objeto por linha:

```bash
curl -X POST "https://centralops.example.com/api/ingest/firewall-x" \
  -H "Authorization: Bearer <SEU_TOKEN>" \
  -H "Content-Type: application/x-ndjson" \
  --data-binary $'{"ts":"2026-09-05T12:00:00Z","src":"10.0.0.1","action":"deny"}\n{"ts":"2026-09-05T12:00:01Z","src":"10.0.0.2","action":"allow"}'
```

O token é da **integração**, não do stream: o mesmo token serve para todos os streams que você criar. Um stream que não existe devolve `404 ingest.unknown_stream`.

Com Vector, é um sink `http` apontando para o endpoint do stream — o painel gera o snippet pronto.

## Passo 4: Refinar o mapping

Em **Normalização → Mappings**, o stream aparece como `custom_json / custom_json.<stream>`. A partir das amostras que já chegaram:

1. Aponte `normalized.time` para o campo de tempo do produto (o esqueleto usa `_ingest.received_at`).
2. Mapeie os campos da classe (`src_endpoint.ip`, `action_id`, `user.name`…). O painel de **campos descobertos** lista o que os eventos trazem e o que ainda não está mapeado.
3. Faça o dry-run e publique a versão 2.

Cada stream é um mapping independente: um produto com dois tipos de evento (tráfego e autenticação, por exemplo) fica melhor em dois streams do que em um só com classe `Base Event`.

## Como os streams se propagam

O registro de coletores consulta o banco (`mapping_definitions` com `vendor = custom_json`) antes de responder que um stream não existe, com cache de 5 segundos por processo. Assim:

- o processo da API que criou o stream o conhece na hora;
- workers e o agendador o veem no próximo ciclo;
- ao criar um stream, as integrações `custom_json` ativas são re-registradas no agendador para ganhar a entrada de drenagem do stream novo (best-effort — a reconciliação periódica corrige se o Redis estiver fora naquele instante).

Streams são **globais** (como toda definição de mapping): criados uma vez, valem para todas as organizações; cada organização tem a própria integração e o próprio token.

## Solução de problemas

| Sintoma | Causa provável |
|---|---|
| `404 ingest.unknown_stream` logo após criar | Você está enviando para outro nó da API dentro da janela de 5 s. Tente de novo. |
| Eventos aceitos mas nada nos destinos | A versão atual do mapping não emite o que a rota espera. Veja a quarentena e o dry-run do mapping. |
| `422 mapping.invalid_class_uid` | A classe não está no catálogo OCSF suportado. Use `0` (Base Event) ou uma das listadas na tela. |
