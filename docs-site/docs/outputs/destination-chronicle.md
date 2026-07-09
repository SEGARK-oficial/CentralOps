---
sidebar_position: 19
title: "Destino: Google SecOps (Chronicle)"
description: Integre com o SIEM next-gen do Google — Chronicle ingere eventos normalizados via API REST.
---

# Destino: Google SecOps (Chronicle)

O destino **Google SecOps (Chronicle)** encaminha seus eventos normalizados para o Chronicle, o SIEM na nuvem do Google. Use-o quando sua infraestrutura ou seus clientes estão centralizados na plataforma Google Cloud e você precisa de um SIEM moderno e nativo da nuvem.

Esta tela só aparece para administradores da plataforma.

## Quando usar

- **SIEM nativo da GCP.** Você já usa Google Cloud Platform e quer um SIEM que se integre naturalmente com VPC, Cloud Logging, Cloud Audit e outras soluções Google.
- **Mandatório em ambientes Google.** Seu cliente ou regulação exigem que os dados de segurança fiquem no Google Cloud (ex.: contract com cloud provider específico).
- **Correlação com Google Cloud Security Command Center (SCC).** Chronicle integra com SCC para uma visão unificada de segurança na Google Cloud.

## O que você precisa antes de começar

- **GCP Project ID** — o ID do projeto Google Cloud que hospeda a instância Chronicle.
- **Customer/Instance ID** — um GUID que identifica a instância Chronicle dentro do projeto (gerado quando você cria a instância Chronicle).
- **Region** (padrão: `us`) — onde o Chronicle reside:
  - `us` — Estados Unidos.
  - `europe` — Europa.
  - `asia-southeast1` — Ásia/Pacífico.
  - Consulte a [documentação do Chronicle](https://cloud.google.com/chronicle/docs) para sua região.
- **Log type** (padrão: `UDM`) — o tipo de log de destino no Chronicle:
  - `UDM` — Unified Data Model (recomendado; suporta o schema OCSF).
  - `OKTA`, `WINEVTLOG`, ou outros tipos customizados configurados no seu Chronicle.
- **Service Account JSON** — credencial OAuth2 para autenticar na Google Cloud. Precisa do escopo `cloud-platform`.

> A service account e o Chronicle instance são provisionados pela equipe de GCP. No CentralOps você apenas informa os identificadores e faz upload do JSON.

## Criar o destino

1. No menu lateral, abra **Operação → Destinos**.
2. Use a opção de criar um novo destino.
3. Escolha o tipo **Google SecOps (Chronicle)**.
4. Preencha os campos abaixo.

| Campo | O que informar |
|-------|----------------|
| **Nome** | Um nome claro (ex.: "Chronicle Prod", "Google SIEM"). |
| **GCP Project** | O project ID do Google Cloud (ex.: `my-project-12345`). |
| **Instance ID** | O GUID da instância Chronicle (ex.: `550e8400-e29b-41d4-a716-446655440000`). |
| **Region** | A região do Chronicle (`us`, `europe`, `asia-southeast1`, etc.). |
| **Location** | Location do recurso, geralmente igual à region. |
| **Log Type** | O tipo de log de destino (`UDM` é o padrão para OCSF). |
| **Forwarder** | Nome do forwarder (opcional; deixe em branco se não usar). |
| **Service Account JSON** | Coloque o arquivo JSON da service account (criptografado). |

### Preparar a credencial

1. Na console do Google Cloud, navegue para **IAM & Admin → Service Accounts**.
2. Selecione ou crie uma service account com permissão `roles/chronicle.logsWriter` (ou equivalente para seu Chronicle instance).
3. Gere uma chave JSON e baixe o arquivo.
4. No CentralOps, cole o conteúdo completo do JSON na credencial.

### Testar e salvar

Antes de salvar, use o botão **Testar conexão**. O CentralOps verifica:

- se consegue carregar o JSON de service account;
- se consegue obter um token OAuth2;
- se consegue alcançar o Chronicle no region/project especificado.

Se o teste passar, salve. O destino fica **ativo** (badge verde).

## Como os eventos são entregues

- **Envio em lotes.** Os eventos são agrupados para eficiência (limite da API: ~4 MB por requisição).
- **Formato base64 OCSF.** O evento normalizado é codificado em base64 e enviado dentro de um payload `logs:import` do Chronicle. O Chronicle decodifica e injeta no seu datamodel.
- **Nova tentativa automática.** Falhas transitórias (timeouts, 5xx, 429) disparam reenvio automático.
- **Entrega ao menos uma vez.** Em caso de queda, um evento pode chegar duplicado no Chronicle. Use o ID único em `_centralops.event_id` para deduplicar.
- **Proteção contra destino instável.** Se o Chronicle falhar persistentemente, o CentralOps pausa o envio e retoma automaticamente.

## Acompanhar a saúde do destino

Abra **Operação → Destinos** e selecione o seu destino Chronicle.

O badge de saúde mostra:

| Cor | Significado |
|-----|-------------|
| Verde | Eventos sendo entregues normalmente. |
| Amarelo | Eventos chegando, mas há itens na fila de reenvio. |
| Vermelho | Envio pausado ou Chronicle indisponível. |
| Cinza | Destino desativado. |

Na visão do destino você acompanha:

- **Eventos por segundo** — taxa de entrega na última hora.
- **Latência média** — tempo de resposta do Chronicle.
- **Itens na fila de reenvio (24h)** — eventos recusados.

Para ver o que não foi entregue, abra a **fila de reenvio**. Cada item mostra o motivo (ex.: "service account expirada", "HTTP 403") e o conteúdo, útil para debugar.

## Resolver problemas comuns

| Sintoma | O que verificar |
|---------|-----------------|
| **"Service account JSON ausente"** | A credencial não foi resolvida. Confirme que o JSON foi guardado no cofre. Use o botão testar a conexão. |
| **"Credencial inválida" / "401 Unauthorized"** | O JSON está corrompido, expirou ou a service account não tem a permissão `roles/chronicle.logsWriter`. Verifique na console do Google Cloud. |
| **"Project/Instance não encontrado"** | Confirme que o Project ID e Instance ID estão corretos. Veja-os na console do Chronicle. |
| **"Region inválida"** | A region deve ser `us`, `europe`, `asia-southeast1` ou outro valor válido para o Chronicle. Consulte a [documentação](https://cloud.google.com/chronicle/docs). |
| **"Teste passa, mas eventos não chegam"** | O log type pode estar incorreto ou desabilitado no Chronicle. Confirme em **Operação → Destinos** que o tipo está correto. Se persistir, consulte o suporte do Chronicle. |
| **google-auth não instalado** | O ambiente de backend precisa da biblioteca `google-auth`. Peça ao administrador da plataforma que instale `pip install -r requirements-sinks.txt`. |

## Próximos passos

- **Confirmar que os dados estão chegando:** abra **Operação → Destinos**, selecione o Chronicle e veja as métricas de eventos por segundo.
- **Investigar eventos recusados:** abra a fila de reenvio na visão do destino.
- **Adicionar outros destinos:** veja a [visão geral de destinos](./overview.md).
- **Decidir quais eventos vão para cada destino:** use a tela de [Roteamento](./routing.md).
