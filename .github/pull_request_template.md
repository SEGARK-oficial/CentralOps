<!--
Obrigado por contribuir com o CentralOps (Community core)!

Este repositório é o núcleo Community e aberto do CentralOps. Contribuições
são aceitas sob o modelo DCO (Developer Certificate of Origin 1.1): cada commit
deve ser assinado com `git commit -s`, atestando que você tem o direito de
submeter o código sob a licença do projeto, conforme declarada no arquivo
LICENSE. Não usamos CLA nem cessão de copyright.

A licença do core é a AGPLv3 (veja o arquivo LICENSE).
-->

## Descrição

<!-- O que esta mudança faz e por quê. Seja conciso e objetivo. -->

## Issue ligada

<!-- Ex.: Closes #123 / Refs #123. Discussões de uso ficam em GitHub Discussions, não em Issues. -->

Closes #

## Tipo (Conventional Commit)

<!-- Marque o tipo predominante; o título do PR deve seguir Conventional Commits. -->

- [ ] `feat` — nova funcionalidade
- [ ] `fix` — correção de bug
- [ ] `docs` — documentação
- [ ] `refactor` — refatoração sem mudança de comportamento
- [ ] `test` — apenas testes
- [ ] `chore` / `ci` / `build` — manutenção, CI ou build
- [ ] `perf` — desempenho

## Checklist

- [ ] **DCO**: todos os commits estão assinados (`git commit -s`) — `Signed-off-by:` presente em cada commit.
- [ ] Título do PR e mensagens de commit seguem **Conventional Commits**.
- [ ] **Testes adicionados/atualizados e VERDES**:
  - Backend: `APP_ENV=test APP_MASTER_KEY=<>=32 chars> SESSION_SECURE_COOKIE=false python -m pytest backend/`
  - Frontend (se aplicável): `npm test` (vitest) em `frontend/`
- [ ] **Não adiciona código EE/proprietário ao core** — o boundary gate de open-core passa:
  - `python -m pytest backend/tests/test_open_core_boundary.py`
  - (mesmo gate roda em CI via `.github/workflows/openness-gate.yml`)
- [ ] **Docs/ADR atualizados** quando aplicável (comportamento, contrato de API, configuração ou decisão arquitetural).
- [ ] **Sem segredos/chaves** no diff (sem credenciais, tokens, chaves privadas ou `.env`).

---

<sub>Contribuições somente ao Community core. Código EE/proprietário **nunca** deve ser adicionado a este repositório — o boundary gate falha o PR se isso ocorrer. Itens proibidos incluem: pacote `centralops_ee`, workspace `web-ee`, integração de billing/Stripe, busca federada cross-org e imports de componentes EE no frontend (ex.: `@centralops/web-ee`, `web-ee/`, `PartnerTenantsPanel`, `AutoApprovePolicyModal`).</sub>
