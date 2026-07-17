---
sidebar_position: 2
title: Upgrade para Enterprise
description: Passo a passo para ativar a edição Enterprise — da assinatura ao produto rodando com os recursos MSSP, sem reinstalar.
---

# Upgrade para Enterprise

Ativar a edição Enterprise **não reinstala** o CentralOps. Você troca as imagens
Community pelas imagens Enterprise (o mesmo produto, com os módulos pagos compilados) e
fornece a sua **licença**. Sem uma licença válida, os recursos Enterprise ficam
bloqueados (a imagem continua rodando como Community) — então o upgrade e o downgrade
são reversíveis.

:::note[Trocar de edição ≠ trocar de versão]

Esta página é sobre **mudar de edição** (Community → Enterprise). Para atualizar de uma
**versão** para a mais recente (ex.: `1.1.0` → `1.2.0`), veja
**[Atualizar de versão](../deployment/upgrading.md)**.

:::

## O que você vai precisar

A licença tem **dois arquivos**, e os dois são obrigatórios:

| Arquivo | O que é | Para onde vai |
|---|---|---|
| `segark-pipeline-license-<kid>.jwt` | O **token** assinado (EdDSA) da sua licença. | `CENTRALOPS_LICENSE_TOKEN` (ou a tela **Configurações → Licença**). |
| `key.prod.pem` | A chave **pública** que o produto usa para **verificar** o token offline. | Um diretório apontado por `CENTRALOPS_LICENSE_KEYS_DIR`. |

:::warning[Sem a chave pública, a licença não ativa]

O token sozinho **não basta**: sem o `key.prod.pem` no keyring, o produto não consegue
verificar a assinatura e responde `unknown key id: 'key.prod'` — ficando em Community
por segurança. Sempre baixe **os dois** arquivos.

:::

## Visão geral

```text
  Portal segark.com          Registry                    Rodar
 ┌───────────────────┐      ┌────────────────┐      ┌──────────────────────────┐
 │ página License:   │ ───▶ │ docker login → │ ───▶ │ subir imagens EE com     │
 │ token + key.pem   │      │ pull imagens EE│      │ token + keyring montado  │
 └───────────────────┘      └────────────────┘      └───────────┬──────────────┘
                                                                ▼
                                                    edition=enterprise ✅
```

## 1. Baixe a licença no portal

1. Assine um plano no portal (**segark.com**). A licença é emitida para a sua conta.
2. Entre no portal e abra a página **License**.
3. Baixe os dois arquivos:
   - **Download token (.jwt)** — o token assinado da licença.
   - **Download key (key.prod.pem)** — a chave pública do keyring.

A própria página mostra o resumo de ativação ("How to activate") com estes passos.

## 2. Autentique no registry

As imagens Enterprise são **privadas** no GitHub Container Registry:
`ghcr.io/segark-oficial/centralops-ee` (API/workers) e
`ghcr.io/segark-oficial/centralops-ee-frontend` (frontend). Use a credencial de pull
fornecida com a sua assinatura (bundle de instalação do portal, ou **support@segark.com**):

```bash
echo "<password da credencial>" | \
  docker login ghcr.io -u "<username da credencial>" --password-stdin
```

O token vai por stdin para não ficar no histórico do shell.

:::info[Segurança]

A credencial de pull só controla o **download** da imagem. A ativação real dos recursos
é a **licença**, verificada offline dentro do produto.

:::

## 3. Suba com as imagens Enterprise

As tags EE seguem a versão do Core: `vX.Y.Z-ee` acompanha a release (ex.: `v1.0.1-ee`)
e `vX.Y.Z-ee.<sha>` é imutável (ex.: `v1.0.1-ee.2e8917d` — **prefira esta em produção**).

### Docker Compose

Coloque a chave pública ao lado do compose e configure o `compose/.env`:

```bash
mkdir -p compose/license-keys
cp ~/Downloads/key.prod.pem compose/license-keys/
```

Em `compose/.env`, adicione:

```dotenv
CENTRALOPS_LICENSE_TOKEN=<conteúdo do arquivo .jwt>
LICENSE_KEYS_DIR=./license-keys
CENTRALOPS_EE_IMAGE=ghcr.io/segark-oficial/centralops-ee:v1.0.1-ee
CENTRALOPS_WEB_EE_IMAGE=ghcr.io/segark-oficial/centralops-ee-frontend:v1.0.1-ee
```

E suba o CE + a overlay Enterprise (a partir da raiz do projeto):

```bash
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml pull
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml up -d
```

:::warning[A overlay Enterprise não é permanente ("sticky")]

O Compose só aplica o `docker-compose.ee.yml` quando ele é passado com `-f` — e isso
vale para **todo comando futuro**. Um `docker compose up -d` (ou `pull`, ou qualquer
recriação) só com o arquivo base **rebaixa a stack para Community silenciosamente**:
a imagem volta a ser a CE, o mount do keyring some e a próxima ativação falha com
`unknown key id`. Para tornar a overlay permanente, defina no `compose/.env`:

