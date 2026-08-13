---
sidebar_position: 6
title: Esquema OpenAPI
description: Onde encontrar o esquema da sua instância, como explorar a API pelo navegador e o que corrigir antes de gerar um cliente a partir dele.
---

# Esquema OpenAPI

A instância publica o próprio esquema. Ele é gerado a partir do código, então descreve exatamente a versão que está rodando aí, incluindo qualquer diferença em relação a esta documentação.

| Endereço | O que é |
|----------|---------|
| `/openapi.json` | O esquema, em JSON |
| `/docs` | Interface interativa, permite disparar chamadas |
| `/redoc` | Leitura em formato de referência |

Repare que esses três **não** ficam sob `/api`:

```bash
curl https://centralops.example.com/openapi.json
```

## Quando eles não estão lá

As três rotas são ligadas quando `APP_ENV` é diferente de `production`. Em produção elas ficam desligadas por padrão, e a resposta é `404`.

O administrador da instância pode forçar o estado com `ENABLE_API_DOCS`.

:::caution[A comparação é com a palavra exata `production`]
A verificação é literal. Se a instância subiu com `APP_ENV=prod`, `APP_ENV=producao` ou qualquer outra grafia, ela **não** é considerada produção, e `/docs` fica exposto sem que ninguém tenha pedido.

Vale conferir num ambiente que você acha que é produção: abra `/docs`. Se carregar, ou o `APP_ENV` está escrito diferente, ou o `ENABLE_API_DOCS` está ligado de propósito. Esse mesmo detalhe afeta outras proteções que dependem do ambiente, então é uma boa hora para conferir a variável.
:::

## Um problema para gerar cliente

O esquema **não declara o método de autenticação**. O bloco `securitySchemes` vem vazio, e nenhuma operação declara `security`.

Três consequências práticas:

1. Em `/docs`, não existe botão de autorizar. Você consegue navegar e ler os esquemas, mas testar um endpoint protegido pela interface não funciona sem gambiarra.
2. Um cliente gerado por ferramenta (`openapi-generator`, `swagger-codegen` e afins) sai **sem nenhuma camada de autenticação**. Você vai precisar acrescentar o header `Authorization` à mão no cliente gerado.
3. Uma auditoria automatizada que leia o esquema vai reportar algo como "217 endpoints sem autenticação". É um falso positivo, e caro de refutar, porque a evidência dela é o documento oficial da própria API.

O campo `info.version` também está em `0.1.0`, que não acompanha a versão do produto. Não use esse número para decidir compatibilidade.

Enquanto isso não for corrigido no servidor, um cliente gerado precisa de dois ajustes manuais: injetar `Authorization: Bearer` em todas as chamadas, e tratar `401`, `403` e `429`, que o esquema também não descreve.

## Gerando um cliente

```bash
curl -s https://centralops.example.com/openapi.json > centralops-openapi.json

npx @openapitools/openapi-generator-cli generate \
  -i centralops-openapi.json \
  -g python \
  -o ./cliente-centralops
```

Depois de gerar, configure o header em todas as chamadas. Em Python, isso costuma ser um cabeçalho padrão na configuração do cliente:

```python
config.api_key["Authorization"] = f"Bearer {os.environ['CENTRALOPS_TOKEN']}"
```

Trate também o `Retry-After` do `429`. O gerador não cria essa lógica sozinho.

## Comparando com a sua instância

Se algo nesta documentação não bater com o que você vê, a instância manda. Para achar a diferença rápido:

```bash
# Existe mesmo esse endpoint na minha versão?
curl -s https://centralops.example.com/openapi.json \
  | jq '.paths | keys[]' | grep quarantine

# Quais campos esse corpo aceita?
curl -s https://centralops.example.com/openapi.json \
  | jq '.components.schemas.ApiTokenCreate'
```

Se você encontrar uma divergência, ela é um defeito de documentação. Abra uma issue com o trecho do esquema, porque documentação errada custa mais caro que documentação faltando.
