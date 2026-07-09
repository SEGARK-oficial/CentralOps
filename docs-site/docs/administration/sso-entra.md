---
sidebar_position: 5
title: SSO com Microsoft Entra ID
description: Login com a conta corporativa Microsoft (Entra ID), papéis automáticos e provisionamento de usuários
---

# SSO com Microsoft Entra ID

O CentralOps permite que sua equipe entre na plataforma com a **conta corporativa Microsoft** (Microsoft Entra ID), clicando em **"Entrar com Microsoft"** na tela de login. O papel de cada pessoa (Admin, Engineer, Operator ou Viewer) pode ser definido automaticamente a partir do Entra, e a conta no CentralOps pode ser criada no primeiro acesso.

Tudo é configurado por um administrador em duas etapas: primeiro um registro do lado da Microsoft (no Entra), depois o preenchimento de uma tela de configuração dentro do CentralOps.

## Quando usar

- **Centralizar o acesso do SOC na identidade corporativa.** Em vez de criar e gerenciar usuário/senha manualmente para cada analista, a equipe entra com a mesma conta Microsoft que já usa no dia a dia, com MFA e políticas do Entra aplicadas.
- **Conceder o papel certo automaticamente.** Ao atribuir analistas a papéis no Entra, o papel correspondente no CentralOps é aplicado a cada login — sem ajuste manual de permissões na plataforma.
- **Desligar o acesso junto com o offboarding.** Quando alguém sai da empresa e perde o acesso no Entra, é possível ativar a sincronização para que a conta seja desativada também no CentralOps, encerrando sessões e tokens.

## Visão geral do que você vai fazer

1. **No portal do Entra (lado Microsoft):** registrar o CentralOps como aplicativo e definir os papéis (App Roles).
2. **No CentralOps (pela interface):** preencher as credenciais e as regras de papéis em **Administração → Configurações**, aba **Identidade & SSO**.

---

## Passo 1 — Registrar o CentralOps no Entra

Você precisa de acesso de administrador no Entra para esta etapa.

1. No **Entra admin center**, vá em **App registrations → New registration**.
2. Dê um nome (ex.: `CentralOps`) e escolha **Accounts in this organizational directory only** (single-tenant).
3. Em **Redirect URI**, escolha a plataforma **Web** e informe o endereço de retorno do CentralOps. Ele termina em `/api/auth/sso/callback` e precisa ser **HTTPS** — por exemplo `https://centralops.suaempresa.com/api/auth/sso/callback`. Esse endereço precisa ser exatamente o mesmo que você vai preencher no CentralOps (campo **Redirect URI** do Passo 2).
4. Clique em **Register** e anote o **Application (client) ID** e o **Directory (tenant) ID** — você vai colá-los no CentralOps.
5. Vá em **Certificates & secrets → New client secret**, gere o segredo e **copie o valor imediatamente** (ele só aparece uma vez).

## Passo 2 — Definir os papéis no Entra (App Roles)

Os papéis controlam o que cada pessoa pode fazer no CentralOps. O recomendado é criar **App Roles** no Entra (em vez de grupos), pois eles chegam de forma limpa e legível no login.

1. No registro do aplicativo, vá em **App roles → Create app role** e crie um papel para cada nível de acesso. Sugestão de valores:

   | Nome de exibição | Value (usado no mapeamento) |
   |---|---|
   | CentralOps Admin | `CentralOpsAdmin` |
   | CentralOps Engineer | `CentralOpsEngineer` |
   | CentralOps Operator | `CentralOpsOperator` |
   | CentralOps Viewer | `CentralOpsViewer` |

2. Em **Enterprise applications → CentralOps → Users and groups**, atribua cada analista (ou um grupo) ao papel adequado. Quem não receber nenhum App Role entra com o **papel padrão** que você definir no CentralOps.

Guarde os valores da coluna **Value** — você vai usá-los no mapa de papéis do Passo 3.

## Passo 3 — Configurar o CentralOps pela interface

Como administrador, acesse o menu **Administração → Configurações** e abra a aba **Identidade & SSO**. Preencha os campos abaixo e clique em **Salvar configuração**. As alterações passam a valer no próximo login — não é preciso reiniciar nada.

### Campos da tela

| Campo na tela | O que informar |
|---|---|
| **Habilitar login via Microsoft Entra** | Marque para mostrar o botão "Entrar com Microsoft" na tela de login. |
| **Tenant ID (Directory)** | O Directory (tenant) ID anotado no Passo 1. |
| **Client ID (Application)** | O Application (client) ID anotado no Passo 1. |
| **Client secret** | O valor do segredo gerado no Passo 1. É guardado de forma cifrada e nunca exibido de volta — deixe em branco depois para manter o atual. |
| **Redirect URI** | O mesmo endereço de retorno cadastrado no Passo 1 (HTTPS). |
| **Papel padrão** | Papel aplicado quando a pessoa não tem nenhum App Role correspondente (ex.: Viewer). |
| **Domínios de e-mail permitidos** | Lista de domínios separados por vírgula que podem entrar. Deixe em branco para aceitar qualquer domínio do tenant. |
| **Mapa de App Roles → papel local** | Liga cada **Value** do Entra (Passo 2) a um papel do CentralOps. Veja o exemplo abaixo. |
| **Escopo global por padrão** | Marque para que contas criadas via SSO enxerguem **todas as organizações** — caso típico de um SOC interno que monitora todos os clientes. |
| **Provisionamento JIT** | Marque para **criar a conta no primeiro login**. Se desmarcar, a conta precisa já existir antes (veja "Sincronização de usuários"). |
| **Rótulo do botão** | Texto exibido no botão da tela de login (padrão: "Entrar com Microsoft"). |
| **Redirect pós-login** | Para onde a pessoa é levada após entrar (padrão: a página inicial). |

