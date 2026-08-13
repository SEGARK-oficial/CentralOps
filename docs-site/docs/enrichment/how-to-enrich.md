---
sidebar_position: 2
title: Como enriquecer um evento
description: Passo a passo completo para criar uma tabela, escrever uma política e testar antes de publicar
---

# Como enriquecer um evento

Este guia cria, do zero, um enriquecimento completo: uma tabela com o plano de endereçamento da sua empresa, e uma política que marca cada evento com o **site** e a **criticidade** do IP de origem. É o exemplo mais direto porque não depende de nenhuma integração externa, só do seu próprio dado.

**Quem usa**: enriquecimento é recurso de administrador, escopado por organização.

:::tip[Dois jeitos de fazer o mesmo fluxo]
As telas do console (**Enriquece → Enriquecimento**) já têm formulário completo para criar tabela, publicar dados, escrever política e testar antes de publicar, é o caminho mais direto para a maioria dos casos, e o que este guia mostra primeiro. Para automação, scripts, CI ou importação em massa, a API REST expõe exatamente os mesmos passos; a segunda metade deste guia mostra o mesmo exemplo com `curl`.
:::

## Antes de começar: a chave mestra

O enriquecimento inteiro está atrás de `ENRICHMENT_ENABLED`, que vem **desligada** de fábrica (`ENRICHMENT_ENABLED: bool = False`). Com ela desligada, tudo neste guia funciona no console (criar tabela, publicar política, habilitar, testar com dry-run) e **nada acontece com o tráfego real**: o worker nem carrega a política.

Ligue no ambiente do worker antes de esperar resultado:

```bash
ENRICHMENT_ENABLED=true
```

O enriquecimento **remoto** tem um segundo requisito, `ENRICH_REDIS_URL`, apontando para uma instância Redis dedicada. Sem ela, os enrichers locais (tabelas, OpenCTI, TAXII) funcionam e os remotos (VirusTotal) ficam desligados. A aba **Execução** reporta isso explicitamente, com o motivo, em vez de deixar parecer que a consulta está de pé.

## Pelo console

### 1. Veja o que já está disponível

Abra **Enriquece → Enriquecimento**. A aba **Catálogo** lista toda fonte de enriquecimento disponível na sua instância, plugin-driven, então o que aparece aqui reflete exatamente o que o backend tem registrado. Para este exemplo, você vai usar **Tabela do cliente (CIDR)**, a fonte que casa um IP contra a sua própria tabela, pelo prefixo de rede **mais específico**.

![Catálogo de fontes de enriquecimento](/img/console/console-enriquecimento-catalogo.png)

Repare no selo de egresso em cada card: fontes marcadas **sem egresso** nunca enviam nada do seu ambiente para fora; fontes marcadas **envia a terceiro** (como VirusTotal) exigem opt-in, veja o aviso amarelo no topo da aba quando alguma fonte assim está no catálogo.

### 2. Crie a tabela

Na aba **Tabelas**, clique em **Nova tabela**. Escolha a organização, dê um nome (`rede-corporativa`), uma descrição, e o tipo de casamento:

| Tipo de casamento | Como casa | Use para |
|---|---|---|
| chave exata | Igualdade exata da chave | Listas de usuários, hosts, hashes, qualquer coisa que se compara ao pé da letra |
| CIDR | O prefixo de rede **mais específico** que contém o IP | Planos de endereçamento, inventário de rede, listas de bloqueio por sub-rede |

![Formulário de nova tabela de enriquecimento](/img/console/console-enriquecimento-nova-tabela.png)

A tabela nasce **vazia**, criar só reserva o nome e o formato da chave. Os dados entram no próximo passo.

### 3. Publique os dados da tabela

Clique na tabela recém-criada para abrir o histórico de versões. Cole as linhas como JSON `{chave: {campo: valor}}`, a mesma forma que a API espera, e escreva uma mensagem de commit:

