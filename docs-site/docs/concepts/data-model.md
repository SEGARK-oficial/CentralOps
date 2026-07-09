---
sidebar_position: 3
title: Como os dados se organizam
description: Os principais elementos que você manipula no CentralOps (organizações, integrações, destinos, eventos) e como eles se conectam.
---

# Como os dados se organizam

O CentralOps coleta eventos de segurança dos seus produtos (Sophos, Wazuh, Microsoft Defender e outros), padroniza esses eventos em um formato único, decide para onde cada um deve ir e entrega a vários destinos ao mesmo tempo. Esta página explica, em linguagem de produto, os principais elementos que você vê e manipula na interface — e onde encontrar cada um no menu lateral.

Você não precisa saber nada sobre banco de dados para usar o CentralOps. O objetivo aqui é dar um mapa mental: o que é cada coisa, para que serve e em qual tela você mexe nela.

## Quando usar

Consulte esta página quando:

- Você é novo na plataforma e quer entender os termos que aparecem nos menus (integração, destino, rota, mapping, quarentena, drift) antes de começar a operar.
- Um evento "sumiu" e você precisa saber por onde ele passa — da coleta na integração, pela normalização, até a entrega no destino — para descobrir em que etapa parou.
- Você vai planejar o atendimento de um novo cliente (cenário MSSP) e precisa entender como o isolamento por organização separa os dados de cada um.

## Os elementos principais

### Organização

Uma organização separa completamente os dados de cada cliente ou unidade. Quem opera dentro da organização A nunca enxerga dados da organização B, mesmo que ambas usem a mesma instalação do CentralOps.

- Em uma operação interna, normalmente há uma única organização.
- Em uma operação de MSSP (atendendo vários clientes), cria-se uma organização por cliente.

Onde mexer: menu **Visão geral -> Organizações** (visível apenas para administradores). Ali o administrador cria, renomeia e gerencia as organizações.

### Hierarquia de tenants

Quando você trabalha com múltiplos clientes, é possível organizar as organizações em uma hierarquia (árvore) para refletir relacionamentos comerciais — por exemplo, um reseller que atende vários clientes finais.

Nessa hierarquia:

- Uma organização pode ser um **cliente final** (folha da árvore) ou um **reseller** (ramo que tem filhos). Um reseller é autorizado pelo administrador da plataforma para atender clientes próprios.
- Cada relação é **unidirecional**: uma organização tem um organizador pai (exceto a raiz), formando uma corrente hierárquica.
- Um **reseller pode ter um teto de clientes** — o número máximo de organizações filhas permitidas. Se não há limite, o reseller pode criar quantos clientes precisar.
- **Papéis e permissões** são atribuídos por organização e respeitam a hierarquia: um administrador de reseller enxerga sua própria organização e todos os clientes abaixo dela (sua subárvore), mas não enxerga organizações irmãs ou acima dela. Um cliente final (folha) é administrado localmente e não tem visibilidade sobre outras organizações.

Essa separação hierárquica mantém os dados isolados mesmo dentro de uma árvore: cada organização permanece um container seguro de dados. O que muda é a **visibilidade operacional** — um reseller pode monitorar e gerenciar seus clientes de forma centralizada.

Onde mexer: a hierarquia e os limites são gerenciados no menu **Visão geral -> Organizações** (somente administradores globais ou resellers com permissão).

### Integração

Uma integração é a conexão com um produto de origem — é por ela que o CentralOps coleta os eventos. Cada integração guarda as credenciais daquele produto e o estado da coleta (até onde já leu, qual foi a última execução, se está saudável).

O que você vê em cada integração:

- O produto de origem (por exemplo, Sophos, Wazuh, Microsoft Defender).
- Se está ativa ou pausada.
- O status de saúde mais recente e o horário da última coleta.

As credenciais ficam guardadas de forma criptografada. Você nunca vê senhas, tokens ou segredos em texto puro na interface.

Onde mexer: menu **Visão geral -> Integrações**.

### Destino

Um destino é um lugar para onde o CentralOps entrega os eventos já padronizados — por exemplo, um SIEM, um data lake ou um sistema externo. Você pode ter vários destinos por organização e enviar o mesmo evento simultaneamente para todos eles (envio simultâneo a vários destinos).

Exemplos de destinos suportados:

- Wazuh (destino padrão de quem migrou da configuração antiga).
- Splunk.
- Elasticsearch.
- Armazenamento em nuvem (data lake).
- Microsoft Sentinel.
- Filas de mensageria.
- Coleta no padrão OpenTelemetry.

Cada destino tem seu próprio histórico de alterações e uma trilha de auditoria: quem mudou o quê e quando. As credenciais de cada destino também ficam criptografadas e nunca aparecem em texto puro — nem na tela, nem no histórico de mudanças.

Onde mexer: menu **Operação -> Destinos** (visível apenas para administradores).

### Rota (regra de roteamento)

Uma rota é a regra que decide quais destinos recebem cada evento. Você define uma condição (por exemplo, "eventos de severidade alta do Sophos") e quais destinos devem recebê-los.

