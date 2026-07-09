---
sidebar_position: 2
title: RBAC (Controle de acesso)
description: Papéis e permissões — o que cada perfil de usuário pode fazer na plataforma
---

# RBAC — Controle de acesso por papéis

O CentralOps controla o que cada usuário pode ver e fazer por meio de **papéis**. Cada usuário recebe um papel, e o papel define o conjunto de permissões — desde apenas visualizar até administrar a plataforma inteira. São quatro papéis, com permissões progressivas: **Viewer**, **Operator**, **Engineer** e **Admin**.

## Quando usar

- **Onboarding de um novo analista de SOC**: dar a um analista de triage apenas o papel Viewer, para que ele investigue alertas e eventos sem risco de alterar regras ou pausar coletas.
- **Plantão (oncall) que limpa o pipeline**: um técnico de plantão precisa descartar e reprocessar eventos em quarentena e pausar uma integração com problema, mas não deve mexer em regras de normalização — o papel Operator cobre exatamente isso.
- **Separação de funções para auditoria/compliance**: garantir que só o Admin crie destinos, configure roteamento e gerencie usuários, mantendo o restante do time no menor privilégio necessário e com tudo registrado em histórico.

## Escopo por organização e subárvore

O CentralOps suporta ambientes multicliente com hierarquias de organizações. A visibilidade de dados e permissões de cada usuário é controlada pelo seu **escopo de organização** e pelo tipo de relacionamento (próprio nó vs. subárvore).

### Três personas principais:

**Admin Global (plataforma)**
- Sem organização definida (`is_global=True` ou `organization_id` vazio)
- Vê e gerencia **todas as organizações** e todas as operações de plataforma
- Pode criar, editar e deletar organizações
- Pode criar e gerenciar outros admins globais
- Responsável pela infraestrutura, compliance global e permissões em nível de plataforma
- Use-case: equipe de infraestrutura ou administrador central

**Admin de MSP/Reseller (subárvore)**
- Escopado a uma organização-pai (reseller ou MSP) com visibilidade de subárvore
- Vê e gerencia a organização-pai **e todas as suas filhas** (clientes, subresellers, etc.)
- Pode criar usuários dentro da sua subárvore
- **Não pode** criar ou deletar organizações (ação de plataforma)
- **Não pode** conceder o papel de admin global a ninguém
- **Não pode** gerenciar integrações auto-gerenciadas (partner-managed)
- Use-case: provedor de serviços gerenciados (MSSP) ou reseller que suporta múltiplos clientes

**Admin da Organização (tenant-admin)**
- Escopado a uma organização específica (folha ou nó isolado) — só vê aquela organização
- Gerencia usuários e configurações **apenas da própria organização**
- **Não pode** criar, editar ou deletar organizações
- **Não pode** conceder o papel de admin global a ninguém
- **Não pode** gerenciar integrações auto-gerenciadas (partner-managed)
- Ideal para: cliente que tem autonomia sobre seus próprios usuários e políticas

**Analista SOC com Visibilidade Global**
- Usuário com papel Operator ou Engineer + `is_global=True`
- Acesso de leitura a **todas as organizações**, mas sem capacidade de criar/editar
- Usado para correlação, investigação cross-tenant e análise centralizada
- Cria auditoria se tocar em dados sensíveis (mesmo com acesso de leitura)

### Regra de visibilidade:

- Um usuário escopado a uma organização **vê apenas aquela organização** (e suas filhas, se aplicável)
- Tentativas de acessar dados de outras organizações são bloqueadas com erro 403 (acesso negado)
- Essa restrição vale para todos os endpoints: mappings, integrações, quarentena, histórico, etc.
- Isolamento é aplicado no backend — não é possível contornar via UI

## Matriz de papéis e permissões

A tabela mostra o que cada papel pode fazer. "✓" = permitido; "—" = não permitido.

