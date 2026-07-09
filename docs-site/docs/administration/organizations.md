---
sidebar_position: 2
title: Organizações
description: Separe os dados de cada cliente ou departamento na mesma plataforma, com isolamento total entre eles.
---

# Organizações

Uma **organização** é um espaço isolado dentro do CentralOps. Cada organização tem seus próprios dados, e o que pertence a uma nunca aparece para outra, mesmo rodando tudo na mesma plataforma.

Essa tela está disponível apenas para administradores da plataforma.

## Quando usar

- **MSSP que atende vários clientes.** Você opera um SOC para 50 clientes diferentes e precisa que cada um veja só os próprios alertas, integrações e destinos — sem montar 50 ambientes separados.
- **Separar departamentos da mesma empresa.** Vendas, Financeiro e RH usam a mesma plataforma, mas cada área cuida das próprias integrações sem enxergar os dados das outras.
- **Encerrar um contrato com segurança.** Um cliente cancelou. Você precisa apagar todos os dados dele de forma rastreável, sem afetar os demais.

## O que uma organização agrupa

Cada organização tem, de forma independente:

| Item | O que é |
|------|---------|
| Integrações | As credenciais e conexões com os fornecedores (vendors) daquele cliente. |
| Destinos | As saídas configuradas para onde os eventos são enviados (Wazuh, Splunk, S3, etc.). |
| Roteamento | As regras de normalização e de envio dos eventos. |
| Usuários | O time daquele cliente ou departamento. |
| Histórico de ações | O registro de quem fez o quê e quando. |
| Política de retenção | Por quanto tempo cada tipo de dado é guardado. |

Os dados da Organização A são **completamente invisíveis** para a Organização B.

## Acessar a tela

No menu lateral, vá em **Visão geral -> Organizações**.

A tela mostra uma tabela com todas as organizações:

| Coluna | Descrição |
|--------|-----------|
| Nome | Nome da organização (ex.: "Acme Corp"). |
| Identificador | Apelido curto e único usado internamente (ex.: "acme-corp"). |
| Usuários | Quantas pessoas pertencem à organização. |
| Integrações | Quantas integrações estão ativas. |
| Criada em | Data de criação. |
| Ações | Editar ou excluir. |

## Criar uma organização

1. Na tela **Visão geral -> Organizações**, clique no botão de criar nova organização.
2. Preencha o formulário:
   - **Nome**: o nome do cliente ou departamento (ex.: "Acme Corporation").
   - **Identificador**: um apelido curto e único. Ele é sugerido automaticamente a partir do nome, mas você pode ajustá-lo. Use apenas letras minúsculas e hifens, sem espaços.
   - **Descrição** (opcional): uma nota para você se localizar (ex.: "Cliente financeiro, crítico para LGPD").
3. Confirme a criação.

A organização aparece imediatamente na lista.

## Adicionar usuários a uma organização

Cada usuário pertence a uma organização e só enxerga os dados dela.

1. Crie o usuário normalmente em **Administração -> Usuários** (veja [Usuários](./users-and-roles.md)).
2. No cadastro, selecione a organização à qual ele pertence.
3. Salve.

A partir daí, esse usuário vê apenas os dados daquela organização.

### Mover um usuário para outra organização

Não existe um botão de "mover usuário" entre organizações. O caminho recomendado é recriar a conta no destino:

1. Desative a conta na organização de origem.
2. Crie uma nova conta para a pessoa na organização de destino.
3. Envie a ela o link de redefinição de senha.

## Definir a retenção de dados por organização

Cada organização pode guardar os dados por prazos diferentes.

1. Na lista, clique na organização para abrir os detalhes.
2. Abra a edição da política de retenção.
3. Ajuste o prazo (em dias) para cada tipo de dado:

| Tipo de dado | Padrão sugerido |
|--------------|-----------------|
| Quarentena | 7 dias |
| Campos novos detectados (Drift) | 90 dias |
| Histórico | 30 dias |
| Resultados de buscas | 7 dias |
| Histórico de ações | 365 dias (mínimo legal) |

Regras de validação:

- Mínimo de 1 dia e máximo de 3650 dias (10 anos) por tipo.
- O histórico de ações **nunca pode ficar abaixo de 365 dias**, por exigência de conformidade.

Como funciona o descarte:

