---
sidebar_position: 3
title: Hierarquia de tenants e MSP
description: Modelo de tree, personas de admin (global, MSP/reseller, tenant), quotas de sub-tenants, auto-seed de admin
---

# Hierarquia de tenants e MSP

O CentralOps suporta uma **estrutura hierárquica de organizações** que permite a plataforma escalar de clientes diretos para resellers, MSPs (Managed Service Providers) e suas sub-redes de clientes. A hierarquia estabelece limites de visibilidade e controle: cada administrador — seja global, de reseller ou de tenant — enxerga e gerencia apenas o seu ramo, com segurança de dados absoluta entre ramos.

## Quando usar

- **Você é dono/operador da plataforma**: entenda como o modelo tree garante que os dados de um reseller nunca vazem para outro, e como conferir quotas de sub-tenants.
- **Você é administrador de um reseller/MSP**: saiba que domina a subárvore inteira dos seus clientes, como delegar admin para eles (auto-seed), e que clientes não conseguem se escalar além do próprio tenant.
- **Você precisa provisionar um novo cliente**: confirme se ele é subordinado a um reseller (admin-de-MSP cria dentro da subárvore) ou direto à plataforma (admin global cria na raiz).

## O modelo de árvore: plataforma, reseller, cliente

A hierarquia é uma **tree de organizações**. No topo fica a **raiz** — a plataforma ou o owner global. Abaixo dela, **resellers/MSPs** (nós intermediários), cada um com seus próprios **clientes** (nós folha ou filiais). Um cliente pode ser também um reseller se a plataforma grante a ele o programa de parceria (partner program).

```
              Plataforma (admin global)
                     |
         ____________|____________
        |            |            |
     Reseller A   Reseller B   Cliente Direto
        |            |
     _____|___        |
    |   |   |        |
   C1  C2  C3       C4
```

Cada nó nessa árvore é uma **organização**. Cada organização tem:

- **parent_organization_id**: quem está um nível acima (None para a raiz).
- **kind**: `customer` (padrão — nó folha sem sub-orgs) ou `reseller` (nó intermediário que pode ter filhos).
- **root_id**: a organização raiz da subárvore (denormalizado para rapidez).
- **depth**: quantos níveis abaixo da raiz (0 para a raiz mesma).

Os dados (eventos, mapeamentos, integrações) **nunca cruzam o limite organizacional** — um cliente de reseller A não enxerga dados de reseller B nem de nenhum outro ramo.

## As três personas de administrador

O acesso é definido pela **tripla: papel (role) × nó escopo × herança**. Existem três padrões ("personas"):

### Admin global (da plataforma)

- **Papel**: Admin
- **Escopo**: raiz da plataforma
- **Herança**: subtree (vê toda a árvore)
- **O que enxerga**: todas as organizações, todos os usuários, toda e qualquer configuração da plataforma.
- **O que faz**: criar/deletar organizações, conferir quotas de resellers, habilitar programa de parceria, criar e deletar qualquer usuário (com qualquer privilégio).

```
Admin global
├─ cria Reseller A (kind='reseller')
├─ cria Reseller B (kind='reseller')
├─ vê dados de todas as sub-orgs
└─ limita filhos de Reseller A a 10 (quota)
```

### Admin de MSP/reseller (admin-de-MSP)

- **Papel**: Admin
- **Escopo**: nó da organização reseller
- **Herança**: subtree (vê a subárvore inteira daquele reseller)
- **O que enxerga**: o reseller dele, todos os seus clientes (filhos), usuários dentro daquele ramo.
- **O que faz**: criar novos clientes (novos nós filhos), editar ou deletar os seus clientes, delegar admin para cada cliente (auto-seed de tenant-admin), gerenciar usuários dentro da subárvore (mas nunca elevar alguém a admin global).

```
Admin-de-Reseller A
├─ cria novo Cliente C1 (filho de Reseller A)
├─ edita dados da sub-orgs (C1, C2, C3…)
├─ delega admin-de-tenant para C1
└─ NÃO consegue:
   ├─ elevar C1 a global (sem permissão)
   ├─ acessar dados de Reseller B
   └─ deletar o próprio nó pai (Reseller A)
```

### Admin de tenant (admin-de-org)

- **Papel**: Admin
- **Escopo**: nó da organização cliente
- **Herança**: self (só aquele nó)
- **O que enxerga**: só dados e configurações do seu próprio tenant.
- **O que faz**: gerenciar integrações, mapeamentos, eventos, usuários — tudo dentro do próprio tenant. Não consegue criar sub-orgs nem escalar privilégios.

```
Admin-de-Cliente C1
├─ gerencia integrações de C1
├─ edita mapeamentos de C1
├─ convida usuários para C1
└─ NÃO consegue:
   ├─ criar sub-tenant
   ├─ acessar dados de outro cliente
   └─ fazer-se admin de Reseller A (escalonamento bloqueado)
```

