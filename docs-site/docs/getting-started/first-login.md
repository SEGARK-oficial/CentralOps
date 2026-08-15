---
sidebar_position: 2
title: Primeiro Login
description: Crie a conta de administrador inicial, conheça o menu e delegue acesso à equipe de SOC
---

# Primeiro Login

Esta página mostra como entrar no CentralOps pela primeira vez, criar a conta de administrador inicial e dar acesso ao restante da equipe. Tudo é feito pela interface web: você não precisa de terminal nem de configuração técnica.

## Quando usar

- **Primeiro acesso à plataforma**: a instalação acabou de ser entregue e ninguém ainda fez login.
- **Montando a equipe de SOC**: você é o administrador e precisa criar contas para analistas, operadores e engenheiros.
- **Delegando acesso com o nível certo**: você quer dar a cada pessoa só o que ela precisa (por exemplo, um plantonista que pode pausar integrações, mas não pode editar mapeamentos nem criar destinos).

## O que você precisa antes de começar

O CentralOps é acessado 100% pelo navegador. A instalação e a infraestrutura são responsabilidade da equipe que opera o ambiente: você recebe a plataforma já no ar e só precisa do endereço de acesso.

| Item | Onde obter |
|------|-----------|
| Endereço (URL) da plataforma | Com o administrador de infraestrutura. |
| Suas credenciais | Definidas por você no primeiro acesso (admin inicial), ou criadas pelo administrador em **Administração → Usuários**. |
| Um navegador atualizado | Chrome, Edge ou Firefox recentes. |

:::info
O CentralOps usa **HTTPS**. Sempre acesse o endereço começando com `https://`. A URL, o uso de HTTPS e o certificado são definidos pela equipe de infraestrutura no momento do deploy.
:::

## Criar a primeira conta (administrador)

Quando a plataforma ainda não tem nenhum usuário, a própria tela de login entra em modo de configuração inicial e pede a criação da conta de **administrador**. Não existe usuário nem senha de fábrica, e não há uma URL separada de setup: é a mesma tela.

![Tela de login do CentralOps](/img/console/console-login.png)

1. Abra o endereço da plataforma no navegador.
2. A tela indica que nenhum usuário existe e convida a criar a conta de administrador.
3. Preencha os dados:
   - **Nome de exibição**: como seu nome aparece no console.
   - **Usuário**: o nome de login. É um **nome de usuário**, não um e-mail. `admin` é uma escolha comum.
   - **Senha**: no mínimo **10 caracteres**. Não há exigência de maiúscula, número ou símbolo, o que torna uma frase longa a opção mais segura e mais fácil de lembrar.
4. Confirme.

Você entra na plataforma **imediatamente**, sem precisar fazer login de novo: a criação da conta já abre a sessão.

:::warning
A criação do primeiro administrador é aberta a quem alcança a porta da aplicação. A única trava é não existir nenhum usuário ainda. Entre subir a plataforma e criar essa conta, qualquer pessoa com acesso de rede ao endereço pode reivindicar o administrador global. Faça esse passo logo após o deploy e não exponha a porta à internet antes dele.
:::

