---
sidebar_position: 4
title: Servidor MCP
description: Conecte um assistente de IA (Claude, Cursor, etc.) ao CentralOps pelo protocolo MCP, com a chave de API do próprio analista e as permissões dele.
---

# Servidor MCP

O CentralOps expõe suas ferramentas de operação pelo [Model Context Protocol](https://modelcontextprotocol.io) em um único endpoint, dentro da própria API:

```
POST https://centralops.example.com/api/mcp
Authorization: Bearer copsk_SUA_CHAVE
```

Não há processo separado, container nem porta extra: é o mesmo servidor, o mesmo certificado e a mesma trilha de auditoria da API REST.

## Configurar o cliente

Crie uma chave em **Conta › Tokens de API** (ela aparece uma única vez) e use a forma remota do seu cliente:

```json
{
  "mcpServers": {
    "centralops": {
      "url": "https://centralops.example.com/api/mcp",
      "headers": { "Authorization": "Bearer copsk_SUA_CHAVE" }
    }
  }
}
```

No Claude Code:

```bash
claude mcp add --transport http centralops https://centralops.example.com/api/mcp --header "Authorization: Bearer copsk_SUA_CHAVE"
```

A página **Conta** de cada usuário mostra esse snippet já com a URL da sua instância e diz se o servidor está ligado e se o seu papel tem a permissão necessária.

## Quem pode usar

Três coisas precisam estar verdadeiras, nesta ordem:

1. **O servidor está ligado.** Vem desligado. Um admin de plataforma liga em **Configurações › Assistentes (MCP)**. Desligado, `/api/mcp` responde `404` para todo mundo.
2. **A chave é de um analista com `mcp.use`.** Operator, Engineer e Admin têm por padrão; Viewer não. Um token emitido com scopes restritos precisa incluir `mcp.use`.
3. **A chave é enviada como `Authorization: Bearer`.** Cookie de sessão do console não é aceito. Isso é de propósito: um endpoint que aceitasse cookie viraria alvo de CSRF por qualquer página que o navegador visitasse.

Para tirar o acesso de alguém, revogue a chave dele ou desative o usuário. Não existe um cadastro separado de "usuários do MCP".

## O que o assistente pode fazer

Exatamente o que o analista pode fazer pelo REST com a mesma chave — nem mais, nem menos.

Cada ferramenta é implementada como uma chamada à API REST, executada dentro do servidor em nome do analista. A permissão da rota, o escopo de organização e os tetos de página são os do REST. Um Operator com `mcp.use` que pede ao assistente para publicar um mapping recebe o mesmo `403` que receberia com `curl`, e a ferramenta devolve esse erro ao modelo de forma estruturada (`error_kind: upstream_http_error`, `http_status: 403`).

Cinco ferramentas alteram estado e são anunciadas ao cliente como tal (`readOnlyHint: false`): `commit_mapping`, `commit_mapping_patch`, `request_backfill`, `cancel_backfill_job` e `reprocess_quarantine`. As duas de commit exigem um `ack_token` emitido por um dry-run do mesmo analista, válido por 5 minutos e de uso único. Todas as outras só leem — inclusive `dry_run_mapping`, que é um `POST` mas não persiste nada.

## Auditoria

Toda chamada de ferramenta gera uma linha `mcp.tool_call` no log de auditoria com o **usuário do analista**, a ferramenta, o resultado e a duração — mais as linhas normais das rotas REST que a ferramenta chamou, com o mesmo usuário e um `User-Agent` que identifica o tráfego como MCP (`centralops-mcp-embedded/...`). Tentativas recusadas (servidor desligado, chave inválida, sem `mcp.use`) geram `mcp.denied`.

## Transporte

- **Streamable HTTP**, sem sessão: cada `POST` é autossuficiente e qualquer réplica da API responde. `GET` e `DELETE` devolvem `405`.
- Resposta em **`application/json`** por padrão. O admin pode trocar para **SSE** (`text/event-stream`) na mesma tela; a troca vale na requisição seguinte.
- O rate limit da chave (60 requisições por minuto) é cobrado **uma vez por chamada MCP**, mesmo quando a ferramenta faz várias chamadas REST internas.
- Corpo máximo de 2 MiB por requisição. `wait_for_backfill_job` espera no máximo 120 s por chamada — proxies reversos costumam cortar acima disso; chame de novo para continuar esperando.

## Dicas para o modelo

O servidor entrega, no `initialize`, instruções que dizem qual ferramenta responde qual pergunta, como interpretar uma lista vazia em chave de escopo global (`organization_id` é obrigatório para dados de tenant) e o fluxo incremental para editar mappings grandes sem carregar o array inteiro no contexto. O cliente as mostra ao modelo automaticamente; nada a configurar.
