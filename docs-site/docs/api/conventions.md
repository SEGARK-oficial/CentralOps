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

A aplicação expõe `/livez` (o processo está de pé) e `/readyz` (está pronto para receber tráfego). As duas são públicas, sem token e sem o prefixo `/api`.

:::danger[No endereço público elas não existem, e `/health` engana]
O nginx da stack não tem rota para `/livez` nem `/readyz`. As duas caem no roteamento do site e devolvem a página do console, com `200`. Um monitor que só olhe o código de status vai considerar tudo saudável para sempre, inclusive com a API morta.

Pior: `/health` **existe** no nginx e devolve a palavra `healthy` sem consultar coisa alguma. Ele prova que o nginx subiu, e só isso.

Para sondar de verdade, bata no processo da API dentro da rede da stack:

```bash
docker compose exec centralops curl -sf http://127.0.0.1:8000/readyz
```

É esse o alvo certo no healthcheck do contêiner e no balanceador interno.
:::

Para monitoramento de operação, a pergunta é outra e a resposta está em `/api/integrations/pipeline-health`: ela diz se o **pipeline de dados** está saudável, exige token, e responde normalmente pelo endereço público.

## Formato de erro

O corpo de erro segue o padrão do FastAPI:

```json
{ "detail": "Invalid or expired API token" }
```

Alguns endpoints acrescentam um envelope com código estável, ao lado do `detail`:

```json
{
  "error": {
    "code": "enrichment.source_sharing_requires_enterprise",
    "message": "Compartilhar fonte entre organizações exige Enterprise.",
    "details": {}
  },
  "detail": "Compartilhar fonte entre organizações exige Enterprise."
}
```

O `code` fica em `error.code`, na raiz, **nunca dentro de `detail`**. O campo `detail` é sempre uma string, mantido para compatibilidade com o formato padrão do FastAPI.

Prefira casar por `error.code` quando ele existir: as mensagens mudam de redação e são traduzidas, os códigos não.

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
| `429` | Passou de 60 requisições no minuto, naquele token. Respeite o `Retry-After`. |
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
     https://centralops.example.com/api/integrations/
```

Registre esse valor no log da sua automação. Na hora de pedir suporte, ele liga a sua chamada à linha de log do servidor sem precisar adivinhar por horário.

## Paginação

Não é uniforme, e fingir que é levaria você a escrever um cliente que quebra na metade dos endpoints.

**16 endpoints aceitam `limit`.** Desses, só 8 aceitam `offset` também. Nos outros 8 você consegue limitar o tamanho, e não consegue avançar: não há segunda página.

Entre os que têm `limit` e `offset`, metade responde com este envelope:

```json
{ "total": 431, "items": [ ... ], "limit": 50, "offset": 0 }
```

A outra metade responde de outro jeito, às vezes com array puro. Confira o esquema do endpoint antes de assumir que existe `total`.

**3 endpoints usam `page` e `size`.**

**As demais listagens não paginam.** Devolvem um array puro.

:::caution[Array puro nem sempre significa "tudo"]
Vários endpoints que devolvem array aplicam um `limit` padrão silencioso, por exemplo 100. Você recebe uma lista aparentemente completa, sem `total` e sem indicação de que foi cortada.

Se a contagem bater exatamente num número redondo como 100, desconfie: peça um `limit` maior e compare.

:::caution[Com `page` e `size` você não sabe quantas páginas existem]
Dos 3 endpoints com `page` e `size`, um devolve `total`, `page` e `size` no corpo (a lista de tenants de uma integração) e outro devolve `X-Total-Count`, `X-Page` e `X-Size` como headers (a lista de organizações). No restante, a única forma de saber que acabou é pedir a próxima página e receber vazio.
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
Em alguns endpoints, entre eles os de auditoria, uma data que o servidor não consegue interpretar (`13/08/2026`, `2026-13-45`, `ontem`) **não** gera `422`. O filtro simplesmente deixa de ser aplicado, e você recebe `200` com o conjunto inteiro, achando que filtrou.

Outros endpoints tipam a data no esquema e devolvem `422` normalmente. Como o comportamento varia, não dá para confiar que uma data ruim sempre acusa.

Além disso, o deslocamento de fuso é descartado: `2026-08-13T00:00:00-03:00` é tratado como meia-noite **UTC**, não como três da manhã UTC.

Mande sempre ISO 8601 em UTC, e confira a contagem de resultados quando aplicar um filtro de data pela primeira vez.
:::

## Barra no final da URL

Alguns caminhos são declarados com barra final. Chamar sem a barra devolve `307` para a versão com barra.

Isso quebra clientes que não seguem redirecionamento, e quebra pior os que seguem mas **descartam o header `Authorization`** no caminho (vários clientes descartam por segurança ao redirecionar). O sintoma é um `401` ou `405` sem explicação, só naqueles endpoints.

Use a URL exata da referência, e configure o seu cliente para preservar os headers ao seguir redirecionamento.

## Chamando de um navegador

Antes dos headers, a lista de origens permitidas: por padrão ela traz apenas endereços de desenvolvimento local. Uma página hospedada em outro domínio é barrada no CORS antes de qualquer outra coisa, e o administrador precisa acrescentar a origem na configuração.

Passada essa barreira, vem a segunda: o CORS está configurado sem `expose_headers`. JavaScript em **outra origem** não consegue ler nenhum header customizado da resposta. `X-Total-Count`, `X-Page`, `X-Size`, `X-Correlation-Id` e até `Retry-After` voltam como `null` em `response.headers.get()`.

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

**60 requisições por minuto, por token.** Uma janela deslizante única, fixa no código, sem variável de ambiente que a ajuste. Estourar devolve `429` com `Retry-After` em segundos.

Além do limite, vale saber o custo de cada chamada: **cada requisição autenticada por token verifica um hash Argon2id**, que é deliberadamente caro (algo como 50 ms e dezenas de megabytes de memória), e não há cache dessa verificação. Cada chamada também grava a data do último uso do token no banco.

A consequência prática: polling agressivo consome CPU e memória do servidor mesmo quando a resposta é pequena. Para monitoramento, um intervalo de 60 segundos é folgado e barato.

Prefira endpoints que devolvem tudo de uma vez a varrer recurso por recurso. Os principais são `GET /api/collectors/destinations/health` (todos os destinos), `GET /api/integrations/pipeline-health` (todas as integrações) e `GET /api/collectors/state` (todos os coletores). Trinta destinos em uma chamada em vez de trinta chamadas é a diferença entre caber e estourar o limite.

## Escritas que doem

Alguns endpoints têm efeito bem maior do que o nome sugere. Eles estão marcados na [referência](reference.md), mas vale a lista curta:

- **Disparar coleta ou backfill** faz chamada real à API do fornecedor e consome a quota dele.
- **Zerar o cursor de um coletor** provoca recoleta e duplicidade temporária, até o prazo de deduplicação passar.
- **Descartar quarentena** apaga de verdade, sem lixeira.
- **Revogar credencial de destino** também desabilita o destino, ou seja, para a entrega até você configurar a nova chave.
- **Criar destino habilitado** cria junto uma rota que já começa a receber evento no ciclo seguinte.
- **Apagar dados de uma organização** é irreversível e exige um texto de confirmação exato no corpo.