| Área | Ação | Viewer | Operator | Engineer | Admin |
|------|------|--------|----------|----------|-------|
| **Mappings** | Ver mappings | ✓ | ✓ | ✓ | ✓ |
| | Editar mappings e criar versões | — | — | ✓ | ✓ |
| | Reverter para versão anterior | — | — | ✓ | ✓ |
| **Integrações (coleta)** | Ver integrações | ✓ | ✓ | ✓ | ✓ |
| | Criar/editar integrações e credenciais | — | — | — | ✓ |
| | Pausar/retomar coleta | — | ✓ | ✓ | ✓ |
| **Quarentena** | Ver eventos em quarentena | ✓ | ✓ | ✓ | ✓ |
| | Descartar e reprocessar eventos | — | ✓ | ✓ | ✓ |
| **Campos novos detectados (Drift)** | Ver campos detectados | ✓ | ✓ | ✓ | ✓ |
| | Marcar campo como "ignorar" | — | ✓ | ✓ | ✓ |
| | Marcar campo como "já mapeado" | — | — | ✓ | ✓ |
| | Excluir entrada de campo detectado | — | — | ✓ | ✓ |
| **Destinos** | Ver destinos | ✓ | ✓ | ✓ | ✓ |
| | Criar/editar destinos | — | — | — | ✓ |
| **Roteamento** | Ver regras de roteamento | ✓ | ✓ | ✓ | ✓ |
| | Criar/editar regras de roteamento | — | — | — | ✓ |
| **Histórico** | Consultar histórico de auditoria | ✓ | ✓ | ✓ | ✓ |
| **Buscas e queries** | Ver eventos já entregues (sem busca ao vivo) | ✓ | ✓ | ✓ | ✓ |
| | Rodar query/hunt AO VIVO na fonte (query.run) | — | ✓ | ✓ | ✓ |
| | Triar detecções (abrir/reconhecer/fechar) | — | ✓ | ✓ | ✓ |
| | Salvar query, agendamentos e correlações (query.save) | — | — | ✓ | ✓ |
| | Criar/editar/excluir regras de correlação | — | — | ✓ | ✓ |
| **Ações destrutivas** | Bloquear IP/hash (ACTION_BLOCK) | — | — | — | ✓ |
| **Administração** | Gerenciar usuários | — | — | — | ✓ |
| | Gerenciar organizações | — | — | — | ✓* |
| | Ver credenciais armazenadas | — | — | — | ✓ |

*Nota: criar, editar e deletar organizações é uma **ação de plataforma** que exige **Admin Global** (sem escopo de organização). Um Admin da organização pode gerenciar usuários dentro de seu escopo, mas não pode criar ou deletar organizações.

## O que cada papel faz

### Viewer

**Para quem**: analista que investiga alertas e eventos, mas não executa ações.

**Pode**:
- Ver Dashboard, indicadores e alertas.
- Ver detalhes de eventos em quarentena (somente leitura).
- Ver campos novos detectados (somente leitura).
- Consultar o Histórico de auditoria.
- Buscar eventos na base histórica pelos filtros de **Alertas** e **Investigações** (leitura de eventos JÁ ENTREGUES aos destinos).
- Ver destinos, regras de roteamento e mappings (somente leitura).

**Não pode**:
- Rodar query/hunt AO VIVO na fonte (precisa de Operator) — isso dispara coleta no cliente e custa $.
- Triar detecções ou triage de resultados ao vivo (precisa de Operator).
- Salvar query, criar agendamentos ou regras de correlação (precisa de Engineer).
- Descartar quarentena (precisa de Operator).
- Editar mappings (precisa de Engineer).
- Criar destinos ou regras de roteamento (precisa de Admin).

**Exemplo**: analista de SOC que só faz triage de alertas, sem alterar nada.

### Operator

**Para quem**: técnico que monitora o pipeline e executa ações de rotina.