Os campos **Authority** e **Scopes** já vêm preenchidos com valores padrão. Só troque o **Authority** em nuvens soberanas da Microsoft (US Gov / China); caso contrário, deixe como está.

### Exemplo de mapa de papéis

No campo **Mapa de App Roles → papel local**, informe um objeto que liga cada valor do Entra a um papel do CentralOps:

```
{
  "CentralOpsAdmin": "admin",
  "CentralOpsEngineer": "engineer",
  "CentralOpsOperator": "operator",
  "CentralOpsViewer": "viewer"
}
```

### Validar e ativar

1. Clique em **Testar conexão** para verificar se as credenciais estão corretas.
2. Clique em **Salvar configuração**.
3. A partir daí, a tela de login passa a exibir o botão **"Entrar com Microsoft"**.

## Como os papéis são aplicados a cada login

- Quando o **Provisionamento JIT** está ligado, a conta é criada automaticamente no primeiro acesso, sem senha — a pessoa sempre entra pela Microsoft.
- A cada login, e-mail, nome e papel são **atualizados a partir do Entra** — o Entra é a fonte da verdade. Se você reorganiza papéis lá, a mudança reflete no próximo acesso.
- Se uma pessoa tiver mais de um App Role, vale o de **maior privilégio**.
- O **escopo global** dá visão de todas as organizações. Ele é sempre concedido a contas Admin e, se você marcar **Escopo global por padrão**, também às demais contas SSO. Para entender os papéis em detalhe, veja [Usuários e papéis](./users-and-roles.md).

## Sincronização de usuários com o Entra (opcional)

Na mesma tela **Identidade & SSO**, a seção **Sincronização de Usuários (Graph)** permite manter a lista de usuários do CentralOps alinhada com quem está atribuído ao aplicativo no Entra, sem depender só do login.

| Opção | O que faz |
|---|---|
| **Sincronização ativa** | Liga a sincronização periódica de usuários atribuídos ao aplicativo no Entra. |
| **Desprovisionamento automático** | Quando alguém deixa de estar atribuído ao aplicativo no Entra, a conta é **desativada** no CentralOps e suas sessões e tokens são revogados — útil para offboarding automático. |

Use essa seção quando quiser que entradas e saídas de pessoas no Entra reflitam na plataforma automaticamente, em vez de depender do próximo login de cada uma.

## Segurança e acesso de emergência

- **Login local continua funcionando.** Mesmo com o SSO ativo, ainda é possível entrar com usuário e senha. **Mantenha pelo menos um administrador local** para não ficar trancado fora caso o Entra fique indisponível (acesso "break-glass").
- **Offboarding imediato.** Desativar uma conta na tela **Administração → Usuários** revoga na hora as sessões e os tokens de API daquela pessoa — eles não sobrevivem à desativação.
- **Conflito de e-mail.** Se o e-mail vindo do Entra já pertence a outra conta, o login é recusado em vez de vincular silenciosamente as contas. Nesse caso, resolva a conta duplicada manualmente em **Administração → Usuários**.

## Solução de problemas

Quando algo dá errado, o erro aparece de volta na tela de login. Use a tabela abaixo para identificar a causa e onde corrigir — quase tudo se resolve em **Administração → Configurações → Identidade & SSO**.

| O que aconteceu | Provável causa | O que fazer |
|---|---|---|
| O botão "Entrar com Microsoft" não aparece, ou o login não inicia | SSO desligado ou incompleto | Confira se **Habilitar login via Microsoft Entra** está marcado e se Tenant ID, Client ID, Client secret e Redirect URI estão preenchidos. |
| O login falha ao contatar a Microsoft | A plataforma não está conseguindo alcançar o Entra | Problema de conectividade de saída da plataforma. Fale com o administrador da plataforma. |
| A Microsoft recusa o acesso da pessoa | A pessoa não está atribuída ao aplicativo no Entra | No Entra, confirme que o usuário (ou grupo) está atribuído ao aplicativo e a um App Role. |
| O login expirou no meio do caminho | O fluxo demorou demais ou o navegador perdeu a sessão | Peça para tentar novamente. |
| O login é recusado por validação | Tenant, Client ID ou Redirect URI não conferem | Reveja **Tenant ID**, **Client ID** e **Redirect URI** na tela de configuração — o Redirect URI precisa ser idêntico ao do Entra. |
| "Domínio não permitido" | O e-mail está fora da lista de domínios | Ajuste **Domínios de e-mail permitidos** (ou deixe em branco para aceitar qualquer domínio do tenant). |
| "E-mail já em uso" | O e-mail já pertence a outra conta | Resolva a conta duplicada em **Administração → Usuários**. |
| "Conta não provisionada" | O JIT está desligado e a conta não existe | Ligue **Provisionamento JIT** ou crie a conta antes em **Administração → Usuários**. |
| "Conta desativada" | A conta foi desativada no CentralOps | Reative a pessoa em **Administração → Usuários**. |

## Próximos passos

- Entender os níveis de acesso e o escopo global em detalhe: [Usuários e papéis](./users-and-roles.md).
- Gerenciar pessoas e contas: menu **Administração → Usuários**.