- Eventos mais antigos que o prazo definido são removidos automaticamente.
- A limpeza roda uma vez por dia, de madrugada.
- Exemplo: se a quarentena estiver em 14 dias, os eventos com mais de 14 dias são apagados.

## Apagar os dados de uma organização (LGPD/GDPR)

Quando um cliente cancela o contrato e pede a exclusão dos dados, o administrador pode disparar uma exclusão completa.

1. Na lista, clique na organização e escolha a ação de solicitar a exclusão dos dados.
2. O sistema pede uma confirmação de segurança.
3. Digite o texto de confirmação exatamente como solicitado (incluindo o identificador da organização).
4. Informe o motivo (ex.: "Cliente cancelado em 2026-04-25").
5. Confirme.

A exclusão roda em segundo plano e passa pelos estados **pendente**, **em andamento** e **concluída**. Você pode acompanhar o andamento em **Operação -> Histórico**, filtrando pelas ações de exclusão de dados.

### O que é apagado

- As integrações e credenciais da organização.
- Os mapeamentos.
- Os eventos (brutos, normalizados e em quarentena).
- Os usuários.
- A própria organização.

### O que NÃO é apagado

- **Trilha legal da exclusão**: o registro de que a exclusão aconteceu é preservado para fins de conformidade (LGPD, art. 15), mesmo depois que os dados são removidos.
- **Eventos já entregues a destinos externos**: o que já foi enviado para o Wazuh, Splunk ou outro destino permanece lá. Para apagá-los, é preciso fazer isso diretamente no destino.

## Identidade do cliente em destinos externos

Quando os eventos de uma organização são enviados para destinos externos (como o Wazuh ou o DFIR-IRIS), a plataforma associa cada organização à identidade correspondente naquele destino. Essa associação garante que os eventos cheguem ao cliente certo no sistema de destino.

Você não precisa montar esse vínculo manualmente na maioria dos casos: ao configurar um destino para uma organização, a associação é criada e mantida automaticamente. A entrega dos eventos não depende dessa configuração externa — se ela ainda não existir, os eventos continuam sendo coletados, normalizados e roteados normalmente para os destinos configurados.

### DFIR-IRIS (opcional)

A integração com o DFIR-IRIS é **opcional**. Ela serve para associar cada organização ao cliente correspondente no IRIS, mas **não bloqueia a entrega de eventos**: se ela não estiver configurada, tudo segue funcionando para os demais destinos.

A conexão com o IRIS (endereço e chave de acesso) é definida pela equipe de infraestrutura no momento do deploy. Se precisar habilitar ou alterar essa conexão, fale com o administrador da plataforma.

Com a conexão habilitada, você pode acionar, na tela de detalhes da organização, a sincronização com o IRIS. A plataforma localiza o cliente de mesmo nome no IRIS e passa a usar essa associação ao enviar eventos.

## Organizações gerenciadas automaticamente

Em alguns ambientes, parte das organizações é criada e mantida **automaticamente** pela plataforma, a partir de uma sincronização com um sistema parceiro (por exemplo, em ambientes integrados ao Sophos Partner).

O que muda na prática para o administrador:

| Tipo de organização | Origem | O que você pode fazer |
|---------------------|--------|------------------------|
| Gerenciada automaticamente | Criada e atualizada por sincronização com o sistema parceiro. | Visualizar. Não edite nem exclua pela interface — as mudanças seriam sobrescritas na próxima sincronização. |
| Criada manualmente | Criada por você nesta tela. | Editar, configurar e excluir normalmente pela interface. |

Na lista de organizações, as gerenciadas automaticamente aparecem identificadas como tal. Quando esse modo não está ativo na sua instância, todas as organizações são manuais e ficam sob seu controle total.

## Hierarquia e resellers (MSP)

A plataforma permite que uma organização se torne um **reseller** e gerencie outras organizações filhas em uma hierarquia. Esse modelo é ideal para MSPs (Managed Service Providers) que revendem a plataforma para vários clientes finais.

### O que é um reseller

Um reseller é uma organização que:

- Gerencia uma ou mais organizações filhas (tenants).
- Cada filho herda as credenciais do reseller para coletar dados de fontes parceiras (como o Sophos Partner).
- Os administradores do reseller podem ter visibilidade sobre as organizações filhas, conforme as permissões definidas.
- A hierarquia é rastreável: você sempre sabe quem é o pai de cada organização.