```json
{
  "10.0.0.0/16": {"site": "matriz",    "criticality": "normal"},
  "10.0.5.0/24":  {"site": "filial-sp", "criticality": "alta"},
  "10.9.0.0/24":  {"site": "dmz",       "criticality": "critica"}
}
```

Cada versão publicada fica guardada no histórico, publicar uma nova não apaga a anterior, e dá para reverter para qualquer versão com um clique. Se alguma chave não for um CIDR/IP válido, ela é **descartada e contada** como linha inválida, o resto da tabela é publicado normalmente; confira sempre esse número antes de seguir em frente.

### 4. Crie a política e escreva a regra

Na aba **Políticas**, clique em **Nova política** e dê um nome (`contexto-de-ativo`). Criar a política **não a habilita**, ela só passa a valer depois do passo 6.

Clique na política recém-criada e, no editor de regras, clique em **Adicionar regra**. Cada regra tem os mesmos cinco campos do formato da API, enricher, tabela, origem da chave, saídas, só que como formulário em vez de JSON:

- **Enricher**: `table_cidr`
- **Tabela**: `rede-corporativa`
- **Origem da chave**: `normalized.src_endpoint.ip` (já vem preenchido)
- **Saídas**: campo do resultado `site` grava em `_centralops.enrichment.src.site`; adicione uma segunda saída para `criticality`
- **Se não encontrar**: `tag`, em vez de não fazer nada, marca o evento com uma tag de "não reconhecido", útil para depois rotear esses eventos para revisão

