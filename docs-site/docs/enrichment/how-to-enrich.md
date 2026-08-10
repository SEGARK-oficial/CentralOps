---
sidebar_position: 2
title: Como enriquecer um evento
description: Passo a passo completo para criar uma tabela, escrever uma política e testar antes de publicar
---

# Como enriquecer um evento

Este guia cria, do zero, um enriquecimento completo: uma tabela com o plano de endereçamento da sua empresa, e uma política que marca cada evento com o **site** e a **criticidade** do IP de origem. É o exemplo mais direto porque não depende de nenhuma integração externa — só do seu próprio dado.

**Quem usa**: enriquecimento é recurso de administrador, escopado por organização.

:::tip[Dois jeitos de fazer o mesmo fluxo]
As telas do console (**Enriquece → Enrichment**) já têm formulário completo para criar tabela, publicar dados, escrever política e testar antes de publicar — é o caminho mais direto para a maioria dos casos, e o que este guia mostra primeiro. Para automação, scripts, CI ou importação em massa, a API REST expõe exatamente os mesmos passos; a segunda metade deste guia mostra o mesmo exemplo com `curl`.
:::

## Pelo console

### 1. Veja o que já está disponível

Abra **Enriquece → Enrichment**. A aba **Catalog** lista toda fonte de enriquecimento disponível na sua instância — plugin-driven, então o que aparece aqui reflete exatamente o que o backend tem registrado. Para este exemplo, você vai usar **Tabela do cliente (CIDR)** — a fonte que casa um IP contra a sua própria tabela, pelo prefixo de rede **mais específico**.

![Catálogo de fontes de enriquecimento](/img/console/console-enriquecimento-catalogo.png)

Repare no selo de egresso em cada card: fontes marcadas **sem egresso** nunca enviam nada do seu ambiente para fora; fontes marcadas **envia a terceiro** (como VirusTotal) exigem opt-in — veja o aviso amarelo no topo da aba quando alguma fonte assim está no catálogo.

### 2. Crie a tabela

Na aba **Tables**, clique em **New table**. Escolha a organização, dê um nome (`rede-corporativa`), uma descrição, e o tipo de casamento:

| Tipo de casamento | Como casa | Use para |
|---|---|---|
| chave exata | Igualdade exata da chave | Listas de usuários, hosts, hashes — qualquer coisa que se compara ao pé da letra |
| CIDR | O prefixo de rede **mais específico** que contém o IP | Planos de endereçamento, inventário de rede, listas de bloqueio por sub-rede |

![Formulário de nova tabela de enriquecimento](/img/console/console-enriquecimento-nova-tabela.png)

A tabela nasce **vazia** — criar só reserva o nome e o formato da chave. Os dados entram no próximo passo.

### 3. Publique os dados da tabela

Clique na tabela recém-criada para abrir o histórico de versões. Cole as linhas como JSON `{chave: {campo: valor}}` — a mesma forma que a API espera — e escreva uma mensagem de commit:

```json
{
  "10.0.0.0/16": {"site": "matriz",    "criticality": "normal"},
  "10.0.5.0/24":  {"site": "filial-sp", "criticality": "alta"},
  "10.9.0.0/24":  {"site": "dmz",       "criticality": "critica"}
}
```

Cada versão publicada fica guardada no histórico — publicar uma nova não apaga a anterior, e dá para reverter para qualquer versão com um clique. Se alguma chave não for um CIDR/IP válido, ela é **descartada e contada** como linha inválida — o resto da tabela é publicado normalmente; confira sempre esse número antes de seguir em frente.

### 4. Crie a política e escreva a regra

Na aba **Policies**, clique em **New policy** e dê um nome (`contexto-de-ativo`). Criar a política **não a habilita** — ela só passa a valer depois do passo 6.

Clique na política recém-criada e, no editor de regras, clique em **Adicionar regra**. Cada regra tem os mesmos cinco campos do formato da API — enricher, tabela, origem da chave, saídas — só que como formulário em vez de JSON:

- **Enricher**: `table_cidr`
- **Tabela**: `rede-corporativa`
- **Origem da chave**: `normalized.src_endpoint.ip` (já vem preenchido)
- **Saídas**: campo do resultado `site` grava em `_centralops.enrichment.src.site`; adicione uma segunda saída para `criticality`
- **Se não encontrar**: `tag` — em vez de não fazer nada, marca o evento com uma tag de "não reconhecido", útil para depois rotear esses eventos para revisão

