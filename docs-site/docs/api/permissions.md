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
| `integration.pause` | nao | sim | sim | sim | Existe no enum, mas **nenhum endpoint a usa**. Ligar e desligar coleta é feito pelo `PUT` da integração, que exige `integration.write`. |
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
| `audit.read` | sim | sim | sim | sim | Cobre **um** endpoint: a auditoria de um mapping. A trilha da plataforma exige `user.manage`. |
| `query.run` | nao | sim | sim | sim | Rodar consulta ao vivo na fonte do cliente |
| `query.save` | nao | nao | sim | sim | Salvar consulta e agendamento |
| `correlation.preview` | nao | nao | sim | sim | Nenhum endpoint do Core a consome. As regras de correlação são Enterprise. |
| `internal.tenant.read` | nao | sim | sim | sim | Resolução de tenant entre serviços |
| `user.manage` | nao | nao | nao | sim | Criar, editar e remover usuários |
| `org.manage` | nao | nao | nao | sim | Apagar os DADOS de uma organização. Criar, editar e apagar a organização em si exigem `user.manage`, e criar ou apagar exigem ainda escopo global. |
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

- Pedir um scope que o papel não tem **não concede nada** na hora de usar. Um viewer com token `scopes: ["user.manage"]` continua sem `user.manage` em cada requisição.
- Se o dono for rebaixado depois, o token encolhe junto, na hora. Não existe permissão congelada no momento da emissão.

:::danger[O recorte por scope não contém quem tem o token]
Emitir token exige apenas estar autenticado, sem permissão específica. Um token restrito a `["mapping.read"]` pode chamar `POST /api/v1/tokens` e criar **outro** token, sem scope nenhum, que sai com o papel inteiro do dono.

Ou seja, o recorte por scope protege contra uso acidental e contra um vazamento que o atacante não perceba. Ele **não** é uma fronteira de contenção: quem tem o token restrito consegue, em uma chamada, um token sem restrição.

Se você precisa de contenção real, o papel do dono é que precisa ser pequeno. Use uma conta de serviço com o papel mínimo em vez de restringir por scope um token de admin.
:::

:::caution[A interseção tem uma exceção conhecida: `secret.read`]
A visibilidade de campos de credencial nas leituras de integração é decidida olhando direto o papel do dono, sem passar pelo cálculo de scopes.

Na prática, um token de um admin restrito a `["integration.read"]` continua enxergando os campos protegidos por `secret.read` nessas respostas. O recorte não vale ali.
:::

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

Um admin com organização definida e `is_global = false` é um **admin de organização**: administra a própria organização, não a plataforma.

Um viewer, operator ou engineer com `is_global = true` é o analista que monitora todos os clientes com as permissões do próprio papel, sem poder administrativo.

:::caution[Ver as organizações filhas é recurso Enterprise]
Na edição Community, quem não tem escopo global enxerga **somente a própria organização**, e nada das filhas. A visibilidade de subárvore, que é o caso de uso de MSP e revenda, depende de um resolvedor que o pacote Enterprise registra.

A degradação não gera erro: a mesma automação rodando contra uma instância Community simplesmente devolve menos linhas. Ela subexpõe em vez de vazar, o que é o lado seguro, mas nada avisa que faltou dado.
:::

### A resposta vazia que parece "não tem dado"

Esta é a pegadinha que mais custa tempo, e ela não gera erro nenhum.

Quando um token de escopo global chama um endpoint de dado de tenant **sem nomear a organização**, o sistema não agrega os tenants todos. Ele resolve para a organização do próprio dono. Num admin de plataforma essa organização é vazia, e a resposta volta `200` com resultado vazio.

(Escopo global e organização vazia não são a mesma coisa: um usuário com `is_global = true` pode ter organização definida, e aí a resposta vem daquela organização, não de todas.)

Você lê "0 resultados" e conclui que não há dado. Na verdade a pergunta é que não nomeou o cliente.

```bash
# Token global, sem dizer a organização: volta vazio
curl -H "Authorization: Bearer $TOKEN" \
     "https://centralops.example.com/api/mappings/samples?vendor=sophos&event_type=detections"

# Mesma pergunta, nomeando o tenant: volta o dado
curl -H "Authorization: Bearer $TOKEN" \
     "https://centralops.example.com/api/mappings/samples?vendor=sophos&event_type=detections&org_id=7"
```

Repare em dois detalhes que custam uma tentativa cada: `event_type` é obrigatório neste endpoint (sem ele a resposta é `422`, não uma lista vazia), e o parâmetro de organização aqui chama **`org_id`**, não `organization_id`.

A regra prática: se você usa token global e uma leitura de dado de tenant voltou vazia, **nomeie a organização antes de concluir qualquer coisa**.

Os nomes não são uniformes na API. Alguns endpoints usam `organization_id`, outros `org_id`, e o mesmo recurso pode usar um nome na query e outro no corpo. A [referência](reference.md) e o esquema OpenAPI trazem o nome certo por endpoint. Parâmetro com nome errado costuma ser ignorado em silêncio.

### Cruzar organizações

Um token escopado a uma organização que tenta ler dado de outra é barrado no backend, e não existe caminho pela interface que contorne.

A resposta, porém, **não é sempre `403`**. Vários endpoints devolvem `404` de propósito, para não confirmar que o recurso existe. Ao tratar erro em automação, considere as duas.

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
| Inventário e auditoria de mappings | `viewer` | `audit.read`, `integration.read`, `mapping.read` |
| Trilha de auditoria da plataforma | `admin` | não há recorte: exige `user.manage` |

Para o caso de monitoramento, [Receitas](recipes.md) traz o passo a passo completo com um exemplo de coleta periódica.
