---
sidebar_position: 2
title: Autenticação
description: Como criar um token do CentralOps, usá-lo no header, e o que acontece quando ele expira, é revogado ou estoura o limite de requisições.
---

# Autenticação

A API usa um token no header `Authorization`. Não há login por usuário e senha em chamada de API, não há troca de código, não há refresh token.

```bash
curl -H "Authorization: Bearer copsk_SEU_TOKEN_AQUI" \
     https://centralops.example.com/api/integrations
```

O token começa sempre com `copsk_`. Se o seu não começa, ele não é um token de gestão. Veja a comparação das duas famílias na [visão geral](overview.md#dois-tipos-de-token-e-eles-não-se-substituem).

:::danger[Escreva `Bearer` com B maiúsculo]
A comparação do scheme é literal, sem normalizar maiúsculas. `Bearer copsk_...` funciona. `bearer copsk_...` **não**: o servidor não reconhece como token, tenta autenticar por cookie, não acha, e devolve `401` dizendo que falta autenticação. A mensagem não menciona o scheme, então parece token inválido.

Isso morde de verdade porque várias bibliotecas HTTP normalizam cabeçalhos para minúsculo. O nome do header (`authorization`) pode vir em minúsculo sem problema, o que não pode é a palavra `Bearer` dentro do valor.

Para completar a armadilha, o endpoint de ingestão aceita o scheme em qualquer caixa. Os dois caminhos do mesmo produto tratam isso de forma diferente, então funcionar num não garante funcionar no outro.
:::

## Criar um token

### Pelo console

1. Abra o menu do seu usuário e vá em **Tokens de API**.
2. Clique em criar, dê um nome que diga para que serve. "zabbix-producao" ajuda mais que "token1" no dia em que você precisar revogar.
3. Escolha a expiração.
4. Decida se o token herda suas permissões ou fica restrito a scopes específicos. Leia [Permissões e escopo](permissions.md) antes de escolher, porque o padrão é herdar tudo.
5. Copie o token.

### Pela API

Criar um token pessoal não exige permissão nenhuma além de estar autenticado. Qualquer usuário, inclusive um `viewer`, emite tokens para si mesmo sem aprovação. Emitir token de **conta de serviço** é diferente: exige `user.manage`.

```bash
curl -X POST https://centralops.example.com/api/v1/tokens \
  -H "Authorization: Bearer copsk_SEU_TOKEN_ATUAL" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "zabbix-producao",
    "expires_at": "2027-01-01T00:00:00Z",
    "scopes": ["destination.read", "route.read", "integration.read"]
  }'
```

Campos aceitos:

| Campo | Obrigatório | O que faz |
|-------|-------------|-----------|
| `name` | sim | Identifica o token na listagem e na revogação. |
| `expires_at` | não | Data e hora em que o token para de funcionar. Precisa estar no futuro. |
| `is_eternal` | não | `true` cria um token sem validade. Não pode vir junto com `expires_at`. |
| `scopes` | não | Restringe o token a um subconjunto das permissões do dono. Sem isso, o token herda tudo. |

:::caution[O campo `service_account_id` aparece no esquema mas não funciona aqui]
`ApiTokenCreate` declara `service_account_id`, então ele aparece no OpenAPI e em qualquer cliente gerado a partir dele. Mandar esse campo em `POST /api/v1/tokens` devolve `400` com `api_token.service_account_id_not_allowed`.

Para emitir token de conta de serviço, use `POST /api/v1/service-accounts/{id}/tokens`, onde o id vem do caminho. Lá, se você mandar o campo no corpo, ele é ignorado em favor do caminho.

E um token de conta de serviço não consegue criar tokens por `POST /api/v1/tokens`: a resposta é `400` apontando o endpoint certo.
:::

:::danger[Omitir a expiração não cria um token de curta duração, cria um token eterno]
Se você mandar só o `name`, sem `expires_at` e sem `is_eternal`, o token é criado **sem validade**. Esse comportamento existe para não quebrar clientes antigos que não conhecem o campo `is_eternal`, e ele é silencioso: a resposta não avisa nada.

Mande sempre `expires_at`, ou mande `is_eternal: true` de propósito, para que a escolha fique registrada.
:::

Mandar `is_eternal: true` e `expires_at` na mesma requisição é erro. Escolha um dos dois.

## A resposta aparece uma vez só

```json
{
  "token": "copsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "api_token": {
    "id": 12,
    "name": "zabbix-producao",
    "token_prefix": "copsk_xxxxx",
    "expires_at": "2027-01-01T00:00:00",
    "is_eternal": false
  }
}
```

O campo `token` é o valor completo, e ele **não pode ser recuperado depois**. O CentralOps guarda um hash Argon2id, não o token. Perdeu, revogue e crie outro.

O que aparece nas listagens é o `token_prefix`, os primeiros 12 caracteres. Serve para você identificar qual token é qual sem expor o segredo.

## Listar, revogar e conferir

```bash
# Listar os seus tokens (mostra o prefixo, nunca o valor)
curl -H "Authorization: Bearer $TOKEN" \
     https://centralops.example.com/api/v1/tokens

# Revogar. Efeito imediato, sem período de carência.
curl -X DELETE -H "Authorization: Bearer $TOKEN" \
     https://centralops.example.com/api/v1/tokens/12

# Ver a lista de scopes que existem para restringir
curl -H "Authorization: Bearer $TOKEN" \
     https://centralops.example.com/api/v1/tokens/scopes
```

Revogar devolve `204` sem corpo. A partir daí, qualquer chamada com aquele token recebe `401`.

## Contas de serviço

Um token pessoal morre junto com a conta de quem o criou. Se a pessoa sai da empresa e o usuário é desativado, toda automação que dependia daquele token para de funcionar, e o motivo não é óbvio para quem está de plantão.

Para automação que precisa sobreviver a mudança de time, use uma **conta de serviço**. Ela não é uma pessoa, não faz login no console, e existe só para carregar tokens.

```bash
# Criar a conta
curl -X POST https://centralops.example.com/api/v1/service-accounts \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "monitoramento-zabbix", "role": "viewer"}'

# Emitir um token para ela
curl -X POST https://centralops.example.com/api/v1/service-accounts/3/tokens \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "zabbix-prod", "expires_at": "2027-01-01T00:00:00Z"}'
```

Desativar a conta de serviço invalida todos os tokens dela de uma vez, e a resposta passa a ser `401` com `Service account is inactive`.

## Cookie e Bearer na mesma requisição

O console autentica por cookie de sessão. A API autentica por Bearer. Se os dois chegarem juntos, **o Bearer vence**.

Isso importa quando você testa a API no navegador logado: você acha que está testando o token, mas talvez esteja testando o seu cookie de admin. Teste do terminal, ou numa janela anônima.

O fluxo Bearer não devolve `set-cookie`. Ele é sem estado, e cada requisição carrega a credencial inteira.

## Erros de autenticação

| Status | `detail` | O que aconteceu |
|--------|----------|-----------------|
| `401` | `Invalid or expired API token` | Token errado, revogado ou vencido. |
| `401` | `User is inactive` | O token é válido, mas o usuário dono foi desativado. |
| `401` | `Service account is inactive` | O token é válido, mas a conta de serviço foi desativada. |
| `403` | varia | O token autenticou, mas falta permissão ou o recurso é de outra organização. Veja [Permissões e escopo](permissions.md). |
| `429` | `Token rate limit exceeded` | Excesso de requisições. Veja abaixo. |

Todo `401` volta com o header `WWW-Authenticate: Bearer realm="centralops"`.

:::caution[Um token `copsk_` inválido não cai para o cookie]
Se o header traz um token que começa com `copsk_` e ele não é válido, a resposta é `401` na hora. O sistema não tenta o cookie como alternativa, de propósito: cair para o cookie faria um token quebrado parecer que funciona, e você só descobriria em produção, quando não houvesse cookie nenhum.
:::

## Limite de requisições

Cada token tem quatro janelas simultâneas. Os valores padrão:

| Janela | Limite |
|--------|--------|
| por segundo | 10 |
| por minuto | 100 |
| por hora | 1.000 |
| por dia | 50.000 |

O administrador da instância pode mudar esses números na configuração.

Ao estourar qualquer uma das janelas, a resposta é `429` com o header `Retry-After` em segundos. Respeite o `Retry-After` em vez de repetir em laço apertado: a contagem é por token, e insistir só empurra a janela para frente.

Para monitoramento, uma coleta a cada 60 segundos fica muito abaixo do limite. O que costuma estourar é varredura de recurso um a um. Prefira os endpoints de lote quando existirem, e eles estão marcados na [referência](reference.md).

## Guardando o token

O token dá o mesmo acesso que a pessoa (ou a conta de serviço) que o emitiu. Trate como senha:

- Variável de ambiente ou cofre de segredos, nunca no código nem no repositório.
- Um token por consumidor. Compartilhar um token entre três sistemas significa que revogar um derruba os três.
- Expiração curta e renovação programada em vez de token eterno.
- O menor conjunto de scopes que resolve. A página [Permissões e escopo](permissions.md) mostra como.
