---
sidebar_position: 2
title: Primeiro Login
description: Crie a conta de administrador inicial, conheça o menu e delegue acesso à equipe de SOC
---

# Primeiro Login

Esta página mostra como entrar no CentralOps pela primeira vez, criar a conta de administrador inicial e dar acesso ao restante da equipe. Tudo é feito pela interface web — você não precisa de terminal nem de configuração técnica.

## Quando usar

- **Primeiro acesso à plataforma**: a instalação acabou de ser entregue e ninguém ainda fez login.
- **Montando a equipe de SOC**: você é o administrador e precisa criar contas para analistas, operadores e engenheiros.
- **Delegando acesso com o nível certo**: você quer dar a cada pessoa só o que ela precisa (ex.: um plantonista que pode pausar integrações, mas não pode editar mapeamentos nem criar destinos).

## O que você precisa antes de começar

O CentralOps é acessado 100% pelo navegador. A instalação e a infraestrutura são responsabilidade da equipe que opera o ambiente — você recebe a plataforma já no ar e só precisa do endereço de acesso.

| Item | Onde obter |
|------|-----------|
| Endereço (URL) da plataforma | Com o administrador de infraestrutura. Costuma ser algo como `https://centralops.suaempresa.com`. |
| Suas credenciais (e-mail e senha) | Definidas por você no primeiro acesso (admin inicial), ou criadas pelo administrador em **Administração → Usuários**. |
| Um navegador atualizado | Chrome, Edge ou Firefox recentes. |

:::info
O CentralOps usa **HTTPS**. Sempre acesse o endereço começando com `https://`. A URL, o uso de HTTPS e o certificado são definidos pela equipe de infraestrutura no momento do deploy.
:::

## Criar a primeira conta (administrador)

Quando a plataforma ainda não tem nenhum usuário, ela mostra uma tela especial para criar a conta de **administrador** inicial.

1. Abra o endereço da plataforma fornecido pelo seu administrador de infraestrutura no navegador.
2. Você verá a mensagem indicando que nenhum usuário existe e o convite para criar a conta de administrador.
3. Preencha os dados:
   - **E-mail**: seu e-mail corporativo.
   - **Senha**: no mínimo 8 caracteres. Use uma senha forte.
4. Clique no botão para criar a conta de administrador.
5. Você será levado à tela de login. Entre com as credenciais que acabou de definir.