Como as rotas são avaliadas:

- Cada rota tem uma prioridade. As de prioridade menor são avaliadas primeiro.
- Uma rota pode encerrar a avaliação no primeiro acerto ou clonar o evento e continuar avaliando outras rotas (assim o mesmo evento chega a vários destinos).
- É possível liberar uma rota gradualmente para uma fração dos eventos, para testar antes de aplicá-la a tudo.
- Se nenhuma rota estiver ativa, todos os eventos caem em um destino de segurança padrão. Assim nada se perde silenciosamente.

Onde mexer: menu **Operação -> Roteamento** (visível apenas para administradores). Para visualizar como os eventos estão fluindo da coleta até a entrega, use o menu **Operação -> Fluxo de dados** (também só para administradores).

### Mapping (mapeamento de campos)

Um mapping é a regra que traduz os campos de um produto de origem para o formato padronizado do CentralOps. Cada combinação de produto e tipo de evento (por exemplo, alertas do Sophos, eventos de segurança do Wazuh) tem o seu mapping.

Pontos importantes:

- Cada mapping mantém um histórico de versões. Uma versão está sempre em uso (a atual) e as anteriores ficam guardadas.
- Ao editar um mapping, uma nova versão é criada e passa a ser a atual; a anterior vai para o histórico.
- Você pode voltar para uma versão anterior com um clique.
- Versões antigas nunca são alteradas, apenas guardadas — isso garante uma trilha confiável.

Onde mexer: menu **Normalização -> Mappings**.

### Eventos brutos e eventos normalizados

Estes dois conceitos ajudam a entender o caminho de um evento, mesmo que você raramente os manipule diretamente:

- O evento bruto é o evento como chegou do produto de origem, antes de qualquer tratamento. Normalmente ele não aparece na interface — você só o vê quando algo falha e o evento vai para a quarentena.
- O evento normalizado é o evento já traduzido pelo mapping para o formato único, pronto para ser entregue aos destinos. Só eventos processados com sucesso geram um evento normalizado.

Cada destino registra, em seu próprio histórico, quando recebeu o evento. Você acompanha esse caminho no menu **Operação -> Fluxo de dados** (somente administradores) e revê o que já passou no menu **Operação -> Histórico**.

### Quarentena

A quarentena guarda os eventos que falharam na validação ou na tradução — por exemplo, quando falta um campo obrigatório ou um valor vem em formato inesperado. Cada item mostra a mensagem do erro, o tipo do problema e quando aconteceu.

O que você pode fazer com um item em quarentena:

- Descartar: marca como ignorado; o evento não é reprocessado.
- Reprocessar: depois de corrigir o mapping, devolve o evento à fila para ser tratado de novo.

Os itens em quarentena ficam guardados por um período limitado e depois são removidos automaticamente.

Onde mexer: menu **Normalização -> Quarentena**.

### Drift (campos novos detectados)

O drift mostra campos que apareceram nos eventos de um produto mas que ainda não estão tratados no mapping. Isso acontece, por exemplo, quando um fornecedor passa a enviar um campo novo. Cada item indica o produto, o nome do campo, quantas vezes foi visto e quando foi visto pela última vez.

O que você pode fazer com um campo detectado:

- Ignorar: para de avisar sobre aquele campo.
- Marcar como tratado: indica que o campo já está sendo cuidado no mapping.

Onde mexer: menu **Normalização -> Drift Explorer**.

### Saúde do pipeline

A saúde do pipeline reúne, em um só lugar, indicadores de como o fluxo está se comportando: o que está sendo coletado, o que está sendo entregue e onde há problemas. Use essa tela como ponto de partida quando algo parece fora do normal.

Onde mexer: menu **Normalização -> Saúde do Pipeline**.

### Job de consulta (QueryJob)

Um job de consulta é uma busca federada que você dispara manualmente ou via regra de correlação. Ele consulta dados em múltiplas fontes de segurança (Wazuh, Sophos, CrowdStrike, Defender e outras) de forma síncrona, filtrando por janela de tempo, e acompanha o progresso de forma assíncrona.

O que você vê em cada job:

- Um identificador único e opaco (job_id).
- Status atual: aguardando (submitted), em execução (running), finalizado (finished), parcialmente processado (partial) ou com erro (failed).
- Total de resultados encontrados.
- Resultados separados por fonte — cada fonte mostra seu próprio resultado (per_source facets).

O que você pode fazer:

- Disparar uma nova busca federada selecionando as fontes, escrevendo uma consulta e escolhendo a janela de tempo.
- Acompanhar o progresso em tempo real — não precisa ficar esperando a conclusão em uma tela; você pode voltar depois via o identificador.

Onde mexer: menu **Operação -> Busca federada**.

### Detecção (Detection)

Uma detecção é um achado de segurança gerado automaticamente — pode vir de uma consulta que você executou, de uma regra de correlação que roda continuamente, ou de uma fonte externa (como um alert do Sophos).

