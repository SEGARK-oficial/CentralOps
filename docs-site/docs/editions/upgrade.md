---
sidebar_position: 2
title: Upgrade para Enterprise
description: Passo a passo para ativar a edição Enterprise — da assinatura ao produto rodando com os recursos MSSP, sem reinstalar.
---

# Upgrade para Enterprise

Ativar a edição Enterprise **não reinstala** o CentralOps. Você troca as imagens
Community pelas imagens Enterprise (o mesmo produto, com os módulos pagos compilados) e
fornece a sua **licença**. Sem licença válida, a mesma imagem roda como Community —
então o upgrade e o downgrade são reversíveis.

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

As tags EE seguem a versão do Core: `v1.0.0-ee` (acompanha a release) e
`v1.0.0-ee.<sha>` (imutável — **prefira esta em produção**).

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
CENTRALOPS_EE_IMAGE=ghcr.io/segark-oficial/centralops-ee:v1.0.0-ee
CENTRALOPS_WEB_EE_IMAGE=ghcr.io/segark-oficial/centralops-ee-frontend:v1.0.0-ee
```

E suba o CE + a overlay Enterprise (a partir da raiz do projeto):

```bash
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml pull
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml up -d
```

:::tip[Prefere ativar pela interface?]

Com o `key.prod.pem` montado (o `LICENSE_KEYS_DIR` acima), você pode deixar o
`CENTRALOPS_LICENSE_TOKEN` de fora e colar o token na tela
**Configurações → Licença** do produto, como administrador. A licença fica salva
(cifrada) no banco e sobrevive a reinícios.

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
  --set image.tag=v1.0.0-ee \
  --set frontendImage.repository=ghcr.io/segark-oficial/centralops-ee-frontend \
  --set frontendImage.tag=v1.0.0-ee \
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

- **`unknown key id: 'key.prod'`** — o `key.prod.pem` não está no keyring. Confira se o
  arquivo está no diretório do `LICENSE_KEYS_DIR` (Compose) ou no
  `secrets.licenseKeyring` (Helm) e reinicie: o keyring é lido no boot.
- **Token ausente/expirado** — confira o `CENTRALOPS_LICENSE_TOKEN` (ou reative pela
  tela de Licença) e a validade no portal.

## Downgrade

Volte às imagens Community (ou remova o `CENTRALOPS_LICENSE_TOKEN`) e suba de novo — a
plataforma cai para Community por design, sem perder dados.

## Precisa de ajuda?

Fale com **support@segark.com**.
