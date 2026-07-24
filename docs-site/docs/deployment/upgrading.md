---
sidebar_position: 4
title: Atualizar de versão
description: "Passo a passo para atualizar o CentralOps de uma versão para a mais recente (ex.: 1.1.0 → 2.0.0) — mecânica genérica no Compose e no Helm, migração idempotente no boot, verificação e rollback — mais as notas da versão 2.0.0, que traz uma mudança que quebra compatibilidade."
---

# Atualizar de versão

Subir de uma **versão** para a mais recente (ex.: `1.1.0` → `2.0.0`) é, na mecânica, uma
operação de rotina: você troca a **tag da imagem**, puxa a nova imagem e recria os
serviços. Não há reinstalação, não há passo manual de migração, e os **dados são
preservados**. Esta página cobre a mecânica genérica (vale para qualquer versão) e traz,
no fim, as **Notas da versão** com o que muda em cada release.

:::danger[2.0.0 é um major com *breaking change*]

A **2.0.0** sobe o número **maior** de propósito: ela **remove a superfície de Alertas**
(rota `/alerts`, endpoints de alerts da API, o Accept `v1` de `/dashboard/summary` e a
ferramenta MCP `list_integration_alerts`). **Os dados e o schema são preservados** — o que
muda é o **contrato de leitura**. Se você tem bookmarks, automações ou integrações que
batem nesses caminhos, **migre-as antes de atualizar** (detalhes em
[Notas da versão → 2.0.0](#200)). A **ingestão** de alertas Sophos/Wazuh **não muda**.

:::

:::note[Isto é diferente de "Atualizar de edição"]

Esta página trata de subir de **versão** (ex.: `1.1.0` → `2.0.0`), dentro da mesma
edição. Para trocar de **edição** — Community → Enterprise, ativando os módulos MSSP com
a sua licença — veja **[Upgrade para Enterprise](../editions/upgrade.md)**. Os dois
processos são independentes: você atualiza a versão de uma stack Community ou Enterprise
exatamente da mesma forma.

:::

## Antes de começar

- **Faça backup do banco.** Um `pg_dump` rápido antes de qualquer upgrade
  (`docker compose -f compose/docker-compose.yml exec postgres pg_dump -U centralops centralops > backup.sql`).
- **Leia as [Notas da versão](#notas-da-versão)** de destino — em especial as mudanças
  que quebram compatibilidade (*breaking changes*).
- **Fixe uma tag imutável** (com `sha`) em produção, para saber exatamente o que está
  rodando e para um rollback confiável.

## Como as versões são identificadas

As imagens de cada release recebem duas tags — uma **móvel** (acompanha a versão) e uma
**imutável** (nunca muda de conteúdo). Em produção, **fixe a imutável**.

| Edição | Tag de release (móvel) | Tag imutável (fixe em produção) | Tag extra |
|---|---|---|---|
| **Community** | `vX.Y.Z` — ex.: `v2.0.0` | `sha-<shortsha>` — ex.: `sha-a1b2c3d` | — |
| **Enterprise** | `vX.Y.Z-ee` — ex.: `v2.0.0-ee` | `vX.Y.Z-ee.<sha>` — ex.: `v2.0.0-ee.9f8e7d6` | `core-<coresha>` |

- A **tag de release** é ótima para acompanhar a versão, mas pode ser re-publicada — ruim
  para reprodutibilidade.
- A **tag imutável** (com `<sha>`) é a mesma imagem para sempre — **use-a em produção** e
  guarde a tag da versão anterior para o rollback.
- Na **Community**, a imutável é a `sha-<shortsha>` (ex.: `sha-a1b2c3d`). Se preferir não
  fixar por commit, acompanhe a tag de release móvel `vX.Y.Z` ou mantenha uma tag estável
  própria (ex.: `production`).
- As imagens **Enterprise são privadas** no GHCR e exigem `docker login` com a credencial
  de pull da sua assinatura — ver **[Upgrade para Enterprise](../editions/upgrade.md)**.

## Docker Compose

### Community

Em `compose/.env`, aponte para a nova tag:

```dotenv
IMAGE_NAME=ghcr.io/segark-oficial/centralops
IMAGE_TAG=sha-a1b2c3d   # a tag imutável da nova versão
```

Puxe as imagens e recrie os serviços (a partir da raiz do repositório):

```bash
docker compose -f compose/docker-compose.yml pull
docker compose -f compose/docker-compose.yml up -d
```

Não há build local — as imagens já vêm prontas do registry.

### Enterprise

Numa stack Enterprise, troque as **duas** imagens EE em `compose/.env`:

```dotenv
CENTRALOPS_EE_IMAGE=ghcr.io/segark-oficial/centralops-ee:v2.0.0-ee.9f8e7d6
CENTRALOPS_WEB_EE_IMAGE=ghcr.io/segark-oficial/centralops-ee-frontend:v2.0.0-ee.9f8e7d6
```

E recrie **sempre com os dois arquivos** (`-f` base + `-f` overlay EE):

```bash
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml pull
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml up -d
```

:::warning[Enterprise: inclua a overlay em TODOS os comandos]

Um `up -d`/`pull` só com o arquivo base **rebaixa a stack para Community
silenciosamente** (a imagem volta a ser a CE e o mount do keyring da licença some). Se
ainda não fez, torne a overlay permanente com
`COMPOSE_FILE=docker-compose.yml:docker-compose.ee.yml` no `compose/.env`. A mecânica
completa está em **[Upgrade para Enterprise](../editions/upgrade.md)**.

:::

## Kubernetes (Helm)

Faça um `helm upgrade` trocando só a **tag** da imagem.

**Community:**

```bash
helm upgrade centralops kubernetes/helm/centralops -n centralops \
  --set image.tag=sha-a1b2c3d \
  --set frontendImage.tag=sha-a1b2c3d \
  --reuse-values
```

**Enterprise** — mantenha também os repositórios EE:

```bash
helm upgrade centralops kubernetes/helm/centralops -n centralops \
  --set image.repository=ghcr.io/segark-oficial/centralops-ee \
  --set image.tag=v2.0.0-ee.9f8e7d6 \
  --set frontendImage.repository=ghcr.io/segark-oficial/centralops-ee-frontend \
  --set frontendImage.tag=v2.0.0-ee.9f8e7d6 \
  --reuse-values
```

O Helm faz um rollout gradual — a API, o frontend e os workers são Deployments
separados. Acompanhe:

```bash
kubectl -n centralops rollout status deploy/centralops-api
```

:::tip[Se você versiona os valores num arquivo]

Prefira editar `image.tag`/`frontendImage.tag` no seu `values.override.yaml` e rodar
`helm upgrade centralops kubernetes/helm/centralops -n centralops -f values.override.yaml`
— assim o estado desejado fica no Git, em vez de `--set` na linha de comando. Os `-set`
exatos para uma stack Enterprise estão em
**[Upgrade para Enterprise](../editions/upgrade.md)**.

:::

## O que acontece com os dados

No primeiro boot da nova versão, a API roda uma **migração/seed leve e idempotente** —
**não há passo manual de Alembic** neste release. Os **dados são preservados**:

- As definições de mapping **existentes** (que já têm uma versão ativa) **não são
  sobrescritas**.
- Só definições **vazias** ganham uma `v1` — é assim que os **novos defaults** de um
  release aparecem (ver [Notas da versão](#notas-da-versão)).
- Rodar a migração de novo (num restart) **não muda nada** — ela é idempotente.

Como o seed é **aditivo e não-destrutivo**, o rollback para a versão anterior é seguro
(ver abaixo). Se, logo após o upgrade, a plataforma parecer indisponível por um instante
enquanto os serviços sobem, aguarde alguns segundos — a nova versão leva um curto período
para ficar totalmente pronta. Persistindo, veja o runbook
**[A plataforma não está respondendo?](../runbooks/migration-and-boot.md)**.

## Verifique

1. **Edição e boot** — o log de boot mostra a edição resolvida (e confirma que a API
   subiu na versão nova):

   ```bash
   docker compose -f compose/docker-compose.yml logs centralops | grep edition=
   # edition=community        (ou "edition=enterprise plan=... features=..." numa stack EE)
   ```

2. **Saúde geral** — abra **Operação → Fluxo de dados** (`/flow`) e **Normalização →
   Saúde do Pipeline** (`/pipeline-health`) e confirme que os eventos continuam fluindo
   normalmente.

:::note[Como NÃO verificar versão/edição]

O `/readyz` só reporta **prontidão** (db/redis) — **não** a edição nem a versão. O
endpoint `/api/edition` existe, mas **exige autenticação**. Para a edição, use o log de
boot (`edition=`) ou a tela **Configurações → Licença**.

:::

## Rollback

Como este release **não tem migração destrutiva**, voltar é seguro:

- **Compose:** re-aponte a **tag imutável anterior** no `compose/.env` e rode `pull` +
  `up -d` (com os dois `-f` numa stack Enterprise).
- **Helm:** `helm rollback centralops` (volta à revisão anterior) ou
  `helm upgrade ... --set image.tag=<tag-anterior>`.

Os dados gravados pela versão nova continuam legíveis pela anterior — as mudanças de
schema são aditivas.

## Notas da versão

Cada versão adiciona uma seção aqui. Leia a da sua versão de destino **antes** de
atualizar.

### 2.0.0

A **2.0.0** é um **major**: ela remove a superfície de Alertas — por isso o salto de
`1.x` para `2.0`. É a **única** mudança que quebra compatibilidade; o resto são features,
correções e melhorias de performance (sem ação necessária).

:::danger[Breaking: a superfície de Alertas foi REMOVIDA]

A área de **Alertas** foi **totalmente removida** nesta versão. A mudança **está** no
changelog automático (marcada como `⚠ BREAKING CHANGE`) — é o que fez o release virar
`2.0.0`. O que sai:

- A rota **`/alerts`** deixa de existir (bookmarks antigos → **404**).
- Os **endpoints de alerts da API** foram removidos.
- O caminho **Accept v1** do `GET /dashboard/summary`
  (`application/vnd.centralops.v1+json`) foi removido.
- A ferramenta **MCP `list_integration_alerts`** foi removida.

**O que fazer:** a triagem agora é vendor-neutra, por **Operação → Investigações /
Busca federada** e **Detecções**. Se você tem automações ou integrações batendo nos
endpoints de alerts (ou no Accept v1 do `/dashboard/summary`), **migre-as** para esses
caminhos antes de atualizar.

A **ingestão** de `sophos.alert` (o dado que entra no pipeline) **não muda** — só a
superfície de leitura de "alertas" saiu.

:::

**Novidades (nada a configurar — já vêm ligadas):**

- **Exportação CSV robusta da Busca federada**, com rótulos localizados (PT/EN/ES) — em
  **Operação → Investigações**.
- **Mapa de fluxo `/flow` que escala.** O **[Fluxo de dados](../operations/fluxo-de-dados.md)**
  agrupa colunas densas num nó **"+N"** expansível e cabe sozinho na tela (fit-to-view),
  com realce de caminho ao passar o mouse — legível mesmo com dezenas de fontes/rotas/destinos.
- **Rótulos de condição de rota legíveis.** No editor de rotas, os operadores de condição
  aparecem com nomes humanos e localizados (em vez do rótulo cru).
- **Validação de mapping de detecção do Wazuh** + correção de uma definição de seed faltante.

**Metering de custo ligado por padrão.** O `COST_METERING_ENABLED` agora vem **`true`**
por padrão. Com isso, o card **"Redução de volume & custo"** passa a aparecer em
**Operação → [Fluxo de dados](../operations/fluxo-de-dados.md)**: no Community ele mostra volume, percentual e bytes
economizados; no Enterprise ele soma o valor em **US$** (a partir do `cost_per_gb`
configurado em cada destino). Para desligar, defina `COST_METERING_ENABLED=false`.

**Redação de PII ligada por padrão.** O `PII_REDACTION_ENABLED` agora vem **`true`**.
Sem regra de mascaramento configurada numa rota, **nada muda** — a entrega segue idêntica.
A diferença aparece onde existe regra de mascaramento — e o alcance é maior do que
parece. Com a flag desligada, **uma única rota** com mascaramento derrubava o
carregamento de **todas as rotas daquela organização**: o tráfego inteiro dela caía no
destino padrão (ou na fila de reenvio, se não houvesse). É *fail-closed* — nunca houve
entrega em claro, mas houve perda de roteamento silenciosa. Ao subir esta versão, o
roteamento daquela organização volta a valer por completo, e as rotas com mascaramento
passam a entregar ao destino real com os campos mascarados. Antes de subir, confira
quais rotas têm mascaramento configurado e confirme que a entrega ao destino real é o
desejado — e não se surpreenda se destinos que estavam "sem tráfego" voltarem a receber.

**Descartar o evento bruto por rota.** As regras de roteamento ganharam a opção
**Descartar o evento bruto**, que remove o payload original do fornecedor da entrega
daquela rota preservando o evento OCSF. Vem **desligada**. É a maior economia isolada
para um SIEM cobrado por volume — mantenha desligada na rota do data lake. Veja
[Roteamento](../outputs/routing.md).

**Poda do payload bruto nos mapeamentos padrão.** Os mapeamentos de fábrica passaram a
remover campos nulos e, no caso do Sophos Detection, as subárvores de `rawData` que já
foram extraídas para o evento normalizado. Se você depende do payload original íntegro
para perícia, revise o bloco `raw_reduction` do mapeamento antes de subir. Veja
[Especificação da DSL](../normalization/dsl-spec.md).

:::warning[Se você editou um mapeamento pela interface antes desta versão]
Havia um defeito em que **salvar** um mapeamento pela interface apagava silenciosamente o
bloco `raw_reduction` dele. Se você editou algum mapeamento e notou o payload crescer,
verifique se a poda ainda está lá — ela pode ter sido perdida. O defeito está corrigido:
o bloco agora sobrevive a qualquer edição.
:::

**Chave de supressão passou a ser validada.** A chave de supressão de uma rota agora só
aceita as mesmas características usadas na condição (fornecedor, severidade, tipo de
evento…). Campos do log como `src_ip` são **recusados com erro** — antes eram aceitos em
silêncio e faziam todo o tráfego cair numa assinatura só, descartando praticamente tudo.
Se alguma rota sua tem chave de supressão configurada, revise-a. Veja
[Roteamento](../outputs/routing.md).

**Correções operacionais** (informativo — nada a fazer):

- A **latência média por destino** passou a ter dados, pela primeira vez. A série nunca chegava a ser
  gravada — o valor registrado era sempre zero, e zeros são descartados —, então o cartão
  em **Operação → Destinos** ficava permanentemente vazio, em qualquer instalação. Agora
  ele mostra o tempo real de **entrega do lote** ao destino (todos os pedaços e as novas
  tentativas), em **segundos**. O **histórico anterior à atualização continua vazio** —
  isso não é defeito: só existem pontos a partir do momento em que você sobe esta versão.
- Os coletores não entram mais em **crash-loop de RedBeat** (lock, limite de laço e
  registro idempotente do scheduler corrigidos). Ver também
  **[Observabilidade](../operations/observability.md)** para acompanhar a saúde do Beat.
- O **soft-timeout de coleta** não envenena mais o pool de conexões do banco
  (dispose do pool + inicialização adiantada evitam `UnboundLocalError`).
- Um `SESSION_SECURE_COOKIE` **vazio** não derruba mais o boot; o ancoramento de caminho
  do recurso OCSF foi corrigido.
- IDs de **service account (shim)** são sanitizados — sem mais violação de FK em
  auditoria/mapping.
- A **validação OCSF** volta a rodar na imagem compilada.

**Performance:** a medição de volume da ingestão passou a ser **em lote**
(`InVolumeAccumulator`), reduzindo a latência de I/O no Redis do hot-path.

**Novos defaults de mapping.** Esta versão seeda definições padrão para **Wazuh** e para
**CrowdStrike, Entra ID, Okta e CloudTrail**. Elas só preenchem **definições vazias** —
mappings que você já customizou não são tocados (ver
[O que acontece com os dados](#o-que-acontece-com-os-dados)).

## Próximos passos

- **[Upgrade para Enterprise](../editions/upgrade.md)** — trocar de **edição**
  (Community → Enterprise), não de versão.
- **[Deploy com Docker Compose](./docker-compose.md)** — operação da stack single-host.
- **[Deploy com Kubernetes (Helm)](./kubernetes.md)** — rollout, HPA e rollback no cluster.
- **[Configuração](./configuration.md)** — todas as variáveis de ambiente.
