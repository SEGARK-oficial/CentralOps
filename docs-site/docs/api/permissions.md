---
sidebar_position: 3
title: Permissões e escopo
description: O que o seu token pode fazer, como restringir por scope, e por que uma resposta vazia nem sempre significa que não há dados.
---

# Permissões e escopo

O que um token pode fazer sai de três coisas, aplicadas nesta ordem:

1. **O papel** de quem emitiu o token. Define o teto.
2. **Os scopes** do token. Recortam abaixo do teto, nunca acima.
3. **O escopo de organização** do dono. Define de quais tenants ele enxerga dados.

Errar qualquer uma das três produz um erro diferente, e a diferença importa na hora de depurar. Papel e scope insuficientes dão `403`. Escopo de organização errado costuma dar `403` também, mas em algumas leituras dá **`200` com lista vazia**, que engana bem mais.

## Os quatro papéis

Não é possível criar papel personalizado. São estes quatro, fixos no código. A granularidade fina vem dos scopes do token, explicados adiante.

| Papel | Ideia | Permissões |
|-------|-------|------------|
| `viewer` | Só olha | 7 |
| `operator` | Opera o dia a dia, não muda regra | 13 |
| `engineer` | Mexe em normalização e detecção | 19 |
| `admin` | Tudo, incluindo usuários e credenciais | 23 |

## A matriz completa

Esta tabela é gerada a partir do código, não escrita à mão.

| Permissão | Viewer | Operator | Engineer | Admin | O que libera |
|---|:--:|:--:|:--:|:--:|---|
| `integration.read` | sim | sim | sim | sim | Ver integrações e o estado da coleta |
| `integration.pause` | nao | sim | sim | sim | Pausar e retomar uma coleta |
| `integration.reset` | nao | sim | sim | sim | Zerar o cursor de coleta |
| `integration.write` | nao | nao | nao | sim | Criar, editar e apagar integração, incluindo credencial |
| `mapping.read` | sim | sim | sim | sim | Ver mappings e versões |
| `mapping.write` | nao | nao | sim | sim | Editar mapping e publicar versão |
| `mapping.rollback` | nao | nao | sim | sim | Voltar para uma versão anterior |
| `quarantine.read` | sim | sim | sim | sim | Ver eventos que a normalização recusou |
| `quarantine.discard` | nao | sim | sim | sim | Descartar e reprocessar quarentena |
| `drift.read` | sim | sim | sim | sim | Ver campos novos detectados |
| `drift.ignore` | nao | sim | sim | sim | Marcar campo como ignorado |
| `drift.mark_mapped` | nao | nao | sim | sim | Marcar campo como já mapeado |
| `drift.delete` | nao | nao | sim | sim | Apagar entrada de campo detectado |
| `destination.read` | sim | sim | sim | sim | Ver destinos, saúde e contagem de DLQ |
| `route.read` | sim | sim | sim | sim | Ver regras de roteamento |
| `audit.read` | sim | sim | sim | sim | Ler o histórico de mudanças |
| `query.run` | nao | sim | sim | sim | Rodar consulta ao vivo na fonte do cliente |
| `query.save` | nao | nao | sim | sim | Salvar consulta e agendamento |
| `correlation.preview` | nao | nao | sim | sim | Testar regra de correlação contra amostras reais |
| `internal.tenant.read` | nao | sim | sim | sim | Resolução de tenant entre serviços |
| `user.manage` | nao | nao | nao | sim | Criar, editar e remover usuários |
| `org.manage` | nao | nao | nao | sim | Criar e editar organizações |
| `secret.read` | nao | nao | nao | sim | Ler referência de credencial guardada no cofre |

:::caution[Três permissões custam dinheiro ou tocam dado de cliente]
`query.run` roda consulta na fonte do fornecedor, o que pode gerar custo e carga no ambiente do cliente. `correlation.preview` lê amostras reais de evento. `secret.read` alcança referência de credencial.

Não coloque nenhuma das três num token de automação que não precisa delas, mesmo que o papel do dono as tenha.
:::

Escrever destino e escrever rota continuam exigindo `user.manage`, sem permissão própria. Só a **leitura** dessas duas áreas foi separada em `destination.read` e `route.read`, justamente para que um monitor não precise de um token capaz de administrar contas.

## Restringir o token por scope

Por padrão, um token herda **todas** as permissões do papel do dono. Se um admin cria um token sem pensar, esse token pode apagar usuário.

Para restringir, mande `scopes` na criação:

```bash
curl -X POST https://centralops.example.com/api/v1/tokens \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "monitor-somente-leitura",
    "expires_at": "2027-01-01T00:00:00Z",
    "scopes": ["destination.read", "route.read", "integration.read"]
  }'
```

A permissão efetiva é a **interseção** entre os scopes do token e as permissões do papel do dono:

```
efetiva = scopes do token ∩ permissões do papel
```

Duas consequências práticas:

- Pedir um scope que o papel não tem **não concede nada**. Um viewer que emite token com `scopes: ["user.manage"]` continua sem `user.manage`. O token não escala privilégio.
- Se o dono for rebaixado depois, o token encolhe junto, na hora. Não existe permissão congelada no momento da emissão.

