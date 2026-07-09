---
sidebar_position: 7
title: Histórico e auditoria
description: Veja quem fez o quê e quando na plataforma, com filtros, exportação e retenção automática.
---

# Histórico e Auditoria

A tela **Operação -> Histórico** registra, de forma imutável, todas as ações e mudanças feitas na plataforma: quem fez, o quê e quando. É a sua trilha de auditoria para investigar mudanças, atender compliance e apoiar análises pós-incidente.

Os registros ficam disponíveis por **365 dias** (para atender LGPD/GDPR). Operadores e analistas veem as ações da sua organização; administradores também veem os eventos de sistema e de gestão de usuários.

## Quando usar

- **Investigar uma mudança suspeita.** Um mapeamento parou de funcionar do nada — você abre o Histórico, filtra por mapeamento editado e descobre quem mexeu, quando e o que mudou.
- **Forensics pós-incidente.** Depois de um alerta, você reconstrói a linha do tempo: que integração foi criada, que destino foi alterado, quem acessou os dados do cliente afetado.
- **Auditoria de compliance.** O auditor pede evidência de todas as operações críticas do ano anterior — você filtra, exporta em PDF e entrega ao time de GRC.

## O que é registrado

A trilha cobre as ações feitas em cada área da plataforma.

### Integrações

- Criação de integração.
- Edição de credenciais.
- Pausa e retomada.
- Teste de conexão.
- Exclusão.

### Mappings

- Criação de mapeamento.
- Edição no editor de mapeamento.
- Restauração de uma versão anterior.
- Publicação (marcar a versão como atual).

### Quarentena

- Descarte de um evento.
- Reprocessamento de um evento.
- Descarte em massa.
- Reprocessamento em massa.

### Drift Explorer

- Marcar um campo novo detectado como ignorado.
- Marcar um campo novo detectado como mapeado.
- Remoção de uma entrada de campo detectado.

### Destinos (só admin)

- **Criação** de destino (por exemplo syslog, Splunk, Elastic, S3, Sentinel, Kafka, OTLP ou arquivo).
- **Edição** de configuração ou credenciais.
- **Teste de conexão** (verificação rápida de saúde do destino).
- **Modo de validação** (replica os eventos para o destino sem entrega real, para validar antes de ir para produção).
- **Exclusão** de destino.

Cada registro de auditoria mostra a configuração do destino, mas **nunca exibe as credenciais** — apenas indica se o destino tem ou não uma credencial configurada. Veja [Destinos](../outputs/destinations.md) para a lista completa de tipos.

### Roteamento (só admin)

- **Criação** de uma rota (com sua condição e seus destinos de entrega).
- **Edição** de prioridade, condição ou destinos da rota.
- **Reordenação** de rotas (ao arrastar para reordenar na tela).
- **Restauração** de uma versão anterior da rota (pela opção de auditoria dentro da rota).
- **Alteração da regra de redação de PII** de uma rota (mascara ou pseudonimiza campos antes de enviar ao destino — ver [Roteamento e redação de PII](../outputs/routing.md)).
- **Exclusão** de rota (a rota padrão que captura tudo é protegida e não pode ser excluída).

### Fila de reenvio (só admin)

Quando um evento é rejeitado por um destino instável, ele vai para a fila de reenvio. O Histórico registra:

- O **reprocessamento** desses eventos, feito pelo botão de reprocessar na tela de **Operação -> Destinos**.
- Quem reprocessou, quando, quantos eventos foram reenviados e para qual destino.

### Usuários (só admin)

- Criação de usuário.
- Alteração de papel.
- Desativação.
- Redefinição de senha.

### Organizações (só admin)

- Criação de organização.
- Alteração de configuração.
- Mudança de política de retenção.
- Solicitação de exclusão de dados.

### Sistema (só admin)

- **Rotação da chave de criptografia da plataforma.**
- **Rotação de chave de criptografia via provedor externo** (quando a plataforma usa um cofre de chaves externo).
- Backup e restauração.
- Mudanças de configuração (por exemplo, servidor de e-mail).

:::note
A rotação de chaves e a configuração do cofre de chaves são definidas pela equipe de infraestrutura no momento do deploy. O Histórico registra **quando** essas operações acontecem, para fins de auditoria; alterar como elas funcionam é responsabilidade do administrador da plataforma.
:::

## Como usar a tela

### Filtros

| Filtro | Opções |
|--------|--------|
| **Tipo** | Integração, mapeamento, quarentena, destino, rota, fila de reenvio, usuário, etc. |
| **Ação** | Criar, editar, excluir, pausar, testar, reprocessar, restaurar, etc. |
| **Usuário** | Lista de todos os usuários ativos. |
| **Data** | Últimas 24h, 7 dias, 30 dias ou um intervalo personalizado. |

### Colunas

