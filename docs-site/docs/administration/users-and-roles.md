---
sidebar_position: 1
title: Usuários e papéis
description: Criar contas, atribuir papéis, resetar senhas e desativar usuários pela interface
---

# Usuários e papéis

Crie contas, atribua papéis, resete senhas e desative usuários — tudo pela interface do CentralOps, na tela **Administração → Usuários**.

**Público**: apenas administradores da plataforma.

## Quando usar

- **Onboarding de novo analista**: um analista entrou no time de SOC e precisa de acesso ao CentralOps no primeiro dia, com o papel certo para o que vai fazer.
- **Offboarding imediato**: um colaborador saiu da empresa e você precisa cortar o acesso na hora — revogando sessões abertas e tokens de API ativos.
- **Ajuste de privilégio**: um operador provou competência em normalização e vai passar a editar mapeamentos; você precisa promover o papel dele de Operator para Engineer.

## Acessar a administração de usuários

Vá ao menu **Administração → Usuários**.

## A tela de usuários

A lista mostra uma tabela com:

| Coluna | Descrição |
|--------|-----------|
| Email | Endereço usado para login. |
| Papel | Viewer, Operator, Engineer ou Admin. |
| Organização | A organização à qual o usuário pertence. |
| Status | Ativo ou desativado. |
| Último login | Data e hora do último acesso, ou "nunca". |
| Ações | Editar, resetar senha, desativar. |

## Criar um novo usuário

### Passo 1: abrir o formulário de novo usuário

Na tela **Administração → Usuários**, clique no botão para adicionar um novo usuário.

### Passo 2: preencher os dados

Informe:

- **Email**: endereço de login do usuário (por exemplo, `nome@empresa.com`).
- **Papel**: escolha no seletor (Viewer, Operator, Engineer ou Admin).
- **Organização**: deixe a padrão ou selecione outra organização.

A plataforma gera automaticamente uma senha inicial aleatória. Ela é exibida uma única vez — anote ou copie se for compartilhá-la diretamente.

### Passo 3: criar e compartilhar o acesso

1. Confirme a criação do usuário.
2. A plataforma gera um **link de redefinição de senha**.
3. Copie esse link.
4. Compartilhe com o novo usuário pelo canal que preferir (e-mail, mensagem etc.).
5. Ao abrir o link, o usuário define a própria senha.

#### Quando o envio de e-mail está configurado

Se a plataforma estiver configurada para enviar e-mails, ela envia o link de redefinição automaticamente ao novo usuário — você não precisa compartilhar manualmente.

> A configuração de envio de e-mail é definida pela equipe de infraestrutura no momento do deploy. Se precisar habilitá-la ou alterá-la, fale com o administrador da plataforma.

## Alterar o papel de um usuário

1. Localize o usuário na lista (use o campo de busca por email no topo).
2. Abra as **ações** da linha do usuário e escolha editar o papel.
3. Selecione o novo papel: Viewer, Operator, Engineer ou Admin.
4. Salve.

**Como a mudança entra em vigor:**

- O novo papel passa a valer assim que o usuário fizer qualquer nova ação na plataforma.
- A sessão atual do usuário não é interrompida — ele não é deslogado. As novas permissões já se aplicam à próxima ação que ele realizar.

## Resetar a senha de um usuário

Use quando o usuário esquecer a senha ou precisar trocá-la.

1. Abra as **ações** da linha do usuário e escolha resetar a senha.
2. A plataforma gera um link de redefinição.
3. Copie o link e compartilhe com o usuário.
4. Ao abrir o link, o usuário define uma nova senha.

O link de redefinição tem validade limitada (por padrão, **1 hora**). Se expirar, basta gerar um novo pela mesma ação.

> O tempo de validade do link é definido pela equipe de infraestrutura no momento do deploy. Se precisar alterá-lo, fale com o administrador da plataforma.

## Desativar um usuário

Use quando alguém sair da empresa ou perder o direito de acesso.

1. Abra as **ações** da linha do usuário e escolha desativar.
2. Confirme na mensagem de aviso ("o usuário não poderá mais fazer login").

**O que acontece ao desativar:**

- As sessões ativas do usuário são encerradas imediatamente.
- O usuário não consegue mais fazer login.
- O histórico de auditoria das ações dele é preservado.
- Os **Tokens de API** do usuário também são revogados na hora — eles não continuam válidos após a desativação.

### Reativar um usuário

Se a pessoa voltar:

1. Abra as **ações** da linha do usuário e escolha ativar.
2. O usuário volta a conseguir fazer login.

## Papéis e permissões

Para a matriz completa de permissões, consulte [Papéis e permissões (RBAC)](../concepts/rbac.md).

Resumo:

| Papel | O que pode fazer |
|-------|------------------|
| **Viewer** | Lê eventos já entregues, mas não roda busca AO VIVO na fonte. Consulta alertas e investigações em leitura. |
| **Operator** | Roda query/hunt AO VIVO; descartar/reprocessar quarentena; pausa integrações. |
| **Engineer** | Tudo do Operator, mais: salva query, cria agendamentos e edita mapeamentos. |
| **Admin** | Tudo, mais: bloqueia IP/hash, cria destinos/roteamento, gerencia usuários e organizações. |

## Boas práticas

### Menor privilégio possível

- Comece um novo colaborador como **Viewer** ou **Operator**.
- Promova para **Engineer** depois que ele demonstrar domínio das tarefas de normalização.
- Reserve **Admin** apenas para quem é responsável por infraestrutura e compliance.

### Entrada e saída de pessoas

- **Entrada**: crie a conta no primeiro dia e compartilhe o link de redefinição de senha.
- **Saída**: desative a conta assim que a saída for confirmada — isso encerra as sessões e revoga os Tokens de API imediatamente.
- **Revisão periódica**: a cada 90 dias, por exemplo, verifique se há contas inativas que podem ser removidas.

### Senhas

- Um reset ocasional pela interface é suficiente para o dia a dia.
- As senhas são armazenadas com segurança pela plataforma.

## Contas via SSO (Microsoft Entra)

Se a plataforma estiver integrada ao [SSO com Microsoft Entra](./sso-entra.md), os usuários federados entram sem precisar de uma senha local.

**Características:**

- **Sem senha local**: a conta pode ser criada automaticamente no primeiro login do usuário, quando essa criação automática estiver habilitada.
- **Papel sincronizado a cada login**: o papel do usuário vem do Entra, que é a fonte da verdade — ele é reconciliado a cada acesso.
- **Acesso global a várias organizações**: administradores podem configurar contas SSO para terem acesso a múltiplas organizações (útil para um SOC interno que monitora vários clientes). Consulte a página de [SSO com Microsoft Entra](./sso-entra.md) para os detalhes.
- **Saída via Entra**: desativar o usuário no Entra bloqueia o login no CentralOps; desativar a conta no CentralOps revoga os Tokens de API e as sessões.

## Limitações

- **Sem delegação**: um Admin não pode delegar o gerenciamento de usuários a outro Admin.

## Próximos passos

- **Configurar login com Entra?** Veja [SSO com Microsoft Entra](./sso-entra.md).
- **Criar uma organização separada para um cliente?** Veja [Organizações](./organizations.md).
- **Gerenciar credenciais de integrações?** Veja [Integrações](./platform-config.md).
- **Auditar quem acessou o quê?** Veja [Histórico e auditoria](../operations/history-audit.md).