**Pode** (tudo do Viewer, mais):
- Rodar query/hunt AO VIVO na fonte (pull de alertas do cliente — custa $).
- Triar detecções: abrir, reconhecer (acknowledge) ou fechar.
- Descartar eventos de quarentena.
- Reprocessar eventos de quarentena (depois que o mapping foi corrigido).
- Pausar e retomar a coleta de uma integração.
- Marcar campos novos detectados como "ignorar".

**Não pode**:
- Salvar query, criar agendamentos ou regras de correlação (precisa de Engineer).
- Editar mappings (precisa de Engineer).
- Bloquear IP/hash (precisa de Admin).
- Criar destinos ou regras de roteamento (precisa de Admin).
- Gerenciar usuários (precisa de Admin).

**Exemplo**: plantonista (oncall) que acompanha alertas e limpa a quarentena, sem mexer em regras de normalização ou roteamento.

### Engineer

**Para quem**: especialista em segurança que customiza a normalização dos eventos.

**Pode** (tudo do Operator, mais):
- Salvar query e criar agendamentos de busca.
- Criar, editar e excluir regras de correlação (threshold — detecções cross-source).
- Editar mappings no editor de mapeamento, criar versões e reverter para uma versão anterior.
- Marcar campos novos detectados como "já mapeados".
- Excluir entradas de campos detectados.

**Não pode**:
- Bloquear IP/hash (precisa de Admin).
- Criar/editar destinos ou regras de roteamento (precisa de Admin).
- Gerenciar integrações de coleta — criar, excluir, alterar credenciais (precisa de Admin).
- Gerenciar usuários (precisa de Admin).

**Exemplo**: engenheiro de segurança que ajusta as regras de normalização sempre que um fornecedor muda o formato dos eventos.

### Admin

**Para quem**: responsável pela plataforma, compliance e segurança operacional. O Admin também opera tudo pela interface.

**Pode** (tudo, mais):
- Bloquear IP ou hash (ação destrutiva).
- Criar, editar e excluir destinos (Syslog, Splunk HEC, Elastic, S3, Sentinel, Kafka, OTLP, entre outros).
- Configurar as credenciais de cada destino e testar a conexão.
- Criar, editar e excluir regras de roteamento, incluindo envio simultâneo a vários destinos e remoção de dados sensíveis (PII) por regra.
- Criar, excluir e desativar usuários e alterar o papel de cada um (dentro do escopo de organização).
- Gerenciar organizações — **apenas se for Admin Global** (sem escopo de organização).
- Configurar a retenção de dados.
- Ver as credenciais armazenadas (credenciais de integrações e de destinos). Essas credenciais ficam armazenadas de forma encriptada.
- Executar a remoção de dados para atendimento à LGPD/GDPR.

**Restrições de escopo**:
- Um **Admin da Organização** (escopado a uma org específica) gerencia usuários e configurações apenas daquela organização, não pode criar/deletar organizações, não pode conceder `is_global=True` a ninguém e não pode gerenciar integrações auto-gerenciadas.
- Um **Admin Global** (sem escopo ou `is_global=True`) pode executar todas as ações acima, incluindo gerenciar a hierarquia completa de organizações.

> A chave de criptografia da plataforma é definida pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma.

**Exemplo**: administrador da plataforma, responsável por compliance, auditoria e pela saída de dados para destinos externos.

## Permissões de busca e correlação (Operator em diante)

A partir do papel Operator, dois tipos de permissão governam tudo o que envolve query, investigação e detecção:

### query.run — Rodar buscas ao vivo e triar detecções (Operator+)

- **Rodar query ao vivo**: pull de alertas/eventos direto da fonte (Wazuh, Sophos, CrowdStrike, Defender, etc.). Toca o cliente final e incorre em custo.
- **Triar detecções**: abrir, reconhecer (acknowledge) ou fechar uma detecção que surgiu de análise automatizada (query salva, correlação ou scheduled query).
- **Listar e detalhar regras de correlação**: ler o catálogo (não criar/editar).

Quem tem `query.run`: **Operator**, Engineer, Admin.

### query.save — Salvar queries, agendamentos e correlações (Engineer+)