O que você vê em cada detecção:

- Origem: se veio de uma consulta que você rodou (scheduled_query), de uma busca ao vivo (live_query) ou de uma regra de correlação (correlation).
- Severidade em padrão OCSF (ex.: Alta, Média, Baixa). O padrão é Alto.
- Status: aberta (open) esperando ação, reconhecida (ack) ou fechada (closed) após investigação.
- Quantidade de ocorrências agregadas (count) e uma chave de deduplicação (dedup_key).

O que você pode fazer:

- Filtrar por status — mostrar só o que está aberto, reconhecido ou fechado.
- Mudar o status de uma detecção — abrir, reconhecer ou fechar.
- Investigar o evento associado consultando os destinos onde ele chegou.

Onde mexer: menu **Operação -> Detecções**.

### Regra de correlação (CorrelationRule)

Uma regra de correlação dispara uma detecção quando um padrão de eventos ocorre. Por exemplo, pode gerar um alerta se "mais de 5 falhas de login no mesmo usuário dentro de 5 minutos".

Como funciona:

- Define-se um nome descritivo, uma janela de tempo (em segundos) e um campo para agrupar os resultados (ex.: usuário).
- Escreve-se uma ou mais condições (ex.: "campo 'event_type' é igual a 'login_failure'").
- Define-se o mínimo de ocorrências para disparar a regra (padrão: 5).
- Opcionalmente, silencia detecções duplicadas por um período (ex.: 1 hora).

O que você pode fazer:

- Criar uma nova regra manualmente.
- Ativar ou desativar uma regra.
- Salvar histórico e versões da regra.
- Abrir uma detecção que foi disparada por uma correlação para investigar.

Onde mexer: menu **Conhecimento -> Correlação**.

### Histórico e trilha de auditoria

O CentralOps registra as ações realizadas — quem fez, o que foi feito, quando e em qual organização — para rastreabilidade e conformidade. Mudanças em rotas e em destinos também ficam registradas, o que permite responder perguntas como "quem alterou esta rota e quando?" e desfazer mudanças voltando a um estado anterior.

Onde acompanhar: menu **Operação -> Histórico**.

### Sua sessão e seus acessos

Quando você entra na plataforma, é criada uma sessão de uso que expira depois de um tempo, exigindo novo login. Senhas e tokens nunca são guardados em texto puro.

Onde mexer: a gestão de pessoas e acessos fica em **Administração -> Usuários** e **Administração -> Service Accounts**. Para gerar credenciais de uso programático da sua própria conta, vá em **Sua conta -> Tokens de API**.

## Como tudo se conecta

Em alto nível, o caminho de um evento é:

1. A **integração** coleta o evento do produto de origem.
2. O **mapping** traduz o evento para o formato padronizado.
   - Se a tradução falha, o evento vai para a **quarentena**.
   - Se aparecem campos não tratados, eles surgem no **drift**.
3. As **rotas** decidem para quais **destinos** o evento será entregue.
4. Cada **destino** entrega o evento e registra o que recebeu.

Tudo isso acontece dentro de uma **organização**, que mantém os dados de cada cliente ou unidade completamente separados.

Para ver esse caminho em funcionamento, em tempo real, use o menu **Operação -> Fluxo de dados** (somente administradores).

## Retenção: por quanto tempo os dados ficam guardados

Para que a plataforma não acumule dados indefinidamente, cada tipo de informação tem um prazo de retenção e é limpo automaticamente depois. Em linhas gerais:

| Tipo de dado | Permanece guardado por |
|---|---|
| Eventos brutos e normalizados | Período curto (alguns dias) |
| Itens em quarentena | Alguns dias |
| Campos novos detectados (drift) | Alguns meses |
| Histórico e trilhas de auditoria | Mais longo, por exigência de conformidade |
| Sua sessão de uso | Algumas horas, até precisar logar de novo |

Os prazos exatos de retenção são definidos pela equipe de infraestrutura no momento do deploy. Se precisar ajustá-los para a sua organização, fale com o administrador da plataforma.

## Sobre segurança das credenciais

Você nunca precisa lidar com chaves ou segredos diretamente. Credenciais de integrações e de destinos são guardadas de forma criptografada e nunca aparecem em texto puro — nem nas telas, nem nos históricos de mudança. A forma exata como essa proteção é configurada é responsabilidade da equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma.

## Limites

Alguns limites valem a pena conhecer:

- A quantidade de usuários, destinos, rotas e mappings por organização é, na prática, livre.
- Há um teto para a quantidade de integrações por organização. Se você se aproximar desse limite, fale com o administrador da plataforma.
- Itens em quarentena e eventos têm prazo de retenção (veja a seção acima).

Estes limites são definidos no momento do deploy. Se precisar de um valor diferente do que aparece na sua instalação, fale com o administrador da plataforma.

## Veja também

- [Retenção](../compliance/retention.md) — detalhes sobre por quanto tempo cada tipo de dado é mantido.
