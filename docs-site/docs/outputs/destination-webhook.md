---
sidebar_position: 17
title: "Destino: Webhook Genérico"
description: Envie eventos para qualquer endpoint HTTP — integre com SOAR, automação e serviços ad-hoc.
---

# Destino: Webhook Genérico

O destino **Webhook Genérico** encaminha seus eventos normalizados para qualquer endpoint HTTP. Use-o para alimentar plataformas de automação de segurança (SOAR), sistemas de reação customizados ou qualquer serviço que exponha um endpoint POST/PUT HTTP — sem precisar de um plugin dedicado para cada vendor.

Esta tela só aparece para administradores da plataforma.

## Quando usar

- **Integrar com SOAR (orquestração).** Enviar eventos do CentralOps para ferramentas como Tines, Rapid7 InsightConnect ou Cortex XSOAR, que acionam fluxos de resposta automática.
- **Webhook de notificação customizado.** Alimentar um microsserviço seu de notificação, escalação ou enriquecimento que não tem connector nativo no CentralOps.
- **Integração temporária com novos fornecedores.** Enquanto aguarda um adapter oficial, use o webhook genérico para testar integrações prototipadas.
- **Envio a um proxy ou intermediário.** Se o destino real está atrás de um API gateway ou firewall, o webhook pode apuntar para o intermediário.

## O que você precisa antes de começar

- **URL do endpoint HTTP.** A URL completa, incluindo `https://` e a porta se não for 443 (por exemplo, `https://soar.exemplo.com/api/events`).
- **Método HTTP** (padrão: POST) — POST para a maioria dos casos; mude para PUT se o endpoint exigir.
- **Autenticação** (se necessária):
  - **Sem autenticação** — nenhuma credencial (token, user:pass).
  - **Bearer token** — para OAuth2 ou token-based (ex.: `Authorization: Bearer <token>`).
  - **Basic Auth** — para user:password (HTTP Basic).
- **A credencial secreta** (token ou user:pass), se aplicável.

## Criar o destino

1. No menu lateral, abra **Operação → Destinos**.
2. Use a opção de criar um novo destino.
3. Escolha o tipo **Webhook Genérico**.
4. Preencha os campos abaixo.

| Campo | O que informar |
|-------|----------------|
| **Nome** | Um nome claro (ex.: "SOAR Tines", "Webhook de notificação"). |
| **URL** | O endpoint completo do webhook (ex.: `https://soar.exemplo.com/api/events`). |
| **Método** | POST (padrão) ou PUT. |
| **Modo de autenticação** | `none` (sem auth), `bearer` (token), ou `basic` (user:pass). |
| **Credencial** | Se escolheu bearer ou basic, guarde o token ou user:pass aqui (criptografado). |
| **Formato do lote** | `array` (padrão): `[{evento1}, {evento2}]` ou `ndjson`: uma linha por evento. |
| **Corpo enviado** | `envelope` (padrão): evento completo com metadados, ou `normalized`: só o OCSF normalizado. |
| **Headers extras** | Cabeçalhos HTTP adicionais em formato JSON (ex.: `{"X-Api-Key": "valor"}`). |
| **Verificar TLS** | Mantenha ativado para garantir uma conexão segura. |

### Autenticação

**Sem autenticação:** deixe o modo como `none` e pule o campo de credencial.

**Bearer token:** escolha `bearer`, copie o token para a credencial e o CentralOps o enviará como `Authorization: Bearer <token>`.

**Basic Auth:** escolha `basic`, informar em formato `usuario:senha` na credencial. O CentralOps faz a codificação automática.

### Testar e salvar

Antes de salvar, use o botão **Testar conexão**. O CentralOps envia um lote vazio para verificar:

- se consegue alcançar o endpoint;
- se a autenticação é aceita;
- se o formato é válido.

Se o teste passar, salve. O destino fica **ativo** (badge verde).

## Como os eventos são entregues

- **Envio em lotes.** Os eventos são agrupados para eficiência.
- **Nova tentativa automática.** Falhas transitórias (timeouts, 5xx, 429) disparam reenvio automático.
- **Entrega ao menos uma vez.** Em caso de queda, um evento pode chegar duplicado. Use o ID único em `_centralops.event_id` para deduplicar.
- **Proteção contra destino instável.** Se o endpoint falhar persistentemente, o CentralOps pausa o envio e retoma automaticamente.

## Acompanhar a saúde do destino

Abra **Operação → Destinos** e selecione o seu webhook.

O badge de saúde mostra:

| Cor | Significado |
|-----|-------------|
| Verde | Eventos sendo entregues normalmente. |
| Amarelo | Eventos chegando, mas há itens na fila de reenvio. |
| Vermelho | Envio pausado ou destino indisponível. |
| Cinza | Destino desativado. |

Na visão do destino você acompanha:

- **Eventos por segundo** — taxa de entrega na última hora.
- **Latência média** — tempo de resposta do endpoint.
- **Itens na fila de reenvio (24h)** — eventos recusados.

Para ver o que não foi entregue, abra a **fila de reenvio**. Cada item mostra o motivo (ex.: "HTTP 401", "erro de conexão") e o conteúdo exato, útil para debugar.

## Resolver problemas comuns

| Sintoma | O que verificar |
|---------|-----------------|
| **"Não conecta ao endpoint"** | A URL está correta e completa? O endpoint está no ar? Se há firewall, peça à equipe de infraestrutura que libere o acesso. O CentralOps tenta reenviar automaticamente problemas transitórios. |
| **"401 Unauthorized" / "403 Forbidden"** | Confirme o modo de autenticação (none, bearer, basic) e o valor da credencial. Bearer ou basic estão corretos? Teste o endpoint fora do CentralOps com a mesma credencial para confirmar. |
| **"400 Bad Request" / "413 Payload Too Large"** | O endpoint recusou o formato. Mude entre `array` e `ndjson` ou entre `envelope` e `normalized` e tente novamente. Abra a fila de reenvio para ver o payload rejeitado. |
| **Eventos recusados por erro de formato** | Consulte a fila de reenvio para ver o conteúdo exato e o motivo. Pode ser um campo que o endpoint não espera — mude o corpo para `normalized` (só OCSF) em vez de `envelope` (completo). |
| **Muitos eventos na fila de reenvio** | O destino pode estar lento ou sobrecarregado. O CentralOps para temporariamente e retoma sozinho. Se persistir, verifique a latência média e a capacidade do endpoint. |

## Próximos passos

- **Confirmar que os dados estão chegando:** abra **Operação → Destinos**, selecione o webhook e veja as métricas de eventos por segundo.
- **Investigar eventos recusados:** abra a fila de reenvio na visão do destino.
- **Adicionar outros destinos:** veja a [visão geral de destinos](./overview.md).
- **Decidir quais eventos vão para cada destino:** use a tela de [Roteamento](./routing.md).