### Como ativar o reseller de uma organização

Tornar uma organização um reseller é uma **ação de administrador da plataforma** — não é self-service. O dono da plataforma precisa ativar manualmente.

Quando habilitado, o reseller:

1. Ganha a capacidade de gerenciar organizações filhas através da sincronização com o sistema parceiro.
2. Recebe uma **cota máxima de tenants filhos** — um limite que impede crescimento descontrolado. Por exemplo: "este reseller pode criar até 50 organizações filhas".
3. Se a cota for atingida, novas criações são rejeitadas até que a quota seja aumentada.

A cota é configurável (inclusive ilimitada) e é definida pelo administrador da plataforma no momento da ativação.

### Hierarquia e permissões

Quando a hierarquia é ativada:

- Um **administrador global** da plataforma vê todas as organizações em qualquer nível.
- Um **administrador de reseller** vê apenas o reseller próprio e suas organizações filhas.
- Um **administrador de tenant** vê apenas a própria organização.

Essa segmentação garante que cada nível só acesse o que lhe pertence.

## Provisionamento automático (Sophos Partner)

Quando a integração com o Sophos Partner está ativa, a plataforma pode **criar automaticamente um administrador de tenant** no momento em que uma nova organização filha é materializada e aprovada.

### O que acontece

1. Um tenant novo é sincronizado do Sophos Partner e aguarda aprovação.
2. Você aprova o tenant através da interface.
3. A plataforma cria automaticamente (se habilitado):
   - Uma conta de usuário com a função **administrador do tenant**.
   - A conta fica em estado **pendente** — ela não tem senha definida.
   - O administrador do tenant ativa a conta através de um convite por email ou via SSO/SCIM.

### Como ativar

A ativação é **opt-in** e controlada pelo administrador da plataforma. Por padrão, o auto-provisionamento está desabilitado (fail-safe).

Quando ativado, a operação é:

- **Idempotente**: se você ativar novamente a mesma organização, a conta não é duplicada — o sistema reutiliza a existente.
- **Best-effort**: se algo falhar durante a criação da conta, a sincronização do tenant continua — a conta pode ser criada manualmente depois.
- **Segura**: a conta criada é sempre um administrador local do tenant (não global), sem permissão de escalar ou tocar em outras organizações.

### Ativação do usuário

O usuário ativa a conta através de:

- **Email de convite**: recebe um link para definir senha.
- **SSO/SCIM**: ao fazer login pela primeira vez com suas credenciais corporativas, a conta é automaticamente ativada.

## Criação e exclusão de organizações

Criar ou deletar uma organização é uma **ação de plataforma**, restrita a **administradores globais da plataforma**. Um administrador de reseller ou de tenant não consegue criar ou deletar organizações — apenas visualizar, editar configurações, e gerenciar usuários dentro da hierarquia que já existe.

## Limitações atuais

Os itens abaixo ainda não existem e estão no roadmap. Não conte com eles hoje:

- **Residência de dados por região**: a plataforma não oferece pinning de dados a regiões específicas ou sharding por geografia.
- **Cotas de usuários ou de eventos**: não há limite máximo de usuários ou volume de eventos por organização.
- **Observabilidade regionalizada**: métricas e logs não são segregados por reseller ou região.

## Segurança e isolamento

- **Isolamento de dados**: cada consulta enxerga apenas os dados da organização do usuário. Não há vazamento entre organizações.
- **Permissões respeitadas**: um usuário de uma organização não consegue atribuir permissões em outra.
- **Rastreabilidade**: toda mudança em uma organização fica registrada, com autor e horário, no histórico de ações.
- **Anti-escalonamento**: administradores de reseller ou tenant não conseguem se elevar a privilégios de plataforma, nem acessar dados fora de sua hierarquia.

## Próximos passos

- **Gerenciar usuários da organização?** Veja [Usuários](./users-and-roles.md).
- **Integrações da organização?** Vá em **Visão geral -> Integrações**.
- **Destinos e roteamento?** Vá em **Operação -> Destinos** ou veja [Roteamento](../outputs/routing.md).
- **Retenção de dados?** Veja [Compliance > Retenção](../compliance/retention.md).