O prefixo `_centralops.enrichment.` do campo "Grava em" é fixo e não editável. Não é preferência de estilo: fora dessa raiz o enriquecimento escreveria em cima do evento normalizado, e o dado não ficaria sob a redação de PII (veja [O que é o enriquecimento](./overview.md#onde-o-resultado-é-escrito)).

Dois campos evitam erro que não dá erro:

- **Campo do resultado** vira uma lista quando o enricher declara o que devolve. Nomear um campo que a fonte não retorna não causa falha: a regra roda, não acha nada, e nada é escrito.
- **Tags** entram como chips, com sugestão das tags que as outras regras já usam. `asset_conhecido` e `asset_known` são tags diferentes para o runtime, e a regra que consome a tag errada simplesmente para de casar, sem erro em lugar nenhum.

### 4.1. Condições: quando a regra roda

Cada regra tem um campo **Quando aplicar**. Por padrão é "Sempre". As outras opções olham para dois lugares diferentes:

| Condição | Olha para | Exemplo |
|---|---|---|
| Se o campo existir / for igual a / estiver na lista | O evento | só consultar TI quando `normalized.src_endpoint.ip` existir |
| Se NÃO | Inverte qualquer uma acima | pular quando o IP for interno |
| Se o evento já tiver a tag / NÃO tiver a tag | Tags que regras **anteriores** escreveram | só consultar a fonte paga quando o CMDB local não achou |

**Uma condição por regra.** Não existe "E"/"OU" no formato, e isso é deliberado: condição composta se escreve encadeando regras, uma marcando com tag e a seguinte reagindo a essa tag. O botão **Adicionar alternativa** monta esse par para você, com a tag já casando dos dois lados, que é a parte fácil de errar à mão.

O caso comum é a cascata "primeiro o barato, depois o caro":

1. Regra `cmdb` consulta a tabela local e marca `achou_no_cmdb` quando acerta.
2. Regra `ti-remota` roda com **Se o evento NÃO tiver a tag** `achou_no_cmdb`.

Assim a consulta paga só sai para o que a tabela local não resolveu.

### 5. Teste antes de publicar

Ainda no editor, role até **Testar antes de publicar**. Cole um evento de exemplo e, se a tabela ainda não tiver a versão que você quer testar, um JSON de tabelas simuladas, depois clique **Testar**. Nada disso publica dado nem toca em tráfego real.

![Editor de regras com resultado do dry-run](/img/console/console-enriquecimento-editor-regras-dryrun.png)

O resultado mostra o evento **depois** de enriquecido, quantos hits/misses/erros cada regra teve, e quantos bytes o enriquecimento acrescentaria. Ajuste a regra e teste de novo quantas vezes precisar, só publique quando o resultado bater com o esperado.

### 6. Publique a versão e habilite

Escreva uma mensagem de commit e clique **Publicar versão**. Com uma versão publicada, o botão **Habilitar** no topo do modal fica disponível, clique nele para a política passar a valer para eventos novos desta organização.

:::warning[Uma política por organização]
O worker aplica **uma só** política por organização: a mais antiga que estiver habilitada e tiver versão publicada. Habilitar uma segunda não soma nada, e a segunda simplesmente não roda.

Isso costuma aparecer como "editei a política e não mudou nada": a edição foi na política errada. A aba **Execução** mostra qual está valendo, no selo ao lado de "Aproveitamento por regra". Para trocar qual vale, desabilite a antiga.
:::

:::warning[Publicar substitui todas as regras]
Publicar não faz merge: a versão nova é a lista inteira de regras do editor. Por isso o editor **abre já com as regras da versão vigente**, e o selo "Editando a partir da vN" no topo diz de onde elas vieram. Apagar uma regra do editor e publicar é como se remove uma regra; nada mais some sozinho.

No histórico, **Carregar no editor** traz as regras de qualquer versão antiga para revisar e publicar por cima. É diferente de **Reverter para esta**, que troca a versão vigente na hora, sem passar pelo editor.
:::

![Tabela publicada e política ativa](/img/console/console-enriquecimento-politicas.png)

### 7. Acompanhe

As abas **Tabelas** e **Políticas** mostram o estado da configuração: entradas e tamanho de cada tabela, se uma política está ativa, quantas regras tem.

Para saber se o enriquecimento está **de fato funcionando**, use a aba **Execução**. Ela responde duas perguntas diferentes, de propósito:

**Consultas** lista cada tentativa de ir buscar dado, com a mensagem do provedor quando falha. É aqui que aparecem token errado (`401 Unauthorized`), DNS quebrado e coleção TAXII vazia, sem precisar abrir log de worker. Há um registro por carga de tabela (uma por ciclo de coleta) e um por consulta remota (uma por lote). Não há registro por evento, e isso é limite de projeto: gravar por evento transformaria o log de diagnóstico em gargalo do caminho quente.

**Aproveitamento por regra** mostra quanto cada regra casou na janela. Uma regra que vinha em 90% de acerto e foi a zero **com as consultas funcionando** mudou de formato na origem, não de credencial. Regra que não disparou nenhuma vez aparece como "Não disparou na janela", não como 0% de acerto: as duas situações pedem ações opostas, e confundi-las manda você mexer na tabela quando o problema é a política estar desligada.

O log guarda as últimas 200 consultas por organização, por 24 horas. Serve para diagnóstico imediato; para histórico longo, use as métricas exportadas por OpenTelemetry.

## Via API (automação e scripts)

O mesmo fluxo acima, chamando a API REST diretamente, útil para CI, scripts de bootstrap, importação em massa de tabelas grandes, ou qualquer automação que não passe por um navegador.

### Antes de começar

Você vai precisar de:

- **Acesso de administrador** à sua instalação do CentralOps.
- Um jeito de chamar a API, a URL base é `https://<seu-console>/api/collectors/enrichment`.
- Autenticação: um **token de API pessoal**, enviado no cabeçalho `Authorization: Bearer copsk_...`. Gere um em **Conta → Tokens**, no menu do seu usuário. (Se você está testando pela sessão do próprio navegador, os exemplos abaixo funcionam do mesmo jeito trocando o header por autenticação de sessão.)

Os exemplos usam `curl`. Troque `$TOKEN` pelo seu token e `$HOST` pelo endereço do seu console.

```bash
export HOST="https://seu-console.exemplo.com"
export TOKEN="copsk_xxxxxxxxxxxxxxxx"
```

### 1. Veja o que já está disponível

Todo enriquecimento usa uma fonte do catálogo. Para este exemplo, você vai usar `table_cidr`, a fonte que casa um IP contra a sua própria tabela, pelo prefixo de rede **mais específico**.

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
| `exact` | Igualdade exata da chave | Listas de usuários, hosts, hashes, qualquer coisa que se compara ao pé da letra |
| `cidr` | O prefixo de rede **mais específico** que contém o IP | Planos de endereçamento, inventário de rede, listas de bloqueio por sub-rede |

A resposta traz o `id` da tabela, guarde-o, é o que você vai usar no próximo passo:

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

Cada versão fica guardada, publicar uma nova não apaga o histórico, e você pode reverter para uma anterior a qualquer momento (`POST .../tables/{id}/rollback`).

:::tip[Uma linha errada não derruba o upload inteiro]
Se alguma chave não for um CIDR/IP válido, ela é **descartada e contada** em `invalid_rows` na resposta, o resto da tabela é publicado normalmente. Confira sempre esse número: se vier maior que zero, alguma linha do seu arquivo precisa de correção.
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

Criar a política **não a habilita**, ela só passa a valer depois do passo 6.

### 5. Escreva a regra e publique a versão

Uma versão da política é uma lista de regras. Cada regra tem cinco partes:

| Campo | O que faz |
|---|---|
| `id` | Identificador único da regra dentro da política |
| `enricher` | Qual fonte usar (`table_cidr`, `table_exact`, `opencti`, `virustotal`, ...) |
| `table` | Nome da tabela a consultar (só para os enrichers de tabela) |
| `key.source` | De onde tirar o valor a buscar, um caminho dentro do evento normalizado |
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

Repare que todo `target` começa com `_centralops.enrichment`, é a regra fixa do sistema: o enriquecimento nunca escreve em cima do evento normalizado, só na seção reservada a ele (veja [O que é o enriquecimento](./overview.md#onde-o-resultado-é-escrito)).

`on_miss: "tag"` decide o que fazer quando o IP **não** bate em nenhuma faixa da tabela: em vez de não fazer nada, marca o evento com uma tag de "não reconhecido", útil para depois rotear esses eventos para uma fila de revisão. As outras opções são `"skip"` (não faz nada) e `"default"` (grava um valor padrão, se você declarar um em `outputs[].default`).

Se a regra tiver um erro, um campo desconhecido, um `target` fora de `_centralops.enrichment`, ou uma tabela que não existe, a API responde **422** com o motivo, na hora do commit. A política inválida nunca chega a rodar contra tráfego real.

### 6. Teste antes de habilitar

Antes de ligar a política de verdade, teste com um evento de exemplo, sem publicar nada e sem tocar em tráfego real:

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

A resposta mostra o evento **depois** de enriquecido (`enriched`), quantos `hits`/`misses` cada regra teve, e `bytes_added`, quantos bytes o enriquecimento acrescentaria a este evento específico. O campo `tables` no pedido deixa simular o conteúdo da tabela sem precisar publicar nada, útil para desenhar a regra antes mesmo de a tabela existir.

### 7. Habilite a política

```bash
curl -s -X POST "$HOST/api/collectors/enrichment/policies/$POLICY_ID/enable?enabled=true" \
  -H "Authorization: Bearer $TOKEN"
```

A partir daqui, todo evento novo dessa organização passa pela regra.

### 8. Confirme no console

O console lê o mesmo estado que a API acabou de escrever, não precisa de nenhum passo extra para "sincronizar". Abra **Enriquece → Enriquecimento** e confira:

- Na aba **Tabelas**, `rede-corporativa` aparece com 3 entradas.
- Na aba **Políticas**, `contexto-de-ativo` aparece como **active**, com 1 regra.

![Tabela publicada e política ativa](/img/console/console-enriquecimento-politicas.png)

## Usando uma fonte pronta (OpenCTI, VirusTotal)

O mesmo formato de regra vale para os enrichers do catálogo, só muda o `enricher` e, em vez de `table`, a regra cita uma **fonte configurada** com `source`.

### Antes: crie a fonte

Uma fonte é a instância de um enricher **nesta organização**: o endereço com que ele fala e a credencial com que ele autentica. Em **Enriquecimento → Fontes**, clique em **Nova fonte**, escolha o enricher, preencha a configuração (o formulário sai do próprio schema do enricher) e informe a credencial.

```bash
curl -s -X POST "$HOST/api/collectors/enrichment/sources" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "vt-producao",
    "enricher": "virustotal",
    "secret": "sua-chave-de-api"
  }'
```

:::caution[A credencial sobe uma vez e não volta]
`secret` é write-only: o servidor cifra e guarda; a resposta traz apenas `secret_configured: true`. Isso não é conveniência, a referência cifrada **é** o segredo utilizável, e o cofre a decifra sem verificar de que organização ela veio. Se a API a devolvesse, um administrador poderia colá-la em outra organização e usar a credencial alheia. Para trocar a chave, envie um `secret` novo; para removê-la, envie `""`.
:::

Vale o mesmo para a configuração: campos com "secret" no nome são **recusados** no corpo de `config` (422). A credencial entra só pelo campo `secret`.

### Teste a fonte antes de escrever a regra

No formulário da fonte, o botão **Testar** consulta o serviço de verdade com a credencial gravada, em modo reduzido (uma página, poucos registros). Ele devolve o erro real do provedor: chave recusada, DNS que não resolve, certificado, schema GraphQL incompatível.

```bash
curl -s -X POST "$HOST/api/collectors/enrichment/sources/$SOURCE_ID/test" \
  -H "Authorization: Bearer $TOKEN"
```

Sem esse teste, o único sinal de credencial errada seria evento saindo sem contexto no destino, horas depois, com o erro enterrado no log do worker.

### Uma fonte para várias organizações (MSP)

Se a sua matriz atende clientes como organizações filhas, cadastre a credencial **uma vez** e escolha quais filhas a usam. No formulário aparece a lista de organizações filhas; na API, o campo é `shared_organization_ids`:

```bash
curl -s -X POST "$HOST/api/collectors/enrichment/sources" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "vt-msp",
    "enricher": "virustotal",
    "organization_id": 1,
    "secret": "sua-chave-de-api",
    "shared_organization_ids": [7, 12]
  }'
```

A lista é editável depois da criação: mande um `PATCH` com a lista nova, e as organizações que saírem perdem o acesso no ciclo seguinte. A organização dona nunca sai da lista.

Optamos por uma linha só, com lista de organizações, em vez de copiar a fonte para cada filha. Copiar obrigaria a rotacionar a mesma credencial em N lugares, e bastaria esquecer um para deixar um cliente chamando a API com chave revogada.

:::note[Compartilhar entre organizações é Enterprise]
Na edição Community cada fonte atende uma organização. Isso acompanha o escopo: ver a subárvore de organizações filhas também é Enterprise, então na Community não existe nem como escolhê-las. Uma tentativa de compartilhar responde **403**.
:::

### Depois: a regra cita a fonte pelo nome

```json
{
  "id": "vt",
  "enricher": "virustotal",
  "source": "vt-producao",
  "when": { "lacks_tag": ["asset_known"] },
  "key": { "source": "normalized.src_endpoint.ip", "kind": "ip" },
  "outputs": [
    { "from": "malicious", "target": "_centralops.enrichment.vt.malicious" }
  ]
}
```

Enricher que exige credencial **sem** `source` é recusado no commit com 422, em vez de virar uma política publicada que não faz nada.

O `when` acima não é decoração: no seam remoto ele decide **quais chaves saem** para o terceiro, não apenas o que é escrito no evento. Sem ele, o lote inteiro é consultado.

Duas diferenças importantes a considerar antes de usar uma fonte externa:

- **OpenCTI** roda **por evento** (mesma velocidade das tabelas próprias) e não envia nada para fora, a instância é sua.
- **VirusTotal** roda **por lote**, e a chave pública libera só **4 consultas por minuto**. Sem um `when` restritivo na regra (por exemplo, só consultar IPs que ainda não foram marcados como conhecidos por outra regra), a cota se esgota em segundos. Veja o aviso de egresso no card do catálogo antes de habilitar.

## Problemas comuns

### A política não faz nada depois de habilitada

Abra a aba **Execução** e olhe **Consultas** primeiro. O que você vê ali separa os dois casos:

| O que aparece | O que é | O que fazer |
|---|---|---|
| Nada registrado | Nenhum ciclo rodou com a política ativa, ou `ENRICHMENT_ENABLED` está desligada | Confira a flag mestra, se há integração coletando, e se a política está habilitada |
| Consulta com erro (`401`, DNS, timeout) | Credencial ou rede | Corrija a fonte na aba **Fontes** e use **Testar conexão** |
| Consulta OK, regra em "Não disparou" | O gate nunca passou, ou o campo da chave nunca existiu no evento | Revise **Quando aplicar** (a tag que ela espera pode nunca ser escrita, e um gate com campo ou lista vazia nunca casa) e confira **Origem da chave** contra um evento real no dry-run |
| Consulta OK, acerto em 0% | A chave existe mas não casa com o conteúdo da tabela | Compare o valor real do campo com as chaves publicadas na tabela |
| Badge "sem resposta" na regra | A consulta não respondeu: credencial, rede, orçamento ou cache remoto ausente | Veja o motivo em **Consultas**; o percentual de acerto está diluído e não indica problema de dado |

Se a aba estiver totalmente vazia: a política pode ter sido habilitada sem nenhuma versão publicada, ou a tabela referenciada por uma regra não existe. Confira com `GET /api/collectors/enrichment/policies/{id}`, se `current_version_id` estiver vazio, publique uma versão (passo 5); e se aparecer o selo **sem versão publicada** na aba **Tabelas**, volte ao passo 3.

### Regras sumiram depois que publiquei

Publicar substitui a lista inteira de regras da política, não faz merge. Se o editor tinha menos regras que a versão anterior, a versão nova ficou com menos regras.

Recupere pelo histórico: **Carregar no editor** na versão que estava certa, confira as regras e publique de novo. Nenhuma versão antiga é apagada, então nada foi perdido de verdade.

### A consulta funciona mas o acerto caiu

Consulta respondendo e acerto em queda é problema de **dado**, não de credencial: o formato do campo na origem provavelmente mudou. Rode o dry-run com um evento recente e compare o valor real do campo em **Origem da chave** com o que existe como chave na tabela.

### Publicar a versão da política dá 422 "tabela inexistente"

A regra referencia, em `table`, um nome que não existe **nesta organização**. Tabelas não são compartilhadas entre organizações, confira o nome exato na aba **Tabelas** ou via `GET /api/collectors/enrichment/tables`.

### Não consigo apagar uma tabela

Uma tabela referenciada por alguma política não pode ser apagada, apagar quebraria a regra em silêncio a cada ciclo. Remova a regra que a usa (ou desabilite a política) antes de apagar a tabela.

### `invalid_rows` maior que zero ao publicar uma versão de tabela CIDR

Alguma chave da tabela não é um IP nem uma faixa CIDR válida (ex.: um texto digitado errado). Essas linhas são descartadas, a linha exata não vem na resposta hoje, então revise o arquivo de origem por padrões óbvios (espaços, máscara faltando) e publique de novo.

## Próximos passos

- **Quer entender os conceitos antes de configurar?** Veja [O que é o enriquecimento](./overview.md).
- **Quer saber o que cada fonte do catálogo faz e exige?** Veja [Catálogo de fontes](./catalog.md).