- **Salvar query**: persistir uma busca federada (multi-source) para reutilização futura.
- **Criar agendamentos**: executar query salva em horários recorrentes, gerando detecções automatizadas.
- **CRUD de regras de correlação**: criar, editar e excluir regras de threshold para detectar padrões cross-source (ex.: "5 eventos do mesmo IP em 5 minutos").

Quem tem `query.save`: **Engineer**, Admin.

Ambas as permissões são **org-scoped fail-closed**: um Engineer da organização A não consegue fazer query ou criar correlação da organização B, mesmo que ambas estejam na mesma instância (multi-tenant).

## Exemplos de atribuição

### Empresa pequena (cerca de 10 pessoas)

| Papel | Pessoas | Perfil |
|-------|---------|--------|
| Admin | 2 | Responsável por infraestrutura + responsável por segurança |
| Engineer | 3 | Time de resposta a incidentes que ajusta mappings |
| Operator | 3 | Monitoramento 24/7 |
| Viewer | 2 | Auditor externo, somente leitura |

### Provedor de serviços gerenciados (MSSP, 50+ clientes)

| Papel | Quantidade | Perfil |
|-------|-----------|--------|
| Admin global | 1 | Gerencia a hierarquia de organizações, destinos e roteamento global |
| Admin de reseller/MSP | 1 por reseller | Usuário Admin com escopo na organização-reseller, vê a subárvore de clientes |
| Admin da organização | 1 por cliente | Usuário Admin com escopo restrito a uma organização específica (não vê outras) |
| Engineer | 2-3 por organização | Customizam os mappings da organização |
| Operator | 5-10 por organização | Monitoramento |
| Viewer | conforme a necessidade | Consultas e análise |

## Como atribuir papéis

> As telas abaixo só aparecem para usuários com papel Admin.

### Criar um novo usuário com papel

1. Abra o menu **Administração → Usuários**.
2. Inicie a criação de um novo usuário.
3. Preencha o e-mail e selecione o papel.
4. Confirme a criação. A plataforma gera um acesso temporário.
5. Compartilhe com o usuário o link para definir a senha.

### Alterar o papel de um usuário existente

1. Abra o menu **Administração → Usuários**.
2. Localize o usuário na lista.
3. Abra a edição do usuário e altere o papel.
4. Salve a alteração.

A mudança vale imediatamente. A sessão ativa do usuário continua, mas o próximo acesso já usa o novo papel.

## Service Accounts (tokens de automação)

Os **Service Accounts** são tokens usados por automações (sem um usuário humano por trás), criados e gerenciados no menu **Administração → Service Accounts**.

Um token de automação **nunca tem mais permissões do que o usuário que o criou**, e pode ser configurado com um acesso ainda mais restrito do que o desse usuário. Assim, mesmo que o token vaze, o estrago possível fica limitado ao escopo que você definiu.

**Exemplo**: integrações com pipelines de CI/CD, webhooks de terceiros e automações de ponta a ponta, sem depender de credencial de uma pessoa.

Para criar e gerenciar tokens, acesse o menu **Administração → Service Accounts**.

## Considerações de segurança

- **Menor privilégio**: sempre atribua o menor papel que permita a pessoa fazer o trabalho dela.
- **Auditoria**: toda mudança de permissão fica registrada e pode ser consultada na tela de Histórico.
- **Isolamento entre organizações**: um Engineer da organização A não consegue acessar os mappings da organização B, mesmo que as duas estejam na mesma instância. Um Admin da organização A não pode criar/deletar organizações ou conceder privilégios globais.
- **Credenciais**: apenas o Admin pode ver os valores de credenciais armazenadas (de integrações e de destinos). Essas credenciais ficam armazenadas de forma encriptada.
- **Delegação com limite**: um Admin escopado a uma organização nunca pode elevar outro usuário acima do seu próprio teto de permissões. Ações de plataforma (criar/deletar organizações, conceder `is_global=True`) exigem Admin Global.
