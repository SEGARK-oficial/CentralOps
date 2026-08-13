---
sidebar_position: 4
title: Convenções
description: Paginação, erros, datas, redirecionamento e limites. O que vale para todo endpoint, incluindo o que não é uniforme.
---

# Convenções

Esta página junta o que se repete em toda a API. Vale a leitura antes de escrever o primeiro cliente, porque três dos itens abaixo falham em silêncio: eles devolvem `200` e um resultado errado, em vez de erro.

## Camadas de proteção

Nem todo endpoint é protegido do mesmo jeito. São três níveis:

| Nível | Onde vale | O que exige |
|-------|-----------|-------------|
| Público | `/livez`, `/readyz`, login, SSO | Nada |
| Autenticado | A maioria dos endpoints | Um token válido, sem permissão específica |
| Com permissão | Escritas e leituras sensíveis | Token válido **e** a permissão listada na [referência](reference.md) |

O nível intermediário explica por que muitos endpoints de leitura funcionam com qualquer papel: eles pedem sessão, não permissão. O recorte por organização continua valendo em todos.

## Sondas de saúde

```bash
curl https://centralops.example.com/livez    # o processo está de pé
curl https://centralops.example.com/readyz   # está pronto para receber tráfego
```

As duas são **públicas, sem token e sem o prefixo `/api`**. São as certas para healthcheck de balanceador e de container.

Não confunda com `/api/integrations/pipeline-health`, que é outra coisa: essa responde se o **pipeline de dados** está saudável, exige token, e é a que interessa para monitoramento de operação.

## Formato de erro

O corpo de erro segue o padrão do FastAPI:

```json
{ "detail": "Invalid or expired API token" }
```

Alguns endpoints usam erro com código estável, mais fácil de tratar em automação:

```json
{ "detail": { "code": "enrichment.source_sharing_requires_enterprise", "message": "..." } }
```

Prefira casar pelo `code` quando ele existir. As mensagens de texto podem mudar de redação e são traduzidas; os códigos não.

### Status que você vai encontrar

| Status | Significado aqui |
|--------|------------------|
| `200` | Deu certo. Cuidado: pode ter vindo vazio por escopo, veja abaixo. |
| `201` | Recurso criado. |
| `204` | Deu certo, sem corpo. Típico de remoção. |
| `207` | Lote processado com resultado misto. Leia os contadores no corpo. |
| `400` | O pedido é inválido pelas regras de negócio. |
| `401` | Credencial ausente, inválida, expirada ou desativada. |
| `403` | Autenticou, mas falta permissão ou o recurso é de outra organização. |
| `404` | Não existe **ou** está fora do seu escopo. Veja abaixo. |
| `409` | Conflito. Por exemplo, apagar integração pai que ainda tem filhas ativas. |
| `422` | O corpo não bate com o esquema. |
| `429` | Limite de requisições estourado. Respeite o `Retry-After`. |
| `503` | Dependência fora do ar (Redis, banco, serviço externo). |

:::caution[`404` nem sempre quer dizer que não existe]
Vários endpoints devolvem `404` para recurso que existe mas pertence a outra organização, em vez de `403`. É proposital: um `403` confirmaria que o id existe, e isso permitiria varrer ids para descobrir o que outros clientes têm.

O efeito colateral aparece quando um token perde escopo. Ele passa a receber `404` onde antes recebia `200`, sem nenhum `403` no meio. **Se o seu monitor só alerta em `403` ou acima, ele não vê essa perda de acesso.** Alerte também em `404` inesperado num recurso que você sabe que existe.
:::

O `403` costuma trazer o nome da permissão que faltou, o que ajuda muito a depurar. A exceção é `internal.tenant.read`, que omite o nome de propósito.

## Correlacionar uma chamada com o log

Toda resposta traz `X-Correlation-Id`. Se você mandar esse header na requisição, o valor é reaproveitado; se não mandar, o servidor gera um.

```bash
curl -i -H "Authorization: Bearer $TOKEN" \
     -H "X-Correlation-Id: minha-automacao-2026-08-13-001" \
     https://centralops.example.com/api/integrations
```

Registre esse valor no log da sua automação. Na hora de pedir suporte, ele liga a sua chamada à linha de log do servidor sem precisar adivinhar por horário.

## Paginação

Não é uniforme, e fingir que é levaria você a escrever um cliente que quebra na metade dos endpoints.

**Padrão mais comum: `limit` e `offset`.** Cerca de 16 endpoints. A resposta é um objeto:

```json
{ "total": 431, "items": [ ... ], "limit": 50, "offset": 0 }
```

**Padrão minoritário: `page` e `size`.** Poucos endpoints, entre eles a listagem de organizações.

**A maioria das listagens não pagina.** Devolve um array puro, com tudo.

:::caution[Com `page` e `size` você não sabe quantas páginas existem]
Nos endpoints que usam `page` e `size`, o corpo **não traz `total`**. A única forma de saber que acabou é pedir a próxima página e receber um array vazio.

A exceção é a listagem de organizações, que devolve `X-Total-Count`, `X-Page` e `X-Size` como headers de resposta.
:::

:::caution[Paginar por `offset` durante gravação pula e repete registros]
Não existe parâmetro de ordenação. A ordem é fixa por endpoint, e em várias listagens é do mais recente para o mais antigo.

