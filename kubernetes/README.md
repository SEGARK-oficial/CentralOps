# kubernetes/

Manifests de Kubernetes do CentralOps. O guia de deployment em Kubernetes está na
[documentação](https://docs.segark.com).

## Conteúdo

- [`helm/centralops/`](helm/centralops/) — Helm chart (api, frontend, workers
  Celery, NetworkPolicies, HPA, PDB). Ver o [README do chart](helm/centralops/README.md).

## Quando usar k8s vs. as outras opções

| Modo | Quando |
|---|---|
| Docker Compose (VM) | dev / single-host / produção em single-host — ver `docs/deployment/` |
| **Kubernetes (Helm)** | **produção em escala / multi-node** (este diretório) |

Ambos compartilham o **mesmo artefato Docker** e os mesmos entrypoints
(`scripts/start-api.sh`, `start-collector.sh`, `start-frontend.sh`).
