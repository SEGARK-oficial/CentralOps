---
sidebar_position: 1
title: Edições — Community vs Enterprise
description: O que o CentralOps Community (open-source) já entrega e o que a edição Enterprise adiciona para MSSPs e operações em escala.
---

# Edições: Community vs Enterprise

O CentralOps segue um modelo **open-core**. A edição **Community** deste repositório é
open-source (AGPLv3), completa e usável em produção por conta própria. A edição
**Enterprise** adiciona os recursos que escalam com o **tamanho e a maturidade** da
organização — pensada para MSSPs e SOCs multi-tenant.

## O que está sempre livre (Community)

Os recursos de segurança de base ficam na edição Community:

- **Ingestão de todas as fontes** — Sophos, Microsoft Defender, Wazuh, NinjaOne e
  qualquer vendor via registry de plugins; syslog, APIs, S3, Kafka e push de edge.
- **Normalização OCSF** com a DSL versionada (CML), dry-run e rollback.
- **Roteamento para 14+ destinos** (SIEMs, data lakes) com quarentena e drift detection.
- **SSO / OIDC (Entra)** + **RBAC**.
- **Criptografia / KMS** e **redação de PII** no pipeline.
- **Auditoria append-only** de base e o **servidor MCP** para automação.

## O que a Enterprise adiciona

| Recurso | Por que é Enterprise |
|---|---|
| **Multi-tenancy hierárquica / reseller (MSSP)** | Hierarquia de tenants e programa de revenda — escala com o porte da operação. |
| **Busca federada cross-org / assíncrona** | Busca ativa que cruza organizações e fontes. |
| **Audit & compliance cross-tenant** | Auditoria tamper-evident (WORM), retenção longa, export assinado. |
| **HA / fleet** | Alta disponibilidade, multi-node e orquestração de frota. |

## Como o gate funciona

A separação é **verificável**:

- O artefato Community **nunca** contém o código Enterprise — os módulos pagos são
  distribuídos como uma **imagem separada, ativada por licença**.
- A licença é um **JWT assinado (EdDSA)** verificado **offline** contra um keyring
  **público** embutido no produto. Sem uma licença válida, o produto roda **fail-closed
  como Community** — nada quebra, os recursos Enterprise apenas ficam inativos.
- A licença tem **expiração curta** e uma **lista de revogação** offline.

## Pronto para a Enterprise?

Veja o passo a passo em **[Upgrade para Enterprise](./upgrade.md)** ou fale com
**support@segark.com**.