Enquanto você pagina, o pipeline continua inserindo. Cada item novo empurra os antigos para frente, então o registro que estava na posição 50 vai para a 51 e você o lê de novo na página seguinte, ou pula outro. Em quarentena e histórico, que recebem escrita o tempo todo, isso acontece de verdade.

Para exportar tudo com fidelidade, prefira filtrar por uma janela de tempo fechada no passado, em vez de varrer por offset.
:::

## Nomes de parâmetro não são uniformes

O mesmo conceito aparece com dois nomes conforme o endpoint:

- Organização: `organization_id` em uns, `org_id` em outros.
- Em um mesmo recurso, o nome pode mudar entre o parâmetro de query e o campo de corpo.

Não deduza. Consulte a [referência](reference.md) ou o esquema OpenAPI para cada endpoint. Um parâmetro com nome errado costuma ser **ignorado em silêncio**, e você recebe `200` com o resultado não filtrado.

## Datas

O formato de entrada é ISO 8601. Em UTC:

```
2026-08-13T14:22:05Z
```

Na saída existem duas formas na mesma API. Campos gerados pelo pipeline saem com `Z` e precisão de segundos. Campos vindos direto do banco saem sem fuso e com microssegundos. Ao comparar datas de origens diferentes, normalize antes.

:::danger[Data inválida não dá erro, o filtro some]
Se você mandar uma data que o servidor não consegue interpretar (`13/08/2026`, `2026-13-45`, `ontem`), a resposta **não** é `422`. O filtro simplesmente deixa de ser aplicado, e você recebe `200` com o conjunto inteiro, achando que filtrou.

Além disso, o deslocamento de fuso é descartado: `2026-08-13T00:00:00-03:00` é tratado como meia-noite **UTC**, não como três da manhã UTC.

Mande sempre ISO 8601 em UTC, e confira a contagem de resultados quando aplicar um filtro de data pela primeira vez.
:::

## Barra no final da URL

Alguns caminhos são declarados com barra final. Chamar sem a barra devolve `307` para a versão com barra.

Isso quebra clientes que não seguem redirecionamento, e quebra pior os que seguem mas **descartam o header `Authorization`** no caminho (vários clientes descartam por segurança ao redirecionar). O sintoma é um `401` ou `405` sem explicação, só naqueles endpoints.

Use a URL exata da referência, e configure o seu cliente para preservar os headers ao seguir redirecionamento.

## Chamando de um navegador

O CORS está configurado sem `expose_headers`. Na prática, JavaScript rodando em **outra origem** não consegue ler nenhum header customizado da resposta: `X-Total-Count`, `X-Page`, `X-Size`, `X-Correlation-Id` e até `Retry-After` voltam como `null` em `response.headers.get()`.

Isso não aparece hoje no console porque ele é servido pelo mesmo processo, ou seja, mesma origem. Se você for construir uma página em outro domínio, conte apenas com o corpo da resposta.

## Corpo e tipo de conteúdo

Mande `Content-Type: application/json` em toda requisição com corpo.

Fogem do JSON:

- Exportações em CSV (histórico de auditoria, resultados de busca).
- Exportações em NDJSON (captura ao vivo).
- Ingestão por push, que aceita JSON e NDJSON.

:::caution[Na ingestão, o formato é adivinhado pelo conteúdo, não pelo `Content-Type`]
O endpoint de ingestão decide se o corpo é NDJSON olhando se há quebras de linha e se o texto começa com `[`. Um JSON único e formatado com indentação (que começa com `{` e tem quebras de linha) é tratado como NDJSON, cada linha vira erro de parse, e o lote inteiro se perde com resposta de sucesso.

Mande JSON compacto, em uma linha, ou NDJSON de verdade.
:::

## Limite de requisições

Quatro janelas por token, com os padrões: 10 por segundo, 100 por minuto, 1.000 por hora, 50.000 por dia. Estourar qualquer uma devolve `429` com `Retry-After` em segundos.

Além do limite, vale saber o custo de cada chamada: **cada requisição autenticada por token verifica um hash Argon2id**, que é deliberadamente caro (algo como 50 ms e dezenas de megabytes de memória), e não há cache dessa verificação. Cada chamada também grava a data do último uso do token no banco.

A consequência prática: polling agressivo consome CPU e memória do servidor mesmo quando a resposta é pequena. Para monitoramento, um intervalo de 60 segundos é folgado e barato. Prefira os endpoints de lote, marcados na referência, a varrer recurso por recurso.

## Escritas que doem

Alguns endpoints têm efeito bem maior do que o nome sugere. Eles estão marcados na [referência](reference.md), mas vale a lista curta:

- **Disparar coleta ou backfill** faz chamada real à API do fornecedor e consome a quota dele.
- **Zerar o cursor de um coletor** provoca recoleta e duplicidade temporária, até o prazo de deduplicação passar.
- **Descartar quarentena** apaga de verdade, sem lixeira.
- **Revogar credencial de destino** também desabilita o destino, ou seja, para a entrega até você configurar a nova chave.
- **Criar destino habilitado** cria junto uma rota que já começa a receber evento no ciclo seguinte.
- **Apagar dados de uma organização** é irreversível e exige um texto de confirmação exato no corpo.