```dotenv
COMPOSE_FILE=docker-compose.yml:docker-compose.ee.yml
```

Com isso, um simples `docker compose up -d` (rodado de dentro de `compose/`, sem `-f`)
já aplica a overlay, e os comandos de dia 2 não rebaixam a stack.

:::

:::tip[Prefere ativar pela interface?]

Com o `key.prod.pem` montado (o `LICENSE_KEYS_DIR` acima), você pode deixar o
`CENTRALOPS_LICENSE_TOKEN` de fora e colar o token na tela
**Configurações → Licença** do produto, como administrador. A licença fica salva
(cifrada) no banco e sobrevive a reinícios.

A tela de Licença também existe — e aceita o paste — numa stack Community subida **sem**
a overlay; nesse caso o keyring do container está vazio e a ativação falha exatamente
com `unknown key id: 'key.prod'`. Antes de colar, confirme que a chave está visível no
container:

```bash
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml \
  exec centralops ls /licensing
# key.prod.pem
```

:::

### Kubernetes (Helm)

Crie o secret de pull das imagens (o chart usa `ghcr-secret` por padrão) e faça o
upgrade apontando imagens, token e keyring:

```bash
kubectl -n centralops create secret docker-registry ghcr-secret \
  --docker-server=ghcr.io \
  --docker-username="<username da credencial>" \
  --docker-password="<password da credencial>"

helm upgrade centralops kubernetes/helm/centralops -n centralops \
  --set image.repository=ghcr.io/segark-oficial/centralops-ee \
  --set image.tag=v1.0.1-ee \
  --set frontendImage.repository=ghcr.io/segark-oficial/centralops-ee-frontend \
  --set frontendImage.tag=v1.0.1-ee \
  --set secrets.licenseToken="<conteúdo do arquivo .jwt>" \
  --set-file "secrets.licenseKeyring.key\.prod\.pem=./key.prod.pem" \
  -f values.override.yaml
```

O chart monta o keyring em todos os pods e define `CENTRALOPS_LICENSE_KEYS_DIR`
automaticamente. Para GitOps/ExternalSecrets, use `secrets.existingSecret` e
`secrets.existingLicenseKeyring` no lugar dos valores inline.

## 4. Verifique

No boot, a API loga a edição resolvida:

```bash
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml \
  logs centralops | grep edition=
# edition=enterprise plan=mssp features=3
```

Você também pode conferir na interface, em **Configurações → Licença** (mostra a
edição, o plano e as features ativas).

Se aparecer **`edition=community`**, a licença não foi encontrada ou não pôde ser
verificada:

- **`unknown key id: 'key.prod'`** — o keyring que o **container** enxerga está vazio
  ou não contém o `key.prod.pem`. Siga o passo a passo abaixo.
- **Token ausente/expirado** — confira o `CENTRALOPS_LICENSE_TOKEN` (ou reative pela
  tela de Licença) e a validade no portal.

Uma instalação Enterprise cuja licença **não cobre um recurso** (plano que não o
inclui, licença ausente ou expirada além da tolerância) recusa a ação correspondente
com o estado **`license_required`** — por exemplo, ao sincronizar os tenants de um
partner. Nesse caso a correção não é o keyring: confira o plano e a validade em
**Configurações → Licença** ou no portal.

### Corrigindo `unknown key id`

A causa dominante é o **keyring vazio dentro do container** — em geral porque a stack
foi subida (ou recriada) **sem a overlay Enterprise**. O `key.prod.pem` pode estar
perfeito no host e mesmo assim nunca chegar ao container. Diagnostique de dentro para
fora:

**1. Imagem e mount** — o container da API usa a imagem EE e tem o mount `/licensing`?

```bash
docker inspect --format '{{.Config.Image}} {{json .Mounts}}' \
  $(docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml ps -q centralops)
```

**2. O que o processo enxerga** — a variável e o diretório dentro do container:

```bash
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml \
  exec centralops sh -c 'echo $CENTRALOPS_LICENSE_KEYS_DIR; ls -la /licensing'
```

**3. Permissões** — a API roda como uid `10001`: o `.pem` precisa ser legível por ela
(arquivo `0644`, diretório `0755`). Um `key.prod.pem` com `0600 root:root` é ignorado
em silêncio.

**4. Logs do keyring** — o boot loga o que foi (ou não) carregado:

```bash
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml \
  logs centralops | grep -iE 'skipping|license keyring'
```

Se o mount ou a variável estiverem ausentes, **recrie** os containers com os dois `-f`
(`up -d`) — `docker compose restart` **não** aplica mounts nem variáveis de ambiente
novas. Com o keyring corrigido, cole o token de novo na tela de Licença **sem reiniciar
nada**: o keyring é relido a cada ativação (e a cada refresh periódico). No Helm,
confira o `secrets.licenseKeyring` e o mount `/licensing` nos pods.

## Downgrade

Volte às imagens Community (ou remova o `CENTRALOPS_LICENSE_TOKEN`) e suba de novo — a
plataforma cai para Community por design, sem perder dados.

## Precisa de ajuda?

Fale com **support@segark.com**.
