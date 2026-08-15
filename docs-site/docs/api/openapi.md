---
sidebar_position: 6
title: Esquema OpenAPI
description: Onde encontrar o esquema da sua instância, por que o endereço público não o serve, e o que ajustar antes de gerar um cliente.
---

# Esquema OpenAPI

A aplicação gera o próprio esquema a partir do código, então ele descreve exatamente a versão que está rodando aí, incluindo qualquer diferença em relação a esta documentação.

| Caminho | O que é |
|---------|---------|
| `/openapi.json` | O esquema, em JSON |
| `/docs` | Interface interativa, permite disparar chamadas |
| `/redoc` | Leitura em formato de referência |

Repare que esses três não ficam sob `/api`.

## Pelo endereço público eles não respondem

:::danger[O nginx bloqueia os três na borda, com 404 fixo]
A stack que acompanha o projeto publica o site por um nginx, e ele tem regras que devolvem `404` para `/docs`, `/docs/`, `/redoc`, `/redoc/` e `/openapi.json`. O bloqueio é incondicional: não olha `APP_ENV`, não olha configuração nenhuma.

Ou seja, `curl https://centralops.example.com/openapi.json` devolve `404` mesmo numa instância de desenvolvimento com tudo ligado. O esquema existe, mas quem responde por ele é o processo da API, e o nginx não deixa chegar lá.
:::

Para pegar o esquema, bata direto no processo da API, que escuta na porta 8000 dentro da rede da stack:

```bash
docker compose exec centralops \
  curl -s http://127.0.0.1:8000/openapi.json > centralops-openapi.json
```

Se você quiser abrir a interface interativa para o time, a mudança é no nginx: troque o `return 404` por uma restrição de origem, no arquivo de configuração do serviço de frontend.

```nginx
location = /docs {
    allow 10.0.0.0/8;   # sua rede interna
    deny all;
    proxy_pass http://centralops:8000;
}
```

## Quando a API desliga os três por conta própria

Além do bloqueio no nginx, a própria aplicação decide se publica ou não. Ela publica quando `APP_ENV` é diferente de `production`.

O valor passa por uma normalização antes da comparação, com remoção de espaços e conversão para minúsculas. Então `Production`, `PRODUCTION` e ` production ` contam como produção normalmente, e desligam os três caminhos.

:::caution[O que escapa do gate é palavra diferente, não caixa diferente]
`prod`, `producao`, `prd` e afins **não** são reconhecidos como produção. Uma instância que subiu com um desses tem o Swagger ativo dentro do contêiner, e o mesmo detalhe afeta outras proteções que dependem do ambiente.

Como o nginx bloqueia a borda, abrir `/docs` no endereço público **não serve como teste**: ele devolve `404` de qualquer jeito, e "não carregou" não prova nada. Para conferir de verdade, pergunte ao processo:

```bash
docker compose exec centralops \
  curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/docs
```

`200` significa Swagger ativo dentro do contêiner. Se você esperava produção, confira a grafia de `APP_ENV`.
:::

Existe uma variável `ENABLE_API_DOCS` no código, que forçaria o estado nos dois sentidos. Vale saber que **nenhum dos deployments que acompanham o projeto a repassa**: o compose lista as variáveis uma a uma, sem `env_file`, e o chart também não a inclui. Colocá-la no ambiente do host não tem efeito sozinho, seria preciso acrescentá-la ao deployment antes.

## Um problema para gerar cliente

O esquema **não declara autenticação em lugar nenhum**. O bloco `components` traz apenas `schemas`: a chave `securitySchemes` não existe (um `jq '.components.securitySchemes'` devolve `null`, não um objeto vazio), e nenhuma das 215 operações declara `security`.

Três consequências práticas:

1. Em `/docs`, não existe botão de autorizar. Você navega e lê os esquemas, mas testar um endpoint protegido pela interface não funciona.
2. Um cliente gerado por ferramenta sai **sem nenhuma camada de autenticação**, e o jeito usual de configurar credencial no cliente gerado não funciona (veja abaixo).
3. Uma auditoria automatizada que leia o esquema vai reportar todas as operações como sem autenticação. É falso positivo, e caro de refutar, porque a evidência dela é o documento oficial da própria API.

O campo `info.version` também está em `0.1.0`, que não acompanha a versão do produto. Não use esse número para decidir compatibilidade.

## Gerando um cliente

```bash
npx @openapitools/openapi-generator-cli generate \
  -i centralops-openapi.json \
  -g python \
  -o ./cliente-centralops
```

:::caution[Configurar `api_key` no cliente gerado não funciona aqui]
No cliente Python do gerador, o dicionário `api_key` da configuração só é consultado através das definições de segurança que vieram do esquema. Como o esquema não declara nenhuma, esse dicionário é ignorado e nenhum header é enviado.

Injete o header direto no cliente:

```python
import os
import openapi_client

config = openapi_client.Configuration(host="https://centralops.example.com")
client = openapi_client.ApiClient(config)
client.set_default_header("Authorization", f"Bearer {os.environ['CENTRALOPS_TOKEN']}")
```

O equivalente em outras linguagens é o mecanismo de header padrão do cliente gerado, não a configuração de credencial.
:::

Trate também o `Retry-After` do `429`, que o esquema não descreve. O gerador não cria essa lógica sozinho.

## Comparando com a sua instância

Se algo nesta documentação não bater com o que você vê, a instância manda. Para achar a diferença rápido, com o esquema já baixado:

```bash
# Existe mesmo esse endpoint na minha versão?
jq '.paths | keys[]' centralops-openapi.json | grep quarantine

# Quais campos esse corpo aceita?
jq '.components.schemas.ApiTokenCreate' centralops-openapi.json
```

Se você encontrar uma divergência, ela é um defeito de documentação. Abra uma issue com o trecho do esquema, porque documentação errada custa mais caro que documentação faltando.