A partir daí, **novas contas só podem ser criadas por um administrador** dentro da plataforma (veja [Criar a segunda conta](#criar-a-segunda-conta-por-exemplo-um-operador) abaixo).

## Se você não conseguir entrar

| Sintoma | O que fazer |
|---------|-------------|
| **A página não abre ou aparece "não foi possível conectar"** | Confirme que digitou o endereço exato fornecido pelo administrador, começando com `https://`. Se ainda assim não abrir, a plataforma pode estar fora do ar. Avise o administrador. |
| **Aviso de certificado ou "conexão não é privada"** | Em ambientes internos, o aviso pode ser normal. Confirme com o administrador se o endereço está correto antes de prosseguir. Não ignore o aviso em endereços que você não reconhece. |
| **"Usuário ou senha inválidos"** | Verifique o nome de usuário (não é o seu e-mail) e a senha, sem espaços extras. Se esqueceu a senha, peça ao administrador para redefini-la em **Administração → Usuários**. |
| **A senha foi recusada ao criar a conta** | O mínimo são 10 caracteres. |
| **Várias tentativas erradas e agora nada funciona** | Há limite de tentativas: cinco falhas em cinco minutos bloqueiam novas tentativas por alguns minutos. Espere e tente de novo, com calma. |
| **"Conta inativa" ou acesso bloqueado** | Sua conta pode não ter sido ativada ou foi suspensa. Peça ao administrador para verificar seu usuário em **Administração → Usuários**. |
| **Entrou, mas não vê telas como Organizações, Destinos ou Rotas** | Essas telas são exclusivas de administradores. Se você precisa delas, peça ao administrador para ajustar seu perfil de acesso. |

## Conhecer o menu

O menu lateral segue a **ordem do pipeline**: um evento entra pela Coleta, é padronizado em Normaliza, é entregue em Roteia e vira alerta em Detecta. Cada grupo tem uma cor própria, e a cor significa o estágio, nada mais.

Há um quinto estágio, **Reduz**, que hoje não tem tela própria e por isso não aparece no menu: os números de economia de volume ficam no cartão de custo dentro de **Roteia → Fluxo de dados**.

![Menu lateral do console, organizado por estágio do pipeline](/img/console/console-navegacao.png)

As três primeiras telas ficam fixas no topo, acima de um separador e **sem cabeçalho de grupo**: são as que você abre a qualquer momento, independentemente do estágio em que está trabalhando. A documentação se refere a elas como **Visão geral**.

| Grupo | Telas | Para que serve |
|-------|-------|----------------|
| **Visão geral** (topo, sem cabeçalho) | Dashboard, Saúde do pipeline, Histórico | Indicadores consolidados, estado do processamento de ponta a ponta e o histórico de execuções. |
| **Coleta** | Integrações, Coletores | As fontes que enviam eventos para a plataforma e o estado de cada coleta. |
| **Normaliza** | Mapeamentos, Drift, Quarentena, Governança OCSF | Como os campos dos eventos são padronizados, campos novos detectados, eventos retidos para triagem e a conformidade com o OCSF. |
| **Roteia** | Rotas, Destinos, Fluxo de dados | As regras que decidem para onde cada evento vai, os destinos configurados e a topologia ao vivo. |
| **Detecta** | Detecções, Queries salvas, Agendamentos | Detecções disparadas, o catálogo de consultas curadas e as tarefas agendadas. |
| **Administração** | Organizações, Usuários, Contas de serviço, Configurações | Gestão de tenants, pessoas, acesso programático e configurações da plataforma. |

Sua conta (perfil, senha e sessões) fica no **menu do avatar**, no canto superior direito, não no menu lateral.

:::info[Console ocioso é um console saudável]
O que está em repouso não recebe cor. Uma rota habilitada não ganha selo, uma integração saudável não fica verde. Você varre a tela procurando o que **não** está neutro: âmbar pede atenção, vermelho já falhou. É por isso que o âmbar registra quando aparece.
:::

### Edição Enterprise

Com a licença Enterprise, o grupo **Detecta** ganha duas telas a mais: **Busca federada** e **Correlação**. Elas não existem na edição Community. Veja [Community vs Enterprise](/editions/community-vs-enterprise).

## Entender o controle de acesso (papéis)

Sua primeira conta é de **administrador** e tem acesso a tudo. Os demais usuários podem receber papéis com permissões menores. Atribua a cada pessoa o papel mínimo necessário para a função dela.

| Papel | Pode ler | Pode alterar |
|-------|----------|--------------|
| **Viewer** | Tudo (somente leitura) | Nada. |
| **Operator** | Tudo (somente leitura) | Descartar itens da quarentena, pausar integrações, marcar campos novos como ignorados, pausar e reativar regras de roteamento. |
| **Engineer** | Tudo (somente leitura) | Editar mapeamentos, marcar campos novos como mapeados, executar recoleta histórica e reverter, simular regras de roteamento. |
| **Admin** | Tudo | Tudo, incluindo criar e editar destinos e rotas, criar usuários, gerenciar organizações e ajustar configurações da plataforma. |

Para os detalhes de cada permissão, veja a página de [controle de acesso (RBAC)](../concepts/rbac.md).

## Criar a segunda conta (por exemplo, um operador)

1. Abra **Administração → Usuários**.
2. Clique para adicionar um novo usuário.
3. Informe o nome de usuário, o nome de exibição e **defina a senha inicial**. Você digita a senha: a plataforma não gera senha temporária nem envia link de definição por e-mail.
4. Selecione o papel (por exemplo, **Operator**).
5. Confirme a criação.
6. Combine com a pessoa um canal seguro para repassar a senha, e peça que ela troque no primeiro acesso, em **Conta → Perfil e segurança**.

:::tip
Trocar a própria senha revoga automaticamente todas as outras sessões daquele usuário. É o comportamento desejado quando a senha inicial passou por um canal que você não controla totalmente.
:::

## Próximos passos

Com a equipe criada, o próximo passo é conectar sua **primeira fonte de eventos**.

- Siga o [Quickstart](./quickstart.md) para ligar uma integração e ver o evento sair do outro lado.
- Depois, veja [Destinos](../outputs/destinations.md) e [Roteamento](../outputs/routing.md) para entender como enviar os eventos para seus SIEMs e data lakes.
