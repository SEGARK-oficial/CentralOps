# Política de Segurança

>
> O modelo de suporte aqui descrito é **comunitário e sem SLA**: reportes de segurança são tratados em regime de melhor esforço e divulgação coordenada — não há garantia de tempo de resposta contratual.

O CentralOps leva segurança a sério. Esta política descreve **como reportar vulnerabilidades**, **o que está no escopo** e **como conduzimos a divulgação coordenada**. Ela vale para o **núcleo Community** deste repositório público (`SEGARK-oficial/CentralOps`).

---

## Como reportar uma vulnerabilidade

Se você acredita ter encontrado uma vulnerabilidade de segurança no **CentralOps**, reporte de forma privada e responsável. Existem dois canais privados — use **qualquer um**:

1. **GitHub Security Advisories (preferencial).** Acesse a aba **Security → Report a vulnerability** do repositório (*Privately report a vulnerability*). Isso abre um advisory privado visível apenas para você e os mantenedores.
2. **E-mail.** Envie para **support@segark.com** (assunto sugerido: `[SECURITY] <resumo curto>`).

> **NÃO** abra uma *issue* pública, *pull request*, *discussion* ou post em redes sociais para reportar vulnerabilidades. Reportes públicos expõem usuários antes da correção.

### O que incluir

Quanto mais detalhe, mais rápida a triagem:

- Descrição clara do problema e do **impacto potencial**;
- Passos para reproduzir / prova de conceito (PoC), quando viável;
- Versões afetadas e detalhes do ambiente (Docker Compose / Kubernetes, versão da imagem);
- Logs, capturas de tela ou amostras de requisição/resposta relevantes — **redija segredos** (tokens, chaves, dados de clientes) antes de enviar.

### Comunicação cifrada (opcional)

Se preferir comunicação cifrada por PGP, mencione isso no contato inicial e a chave pública será fornecida em resposta.

- Chave pública PGP: *(a publicar — solicite no primeiro contato)*

---

## O que esperar (sem SLA)

O suporte de segurança a esta edição Community é **comunitário e em regime de melhor esforço, sem SLA**. Ainda assim, nosso compromisso de boa-fé é:

1. **Confirmação de recebimento** do seu reporte em um prazo razoável;
2. **Triagem e avaliação** do impacto, mantendo você informado sobre o andamento;
3. **Correção** das vulnerabilidades confirmadas conforme severidade e esforço disponível;
4. **Crédito** ao pesquisador na divulgação, salvo se você preferir anonimato.

Perguntas de **uso, configuração e operação** (que não sejam vulnerabilidades) **não** devem usar este canal — leve-as para **GitHub Discussions**.

---

## Divulgação coordenada

Praticamos **divulgação coordenada (coordinated disclosure)**:

- Trabalhamos com você para entender, reproduzir e corrigir o problema **antes** de qualquer divulgação pública.
- A janela-alvo de divulgação é de **até 90 dias** a partir do reporte confirmado. Esse prazo pode ser **antecipado** (ex.: correção pronta e publicada) ou, em casos complexos, **estendido de comum acordo**.
- Após a correção, publicamos um **GitHub Security Advisory** (e, quando aplicável, solicitamos um CVE) com os créditos combinados.
- Pedimos que você **não divulgue publicamente** detalhes antes do término da janela coordenada ou da publicação do advisory — o que ocorrer primeiro.

---

## Escopo

### Dentro do escopo (núcleo Community — este repositório)

- A aplicação CentralOps, a imagem de contêiner e a **configuração padrão** do núcleo Community;
- Segurança da superfície de API: autenticação/autorização, **RBAC e fronteiras de organização (single-tenant)**, injeção, SSRF, exposição de dados, etc.;
- O pipeline base: ingestão, normalização, roteamento para destinos, detecção in-pipeline, redação de PII, segredos via KMS/Vault e ingestão por *push*;
- Questões de cadeia de suprimentos (*supply chain*) relacionadas aos artefatos de build do núcleo, quando aplicável.

Vulnerabilidades em **dependências de terceiros** devem ser reportadas aqui quando impactarem usuários do CentralOps de forma relevante — preferencialmente também ao mantenedor upstream.

### Fora do escopo deste repositório (tratado em canal privado)

O CentralOps adota um modelo **open-core**. Os componentes proprietários da **edição Enterprise** **não fazem parte deste repositório público** e **não devem ser reportados aqui**. Vulnerabilidades que envolvam esses componentes proprietários — por exemplo, hierarquia multi-tenant/reseller (MSSP), busca federada cross-org/assíncrona, auditoria/compliance cross-tenant ou HA/fleet — são tratadas em **canal privado**: use o e-mail **support@segark.com** indicando tratar-se de componente Enterprise.

> Por garantia do projeto (Charter C1), **nenhum código proprietário/EE é versionado neste repositório**; um *gate* automatizado de fronteira impede isso. Se você encontrar o que acredita ser código EE neste repositório público, trate como reporte de segurança/conformidade e use os canais privados acima.

---

## Versões suportadas

O projeto publica *releases* em **cadência fixa**. Correções de segurança são aplicadas, em regra, à **linha de release estável mais recente**. Versões antigas podem não receber *backports* de correções.

| Versão | Suportada |
| ------ | --------- |
| Release estável mais recente | ✅ |
| Versões anteriores | ⚠️ Melhor esforço; sem garantia de *backport* |
| Builds de desenvolvimento / `main` não lançada | ❌ |

Recomenda-se sempre rodar a versão estável mais recente para receber correções de segurança.

---

## Safe harbor (porto seguro para pesquisa de boa-fé)

Apoiamos pesquisa de segurança de boa-fé e divulgação responsável. Se você seguir esta política, conduzir pesquisa de boa-fé e **agir apenas contra ambientes que você controla**, não buscaremos ação contra você por essa pesquisa.

Por favor, **evite** ações que possam prejudicar usuários ou serviços, tais como:

- Exfiltração de dados além do mínimo necessário para demonstrar o problema;
- Testes de negação de serviço (DoS) contra ambientes de produção;
- Acesso, modificação ou destruição de dados de terceiros;
- Engenharia social, *phishing* ou ataque físico contra mantenedores ou usuários.

---

## Sobre licença e contribuições

Esta política não altera os termos de licença do projeto. A licença do núcleo Community é a **AGPLv3**, conforme o arquivo **`LICENSE`**. Contribuições seguem o **DCO 1.1** (*Signed-off-by*) — veja `CONTRIBUTING.md`.

---

*Obrigado por ajudar a manter o CentralOps e seus usuários seguros.*