Para ver a lista de scopes válidos:

```bash
curl -H "Authorization: Bearer $TOKEN" \
     https://centralops.example.com/api/v1/tokens/scopes
```

:::danger[Lista de scopes vazia não é "nenhuma permissão", é "todas"]
O backend trata `scopes: []` igual a não ter mandado o campo. Nos dois casos o token sai com o papel inteiro.

Se a intenção era um token sem poder nenhum, `[]` faz o oposto exato disso, em silêncio. Ou você lista pelo menos um scope, ou não use o campo e assuma que o token herda tudo.
:::

## Escopo de organização

Além de "o que pode fazer", existe "sobre quais organizações". Duas propriedades do dono decidem:

| Papel | Enxerga todas as organizações quando |
|-------|--------------------------------------|
| `admin` | `is_global = true` **ou** `organization_id` vazio |
| `viewer`, `operator`, `engineer` | somente com `is_global = true` |

Um admin com organização definida e `is_global = false` é um **admin de organização**: administra a própria organização (e as filhas dela, em hierarquia de MSP), não a plataforma.

Um viewer, operator ou engineer com `is_global = true` é o analista que monitora todos os clientes com as permissões do próprio papel, sem poder administrativo.

### A resposta vazia que parece "não tem dado"

Esta é a pegadinha que mais custa tempo, e ela não gera erro nenhum.

Quando um token de escopo global chama um endpoint de dado de tenant **sem nomear a organização**, o sistema não agrega os tenants todos. Ele resolve para a organização do próprio dono, que num usuário global é vazia, e devolve `200` com resultado vazio.

Você lê "0 resultados" e conclui que não há dado. Na verdade a pergunta é que não nomeou o cliente.

```bash
# Token global, sem dizer a organização: volta vazio
curl -H "Authorization: Bearer $TOKEN" \
     "https://centralops.example.com/api/mappings/samples?vendor=sophos"

# Mesma pergunta, nomeando o tenant: volta o dado
curl -H "Authorization: Bearer $TOKEN" \
     "https://centralops.example.com/api/mappings/samples?vendor=sophos&organization_id=7"
```

A regra prática: se você usa token global e uma leitura de dado de tenant voltou vazia, **mande `organization_id` antes de concluir qualquer coisa**.

Os nomes dos parâmetros não são uniformes na API. Alguns endpoints usam `organization_id`, outros usam `org_id`. A [referência](reference.md) traz o nome certo por endpoint.

### Cruzar organizações

Um token escopado a uma organização que tenta ler dado de outra recebe `403`. Isso vale para todos os endpoints, e é aplicado no backend. Não existe caminho pela interface que contorne.

## Descobrir o que o token pode

Aqui vem uma limitação que vale conhecer antes de perder tempo: **nenhum endpoint responde "o que este token pode fazer".** Os dois candidatos óbvios respondem outra coisa.

| Endpoint | O que ele realmente devolve |
|----------|------------------------------|
| `GET /api/auth/permissions` | A matriz inteira de papel × permissão. Uma tabela de referência, igual para todo mundo. Não olha o seu token. |
| `GET /api/auth/me` | O perfil e as permissões **do dono** do token, pelo papel dele. Ignora os scopes da credencial que você usou. |

O segundo engana de verdade. Um token restrito a `["mapping.read"]`, emitido por um admin, faz `/api/auth/me` responder com as 23 permissões. Um cliente que se autoconfigure lendo essa resposta vai habilitar funções que tomam `403` no primeiro uso.

Enquanto isso não muda, trate os scopes que você pediu na criação como a fonte da verdade, e valide com uma chamada real ao endpoint que interessa antes de colocar a automação em produção.

:::danger[A criação não recusa scope fora do papel]
`POST /api/v1/tokens` valida apenas que cada scope existe. Ele **não** checa se cabe no papel do dono.

Um viewer que peça `scopes: ["user.manage"]` recebe `201 Created`, com o scope gravado. O token não ganha o poder (a interseção com o papel continua valendo em cada requisição), mas nada avisa no momento da emissão: você descobre no `403`, em produção.

Confira contra a matriz acima antes de emitir.
:::

Um detalhe de robustez que também surpreende: se o campo de scopes do token ficar corrompido no banco, o sistema lê como "sem restrição" e o token passa a valer o papel inteiro. A falha **escala** a credencial em vez de degradá-la. É mais um motivo para o papel do dono ser o menor possível, em vez de confiar só no recorte por scope.

## Escolhendo o perfil certo

| Você quer | Papel | Scopes sugeridos |
|-----------|-------|------------------|
| Monitorar saúde, sem tocar em nada | `viewer` | `destination.read`, `route.read`, `integration.read` |
| Monitorar e destravar coletor parado | `operator` | acima, mais `integration.reset`, `integration.pause` |
| Limpar quarentena no plantão | `operator` | `quarantine.read`, `quarantine.discard` |
| Publicar mapping por pipeline | `engineer` | `mapping.read`, `mapping.write` |
| Inventário e auditoria | `viewer` | `audit.read`, `integration.read`, `mapping.read` |

Para o caso de monitoramento, [Receitas](recipes.md) traz o passo a passo completo com um exemplo de coleta periódica.