### Bônus: analista SOC global

Internos da plataforma podem ter papel Operator/Engineer com herança subtree na raiz — ganham **visibilidade read-only sobre toda a plataforma** sem privilégios de escrever.

## Quota de sub-tenants de um reseller

Um reseller pode ter um **limite de quantas sub-organizações (filhos) ele pode criar**. Esse limite é definido no **partner program** associado a ele:

```python
PartnerProgram(
    reseller_org_id = Reseller A,
    max_child_orgs = 50,   # limite de 50 clientes
    enabled = True
)
```

Quando o admin-de-MSP (ou admin global por ele) tenta **criar um novo cliente** abaixo de Reseller A, o sistema checa:

- Conta quantos filhos diretos ele já tem (via closure table).
- Se a contagem chegou ao `max_child_orgs`, **rejeita a criação**.

Se `max_child_orgs = None`, o limite é **ilimitado** (reseller pode ter quantos clientes quiser).

**Fluxo de sincronização** (p.ex., sync Sophos): quando um novo tenant Sophos entra e precisa ser ligado a um reseller, o sistema checa a quota **antes** de materializar o nó. Se estourou, o evento é logado mas **não bloqueia** a sync inteira — ele é registrado em "erros da sync" para o reseller revisar manualmente.

## Auto-provisioning de admin para o tenant (opt-in)

Quando uma nova **sub-organização é criada** (por um reseller ou pelo admin global), a plataforma pode automaticamente **seed um usuário admin para aquele tenant** — criando uma conta pendente (ainda não ativada) com um email ou username determinístico.

### Como funciona

1. **Admin-de-MSP cria Cliente C1** (filho de Reseller A).
2. Se a configuração `PARTNER_AUTO_SEED_TENANT_ADMIN = True`, o sistema cria automaticamente:
   - Um usuário `AppUser` com papel **Admin**, escopo **só C1** (inherit=self), status **pendente** (is_active=False, sem senha).
   - Uma entrada `OrgRoleBinding` ligando esse usuário ao tenant-admin da org.

3. O cliente recebe um **convite/link de ativação** para definir a senha ou conectar via SSO/SCIM — **ele ativa a conta a seu critério**.

### Restrições de segurança

- O admin **auto-seeded é sempre tenant-local** (organization_id=C1, is_global=False) — nunca global.
- Conta pendente **não consegue fazer nada** até ser ativada.
- Auto-seed **só roda para tenants aprovados** no fluxo de onboarding (não para drafts).
- É **best-effort** — se falhar, não bloqueia o onboarding (erro é logado).

### Opt-in operacional

A flag `PARTNER_AUTO_SEED_TENANT_ADMIN` é a única chave de comportamento remanescente após implementação — default **False** (fail-safe: nunca seed silencioso).

```python
# Plataforma desativa auto-seed por padrão
PARTNER_AUTO_SEED_TENANT_ADMIN = False

# Reseller opta por auto-seed (tenant recebe convite)
PARTNER_AUTO_SEED_TENANT_ADMIN = True
```

Quando ativado, a experiência típica é:

1. Reseller cria novo cliente.
2. Cliente recebe email: "você foi criado no CentralOps, clique aqui para ativar sua conta".
3. Cliente define senha e entra como admin do tenant.
4. Reseller vê o novo cliente na sua lista, com admin já convidado.

Se desativado, o reseller (ou admin global) precisa **criar manualmente** o admin do tenant após a criação da org.

## Segurança: anti-escalonamento

O sistema bloqueia **amplificação de privilégios**:

- Admin-de-MSP (Reseller A) **não consegue** fazer-se admin global nem criar admin global — qualquer nova delegação fica limitada à subárvore de A.
- Admin-de-org (Cliente C1) **não consegue** se elevar a admin-de-MSP.
- Quando um admin delega outro, o sistema valida que o **alvo está dentro da subárvore** (anti-escape).
- Admin-de-org **não cria sub-organizações** — só admin-de-MSP e admin-global conseguem.

Essas restrições rodam no backend em **toda write-path** — não há bypass, brecha de UI ou race condition. A segurança é guarantida pelo modelo de dados (closure table) + validação no código.

## Resumo

| Pessoa | Papel | Escopo | Herança | Vê | Cria/Edita |
|--------|-------|--------|---------|----|----|
| **Admin global** | Admin | Raiz | Subtree | Tudo | Org, reseller, qualquer usuário |
| **Admin-de-MSP** | Admin | Nó reseller | Subtree | Subárvore do reseller | Clientes (filhos), usuários do ramo |
| **Admin-de-org** | Admin | Nó cliente | Self | Só seu tenant | Integrações, mapeamentos, usuários locais |

---

Para aprender como **gerenciar usuários e papéis** em cada nível, veja [RBAC (Controle de acesso)](../concepts/rbac.md). Para **criar ou modificar organizações**, veja [Organizações](./organizations.md).
