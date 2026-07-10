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

## Visão geral

```text
  Assinar          Bundle do portal            Rodar
 ┌────────┐       ┌──────────────────┐        ┌─────────────────────────┐
 │ portal │ ────▶ │ licença + keyring │ ────▶ │ docker login → pull EE  │
 │ segark │       │ + credencial pull │        │ + subir com a licença   │
 └────────┘       └──────────────────┘        └───────────┬─────────────┘
                                                          ▼
                                              edition=enterprise ✅
```

## 1. Assine e pegue o bundle

1. Assine um plano no portal (**segark.com**). Uma **licença** é emitida para a sua conta.
2. No portal, obtenha o **bundle de instalação** (`GET /api/portal/install/{license_id}`).
   Ele traz tudo que você precisa:
   - **`license_token`** — o JWT assinado (EdDSA) da sua licença.
   - **`keyring`** — a chave **pública** (`<kid>.pem`) para verificar a licença offline.
   - **`registry_credential`** — usuário + token para baixar as imagens Enterprise privadas.
   - **`images`** — as refs exatas das imagens Enterprise (fixe estas tags).

:::info Segurança
A credencial de pull só controla o **download** da imagem. A ativação real dos recursos é
a **licença**, verificada offline. Guarde o `license_token` e use sempre as duas — usuário
**e** token — do `registry_credential`.
:::

## 2. Autentique no registry

Use `username` e `password` do `registry_credential` do bundle (o token vai por stdin,
nunca no histórico do shell):

```bash
echo "<registry_credential.password>" | \
  docker login ghcr.io -u "<registry_credential.username>" --password-stdin
```

## 3. Suba com as imagens Enterprise

### Docker Compose

Salve a chave pública do keyring e exporte a licença + as refs de imagem:

```bash
mkdir -p license-keys
echo "<keyring[<kid>] do bundle>" > license-keys/<kid>.pem

export CENTRALOPS_LICENSE_TOKEN="<license_token do bundle>"
export LICENSE_KEYS_DIR=./license-keys
export CENTRALOPS_EE_IMAGE="<images.backend do bundle>"    # ex.: ghcr.io/segark-oficial/centralops-ee:v1.0.0
export CENTRALOPS_WEB_EE_IMAGE="<images.frontend do bundle>"

# sobe o CE + a overlay Enterprise (a partir da raiz do projeto)
docker compose -f compose/docker-compose.yml -f compose/docker-compose.ee.yml up -d
```

### Kubernetes (Helm)

Aponte as imagens para as refs Enterprise, monte o keyring público e passe a licença por
Secret:

```bash
kubectl -n centralops create secret generic centralops-license \
  --from-literal=CENTRALOPS_LICENSE_TOKEN="<license_token>" \
  --from-file=license-keys/<kid>.pem

helm upgrade centralops kubernetes/helm/centralops -n centralops \
  --set image.repository=ghcr.io/segark-oficial/centralops-ee \
  --set frontendImage.repository=ghcr.io/segark-oficial/centralops-ee-frontend \
  --set image.tag=v1.0.0 --set frontendImage.tag=v1.0.0 \
  -f values.override.yaml
```

## 4. Verifique

```bash
curl -fsS http://localhost:3000/readyz
docker compose logs api | grep edition
# edition=enterprise plan=mssp features=3
```

Se você vir **`edition=enterprise`**, o upgrade está completo — os recursos MSSP
(multi-tenancy hierárquica, reseller, busca federada) já estão ativos. Se aparecer
`edition=community`, a licença não foi encontrada ou é inválida: confira o
`CENTRALOPS_LICENSE_TOKEN` e se o `<kid>.pem` está no `LICENSE_KEYS_DIR`.

## Downgrade

Volte às imagens Community (ou remova o `CENTRALOPS_LICENSE_TOKEN`) e suba de novo — a
plataforma cai para Community por design, sem perder dados.

## Precisa de ajuda?

Fale com **support@segark.com**.