| Coluna | Conteúdo |
|--------|----------|
| **Data/hora** | Quando a ação aconteceu. |
| **Usuário** | Quem fez (ou "Sistema", para ações automáticas). |
| **Tipo** | A que área se refere (integração, mapeamento, destino, rota, etc.). |
| **Ação** | O que foi feito (criar, editar, excluir, testar, reprocessar, restaurar). |
| **Recurso** | Nome do que foi alterado. |
| **Detalhes** | As mudanças específicas (clique para expandir). |

## Fluxos comuns

### Investigar uma mudança recente

Pergunta: "Alguém alterou o mapeamento do Sophos?"

1. Abra **Operação -> Histórico**.
2. Filtre por **Tipo = mapeamento** e **Ação = editar**.
3. Localize a edição na lista — por exemplo, "Sophos alerts v3 -> v4", às 10:30, pelo usuário "alice".
4. Clique para expandir e ver exatamente o que mudou.

### Auditar um destino

Pergunta: "Quando o destino Splunk foi criado? Quem editou?"

1. Em **Operação -> Histórico**, filtre por **Tipo = destino** e busque por "Splunk".
2. Você vê os eventos de criação, edição e teste.
3. Expanda cada versão para ver a configuração registrada — sempre **sem as credenciais**.

### Restaurar uma versão de rota

Pergunta: "A rota de PII foi alterada. Como volto?"

1. Abra **Operação -> Roteamento**.
2. Clique na rota e abra a opção de auditoria dentro dela.
3. Veja o histórico de condição, destinos e regra de redação de PII.
4. Restaure a versão anterior. A restauração também fica registrada no Histórico (quem, quando, de qual versão para qual).

### Auditar a atividade de um usuário

Pergunta: "Que ações a alice fez esta semana?"

1. Em **Operação -> Histórico**, filtre por **Usuário = alice** e **Data = últimos 7 dias**.
2. Revise todas as ações — por exemplo, 3 edições de mapeamento, 2 reprocessamentos da fila de reenvio e 1 edição de rota.

### Investigar um possível incidente

Pergunta: "Quando a integração 'Backdoor Defender' foi criada?"

1. Em **Operação -> Histórico**, filtre por **Tipo = integração** e busque pelo nome.
2. Veja a criação — por exemplo, em 25/04/2026 às 14:32, pelo usuário "bob".
3. Compare a data/hora com os alertas do seu SIEM. Uma integração criada durante um ataque pode indicar acesso indevido.

### Auditar acesso por cliente (compliance)

Pergunta: "Quem acessou os dados do cliente X?"

1. Em **Operação -> Histórico**, filtre pela organização "cliente-x".
2. Veja todas as ações relacionadas a esse cliente.
3. Exporte o relatório para o time de GRC.

## Exportar registros

Use o botão **Download** para exportar o resultado filtrado em:

- **CSV** — para abrir em planilha (Excel, Google Sheets).
- **JSON** — para integrar com outras ferramentas.
- **PDF** — relatório formatado, ideal para impressão ou compliance.

O arquivo inclui: data/hora, usuário, tipo, ação, recurso e detalhes. Útil para entregar a auditor externo, montar o relatório mensal de compliance ou anexar como evidência em uma investigação.

## Retenção

- Os registros ficam disponíveis por **365 dias**.
- Após esse período, são removidos **automaticamente** — você não precisa fazer nada.
- **Exceção:** eventos de sistema (gerados automaticamente, sem um usuário responsável) são mantidos por 7 anos, por exigência legal.

A política de retenção é automática e fixa; não há configuração ou limpeza manual a fazer pelo usuário.

## Quem vê o quê

| Papel | Pode ver |
|-------|----------|
| **Viewer** | Tudo (somente leitura), exceto os logs de administração (criação de usuário, mudanças de organização). |
| **Operator** | Tudo do Viewer, mais seus próprios descartes e reprocessamentos. |
| **Engineer** | Tudo do Operator, mais as edições de mapeamento. |
| **Admin** | Tudo, incluindo gestão de usuários, eventos de sistema e auditoria de destinos, rotas e fila de reenvio. |

Cada usuário vê apenas os registros da sua própria organização. O administrador vê os registros de todas.

## Limitações

- **Pequeno atraso:** uma ação costuma aparecer no Histórico em menos de 1 minuto.
- **Uma ação, um registro:** operações em massa geram um registro por item afetado.
- **Dados pessoais:** os registros podem conter nome de usuário e e-mail. Mesmo diante de um pedido de exclusão de dados (GDPR), a trilha de auditoria **não é apagada**, por exigência de compliance.
- **Credenciais:** os registros de destinos e credenciais **nunca** incluem tokens ou senhas — apenas indicam se uma credencial existe.

## Próximos passos

- **Precisa investigar uma mudança?** Use os filtros em **Operação -> Histórico**.
- **Precisa de um relatório de compliance?** Filtre o período e clique em **Download -> PDF**.
- **Trabalhando com destinos ou rotas?** Veja [Destinos](../outputs/destinations.md) e [Roteamento e redação de PII](../outputs/routing.md).