Repare que todo campo "Grava em" começa com `_centralops.enrichment` — é a regra fixa do sistema: o enriquecimento nunca escreve em cima do evento normalizado, só na seção reservada a ele (veja [O que é o enriquecimento](./overview.md#onde-o-resultado-é-escrito)).

### 5. Teste antes de publicar

Ainda no editor, role até **Testar antes de publicar**. Cole um evento de exemplo e, se a tabela ainda não tiver a versão que você quer testar, um JSON de tabelas simuladas — depois clique **Testar**. Nada disso publica dado nem toca em tráfego real.

![Editor de regras com resultado do dry-run](/img/console/console-enriquecimento-editor-regras-dryrun.png)

O resultado mostra o evento **depois** de enriquecido, quantos hits/misses/erros cada regra teve, e quantos bytes o enriquecimento acrescentaria. Ajuste a regra e teste de novo quantas vezes precisar — só publique quando o resultado bater com o esperado.

### 6. Publique a versão e habilite

Escreva uma mensagem de commit e clique **Publicar versão**. Com uma versão publicada, o botão **Habilitar** no topo do modal fica disponível — clique nele para a política passar a valer para eventos novos desta organização.

![Tabela publicada e política ativa](/img/console/console-enriquecimento-politicas.png)

### 7. Acompanhe

As mesmas abas **Tables** e **Policies** mostram o estado sempre atualizado: entradas e tamanho de cada tabela, se uma política está ativa, quantas regras tem. É a mesma tela do passo 1 — não tem uma tela "de resultado" separada, o estado É a tela.

## Via API (automação e scripts)

O mesmo fluxo acima, chamando a API REST diretamente — útil para CI, scripts de bootstrap, importação em massa de tabelas grandes, ou qualquer automação que não passe por um navegador.

### Antes de começar

Você vai precisar de:

- **Acesso de administrador** à sua instalação do CentralOps.
- Um jeito de chamar a API — a URL base é `https://<seu-console>/api/collectors/enrichment`.
- Autenticação: um **token de API pessoal**, enviado no cabeçalho `Authorization: Bearer copsk_...`. Gere um em **Conta → Tokens**, no menu do seu usuário. (Se você está testando pela sessão do próprio navegador, os exemplos abaixo funcionam do mesmo jeito trocando o header por autenticação de sessão.)

Os exemplos usam `curl`. Troque `$TOKEN` pelo seu token e `$HOST` pelo endereço do seu console.

```bash
export HOST="https://seu-console.exemplo.com"
export TOKEN="copsk_xxxxxxxxxxxxxxxx"
```

### 1. Veja o que já está disponível

Todo enriquecimento usa uma fonte do catálogo. Para este exemplo, você vai usar `table_cidr` — a fonte que casa um IP contra a sua própria tabela, pelo prefixo de rede **mais específico**.

```bash
curl -s "$HOST/api/collectors/enrichment/enrichers" \
  -H "Authorization: Bearer $TOKEN" | jq '.[] | {name, mode, egress, key_kinds}'
```

Você verá algo como:

```json
{"name": "table_exact", "mode": "local", "egress": "none", "key_kinds": ["ip","domain","url","file_hash","cve","mac","user","container_id"]}
{"name": "table_cidr",  "mode": "local", "egress": "none", "key_kinds": ["ip"]}
{"name": "opencti",     "mode": "local", "egress": "internal", "key_kinds": ["ip","domain","url","file_hash","mac"]}
{"name": "virustotal",  "mode": "remote","egress": "third_party", "key_kinds": ["ip","domain","file_hash"]}
```

### 2. Crie a tabela

```bash
curl -s -X POST "$HOST/api/collectors/enrichment/tables" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "rede-corporativa",
    "match_mode": "cidr",
    "description": "Plano de endereçamento: matriz, filiais e DMZ"
  }'
```

`match_mode` define como a chave é comparada:

| `match_mode` | Como casa | Use para |
|---|---|---|
| `exact` | Igualdade exata da chave | Listas de usuários, hosts, hashes — qualquer coisa que se compara ao pé da letra |
| `cidr` | O prefixo de rede **mais específico** que contém o IP | Planos de endereçamento, inventário de rede, listas de bloqueio por sub-rede |

A resposta traz o `id` da tabela — guarde-o, é o que você vai usar no próximo passo:

```bash
export TABLE_ID="2086c3af-2bb0-4cd9-9bc8-4ccce95d13ee"
```

### 3. Publique os dados da tabela

Uma tabela recém-criada **não tem nenhum dado até você publicar uma versão**. É só depois de publicar que ela conta com entradas.

```bash
curl -s -X POST "$HOST/api/collectors/enrichment/tables/$TABLE_ID/versions" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "rows": {
      "10.0.0.0/16": {"site": "matriz",    "criticality": "normal"},
      "10.0.5.0/24":  {"site": "filial-sp", "criticality": "alta"},
      "10.9.0.0/24":  {"site": "dmz",       "criticality": "critica"}
    },
    "commit_message": "inventário inicial"
  }'
```

Cada versão fica guardada — publicar uma nova não apaga o histórico, e você pode reverter para uma anterior a qualquer momento (`POST .../tables/{id}/rollback`).

:::tip[Uma linha errada não derruba o upload inteiro]
Se alguma chave não for um CIDR/IP válido, ela é **descartada e contada** em `invalid_rows` na resposta — o resto da tabela é publicado normalmente. Confira sempre esse número: se vier maior que zero, alguma linha do seu arquivo precisa de correção.
:::

### 4. Crie a política

A política é o que decide **quando** aplicar o enriquecimento e **onde** escrever o resultado.

```bash
curl -s -X POST "$HOST/api/collectors/enrichment/policies" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "contexto-de-ativo",
    "description": "Marca o evento com site e criticidade do ativo de origem"
  }'
```

Guarde o `id` retornado:

```bash
export POLICY_ID="a1b2c3d4-..."
```

Criar a política **não a habilita** — ela só passa a valer depois do passo 6.

### 5. Escreva a regra e publique a versão

Uma versão da política é uma lista de regras. Cada regra tem cinco partes:

| Campo | O que faz |
|---|---|
| `id` | Identificador único da regra dentro da política |
| `enricher` | Qual fonte usar (`table_cidr`, `table_exact`, `opencti`, `virustotal`, ...) |
| `table` | Nome da tabela a consultar (só para os enrichers de tabela) |
| `key.source` | De onde tirar o valor a buscar — um caminho dentro do evento normalizado |
| `outputs` | Lista de `{from, target}`: qual campo do resultado escrever, e em qual campo do evento |

```bash
curl -s -X POST "$HOST/api/collectors/enrichment/policies/$POLICY_ID/versions" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "rules": [
      {
        "id": "asset",
        "enricher": "table_cidr",
        "table": "rede-corporativa",
        "key": { "source": "normalized.src_endpoint.ip", "kind": "ip" },
        "outputs": [
          { "from": "site",        "target": "_centralops.enrichment.src.site" },
          { "from": "criticality", "target": "_centralops.enrichment.src.criticality" }
        ],
        "tags": ["asset_known"],
        "on_miss": "tag"
      }
    ],
    "commit_message": "v1"
  }'
```

Repare que todo `target` começa com `_centralops.enrichment` — é a regra fixa do sistema: o enriquecimento nunca escreve em cima do evento normalizado, só na seção reservada a ele (veja [O que é o enriquecimento](./overview.md#onde-o-resultado-é-escrito)).

`on_miss: "tag"` decide o que fazer quando o IP **não** bate em nenhuma faixa da tabela: em vez de não fazer nada, marca o evento com uma tag de "não reconhecido" — útil para depois rotear esses eventos para uma fila de revisão. As outras opções são `"skip"` (não faz nada) e `"default"` (grava um valor padrão, se você declarar um em `outputs[].default`).

Se a regra tiver um erro — um campo desconhecido, um `target` fora de `_centralops.enrichment`, ou uma tabela que não existe — a API responde **422** com o motivo, na hora do commit. A política inválida nunca chega a rodar contra tráfego real.

### 6. Teste antes de habilitar

Antes de ligar a política de verdade, teste com um evento de exemplo — sem publicar nada e sem tocar em tráfego real:

```bash
curl -s -X POST "$HOST/api/collectors/enrichment/dry-run" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "rules": [
      {
        "id": "asset",
        "enricher": "table_cidr",
        "table": "rede-corporativa",
        "key": { "source": "normalized.src_endpoint.ip", "kind": "ip" },
        "outputs": [
          { "from": "site", "target": "_centralops.enrichment.src.site" }
        ]
      }
    ],
    "sample": {
      "_centralops": { "organization_id": 1 },
      "normalized": { "src_endpoint": { "ip": "10.0.5.7" } },
      "raw": {}
    },
    "tables": { "asset": { "10.0.5.7": { "site": "filial-sp" } } }
  }'
```

A resposta mostra o evento **depois** de enriquecido (`enriched`), quantos `hits`/`misses` cada regra teve, e `bytes_added` — quantos bytes o enriquecimento acrescentaria a este evento específico. O campo `tables` no pedido deixa simular o conteúdo da tabela sem precisar publicar nada — útil para desenhar a regra antes mesmo de a tabela existir.

### 7. Habilite a política

```bash
curl -s -X POST "$HOST/api/collectors/enrichment/policies/$POLICY_ID/enable?enabled=true" \
  -H "Authorization: Bearer $TOKEN"
```

A partir daqui, todo evento novo dessa organização passa pela regra.

### 8. Confirme no console

O console lê o mesmo estado que a API acabou de escrever — não precisa de nenhum passo extra para "sincronizar". Abra **Enriquece → Enrichment** e confira:

- Na aba **Tables**, `rede-corporativa` aparece com 3 entradas.
- Na aba **Policies**, `contexto-de-ativo` aparece como **active**, com 1 regra.

![Tabela publicada e política ativa](/img/console/console-enriquecimento-politicas.png)

## Usando uma fonte pronta (OpenCTI, VirusTotal)

O mesmo formato de regra vale para os enrichers do catálogo — só muda o `enricher` e, em vez de `table`, você configura a fonte com suas próprias credenciais (isso é feito na configuração do enricher, fora do escopo deste guia rápido).

Duas diferenças importantes a considerar antes de usar uma fonte externa:

- **OpenCTI** roda **por evento** (mesma velocidade das tabelas próprias) e não envia nada para fora — a instância é sua.
- **VirusTotal** roda **por lote**, e a chave pública libera só **4 consultas por minuto**. Sem um `when` restritivo na regra (por exemplo, só consultar IPs que ainda não foram marcados como conhecidos por outra regra), a cota se esgota em segundos. Veja o aviso de egresso no card do catálogo antes de habilitar.

## Problemas comuns

### A política não faz nada depois de habilitada

**Causa mais provável**: a política foi habilitada sem nenhuma versão publicada, ou a tabela referenciada por uma regra não existe (ou não tem versão publicada).

**Como conferir**: `GET /collectors/enrichment/policies/{id}` — se `current_version_id` estiver vazio, publique uma versão (passo 5). Confira também a tabela: se aparecer o selo **no published version** na aba **Tables**, volte ao passo 3.

### Publicar a versão da política dá 422 "tabela inexistente"

A regra referencia, em `table`, um nome que não existe **nesta organização**. Tabelas não são compartilhadas entre organizações — confira o nome exato na aba **Tables** ou via `GET /collectors/enrichment/tables`.

### Não consigo apagar uma tabela

Uma tabela referenciada por alguma política não pode ser apagada — apagar quebraria a regra em silêncio a cada ciclo. Remova a regra que a usa (ou desabilite a política) antes de apagar a tabela.

### `invalid_rows` maior que zero ao publicar uma versão de tabela CIDR

Alguma chave da tabela não é um IP nem uma faixa CIDR válida (ex.: um texto digitado errado). Essas linhas são descartadas — a linha exata não vem na resposta hoje, então revise o arquivo de origem por padrões óbvios (espaços, máscara faltando) e publique de novo.

## Próximos passos

- **Quer entender os conceitos antes de configurar?** Veja [O que é o enriquecimento](./overview.md).
- **Quer saber o que cada fonte do catálogo faz e exige?** Veja [Catálogo de fontes](./catalog.md).
