---
sidebar_position: 4
title: Atualizar de versão
description: "Passo a passo para atualizar o CentralOps de uma versão para a mais recente (ex.: 1.1.0 → 1.2.0) — mecânica genérica no Compose e no Helm, migração idempotente no boot, verificação e rollback — mais as notas da versão 1.2.0."
---

# Atualizar de versão

Subir de uma **versão** para a mais recente (ex.: `1.0.1` ou `1.1.0` → `1.2.0`) é uma
operação de rotina: você troca a **tag da imagem**, puxa a nova imagem e recria os
serviços. Não há reinstalação, não há passo manual de migração, e os **dados são
preservados**. Esta página cobre a mecânica genérica (vale para qualquer versão) e traz,
no fim, as **Notas da versão** com o que muda em cada release.

:::note[Isto é diferente de "Atualizar de edição"]

Esta página trata de subir de **versão** (ex.: `1.1.0` → `1.2.0`), dentro da mesma
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
| **Community** | `vX.Y.Z` — ex.: `v1.2.0` | `sha-<shortsha>` — ex.: `sha-a1b2c3d` | — |
| **Enterprise** | `vX.Y.Z-ee` — ex.: `v1.2.0-ee` | `vX.Y.Z-ee.<sha>` — ex.: `v1.2.0-ee.9f8e7d6` | `core-<coresha>` |

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
CENTRALOPS_EE_IMAGE=ghcr.io/segark-oficial/centralops-ee:v1.2.0-ee.9f8e7d6
CENTRALOPS_WEB_EE_IMAGE=ghcr.io/segark-oficial/centralops-ee-frontend:v1.2.0-ee.9f8e7d6
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
  --set image.tag=v1.2.0-ee.9f8e7d6 \
  --set frontendImage.repository=ghcr.io/segark-oficial/centralops-ee-frontend \
  --set frontendImage.tag=v1.2.0-ee.9f8e7d6 \
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

### 1.2.0

:::danger[Breaking: a superfície de Alertas foi REMOVIDA]

A área de **Alertas** foi **totalmente removida** nesta versão. Como a mudança entrou
como *refactor* (sem marcador de *breaking change*), **não aparece no changelog
automático** — este guia é o único aviso. O que sai:

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

**Metering de custo ligado por padrão.** O `COST_METERING_ENABLED` agora vem **`true`**
por padrão. Com isso, o card **"Redução de volume & custo"** passa a aparecer em
**Operação → Fluxo de dados**: no Community ele mostra volume, percentual e bytes
economizados; no Enterprise ele soma o valor em **US$** (a partir do `cost_per_gb`
configurado em cada destino). Para desligar, defina `COST_METERING_ENABLED=false`.

**Correções operacionais** (informativo — nada a fazer):

- Os coletores não entram mais em **crash-loop de RedBeat** (lock e limite de laço
  corrigidos).
- O **soft-timeout de coleta** não envenena mais o pool de conexões do banco.
- Um `SESSION_SECURE_COOKIE` **vazio** não derruba mais o boot.
- A **validação OCSF** volta a rodar na imagem compilada.

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