A partir daí, **novas contas só podem ser criadas por um administrador** dentro da plataforma (veja [Criar a segunda conta](#criar-a-segunda-conta-ex-operador) abaixo).

## Se você não conseguir entrar

| Sintoma | O que fazer |
|---------|-------------|
| **A página não abre / "não foi possível conectar"** | Confirme que digitou o endereço exato fornecido pelo administrador, começando com `https://`. Se ainda assim não abrir, a plataforma pode estar fora do ar — avise o administrador. |
| **Aviso de certificado / "conexão não é privada"** | Em ambientes internos, o aviso pode ser normal. Confirme com o administrador se o endereço está correto antes de prosseguir. Não ignore o aviso em endereços que você não reconhece. |
| **"E-mail ou senha inválidos"** | Verifique se digitou o e-mail certo e a senha sem espaços extras. Se esqueceu a senha, peça ao administrador para redefini-la em **Administração → Usuários**. |
| **"Conta inativa" ou acesso bloqueado** | Sua conta pode não ter sido ativada ou foi suspensa. Peça ao administrador para verificar seu usuário em **Administração → Usuários**. |
| **Entrou, mas não vê telas como Organizações, Destinos ou Roteamento** | Essas telas são exclusivas de administradores. Se você precisa delas, peça ao administrador para ajustar seu perfil de acesso. |

## Conhecer o menu

Depois de entrar, você navega pelo **menu lateral**, organizado em grupos. As telas marcadas com "(admin)" só aparecem para quem tem papel de administrador.

| Grupo | Telas | Para que serve |
|-------|-------|----------------|
| **Visão geral** | Dashboard, Organizações *(admin)*, Integrações | Painel de KPIs, gestão de organizações e a lista de integrações conectadas. |
| **Operação** | Collectors, Destinos *(admin)*, Roteamento *(admin)*, Fluxo de dados *(admin)*, Investigações, Busca federada, Detecções, Resposta, Histórico | Operação do dia a dia: fontes de entrada, saídas, regras de envio, buscas (síncrona e federada), detecções, bloqueios e histórico. Busca federada e Detecções aparecem se você tiver permissão de operador ou superior. |
| **Normalização** | Mappings, Drift Explorer, Quarentena, Saúde do Pipeline | Como os eventos são padronizados, campos novos detectados, eventos retidos e a saúde do processamento. |
| **Conhecimento** | Queries Salvas, Correlação, Agendamentos *(admin)* | Buscas reutilizáveis, regras de correlação para detecção automática e tarefas agendadas. Correlação aparece se você tiver permissão de engenheiro ou superior. |
| **Administração** | Usuários, Service Accounts, Configurações *(admin)* | Gestão de pessoas, contas de serviço e configurações da plataforma. |
| **Sua conta** | Tokens de API | Tokens pessoais para acesso programático. |

Algumas telas que você vai usar com frequência:

- **Dashboard** (em **Visão geral**): visão consolidada com os principais indicadores.
- **Collectors** (em **Operação**): as fontes que enviam eventos para a plataforma.
- **Investigações** (em **Operação**): a tela de busca para investigar eventos.
- **Resposta** (em **Operação**): a tela de bloqueios.
- **Mappings** (em **Normalização**): define como os campos dos eventos são padronizados (normalizados).
- **Quarentena** (em **Normalização**): eventos que precisam de triagem.
- **Drift Explorer** (em **Normalização**): campos novos detectados nos eventos.
- **Saúde do Pipeline** (em **Normalização**): estado do processamento de ponta a ponta.

## Entender o controle de acesso (papéis)

Sua primeira conta é de **administrador** e tem acesso a tudo. Os demais usuários podem receber papéis com permissões menores. Atribua a cada pessoa o papel mínimo necessário para a função dela.

| Papel | Pode ler | Pode alterar |
|-------|----------|--------------|
| **Viewer** | Tudo (somente leitura) | Nada. |
| **Operator** | Tudo (somente leitura) | Descartar itens da quarentena, pausar integrações, marcar campos novos como ignorados, pausar e reativar regras de roteamento. |
| **Engineer** | Tudo (somente leitura) | Editar mapeamentos, marcar campos novos como mapeados, executar recoleta histórica e reverter, simular regras de roteamento. |
| **Admin** | Tudo | Tudo, incluindo criar e editar destinos e regras de roteamento, criar usuários, gerenciar organizações e ajustar configurações da plataforma. |

Para os detalhes de cada permissão, veja a página de [controle de acesso (RBAC)](../concepts/rbac.md).

## Criar a segunda conta (ex.: operador)

1. Abra o menu **Administração → Usuários**.
2. Clique no botão para adicionar um novo usuário.
3. Informe o e-mail e deixe a plataforma gerar uma senha temporária.
4. Selecione o papel (por exemplo, **Operator**).
5. Confirme a criação.
6. Copie o link de definição de senha e compartilhe com o usuário.

O novo usuário recebe um e-mail com o link de acesso (quando o envio de e-mail estiver habilitado na plataforma) e poderá definir a própria senha no primeiro login.

> Se o envio de e-mail não estiver habilitado, o usuário não receberá a mensagem automaticamente — nesse caso, repasse o link de definição de senha diretamente. A habilitação do envio de e-mail é definida pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma.

## Próximos passos

Com a equipe criada, o próximo passo é conectar sua **primeira fonte de entrada (collector)**.

- Siga o [Quickstart](./quickstart.md) para conectar o Sophos em cerca de 15 minutos.
- Depois, veja [Destinos](../outputs/destinations.md) e [Roteamento](../outputs/routing.md) para entender como enviar os eventos para seus SIEMs e data lakes.
