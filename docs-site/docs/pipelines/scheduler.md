---
sidebar_position: 2
title: Agendamento de coletas
description: Como o CentralOps coleta eventos automaticamente e como pausar ou retomar a coleta de uma integração
---

# Agendamento de coletas

O CentralOps coleta eventos dos seus fornecedores de segurança automaticamente, em intervalos regulares. Você não precisa apertar nenhum botão para que uma integração ativa continue trazendo dados: a plataforma busca novos eventos sozinha, de forma contínua e em segundo plano.

Esta página explica o que é esse agendamento, como acompanhar o status da coleta e como pausar ou retomar a coleta de uma integração quando precisar.

## Quando usar

- **Janela de manutenção no fornecedor.** O portal do fornecedor (por exemplo Sophos ou Microsoft Defender) vai entrar em manutenção e você quer evitar erros de coleta no período. Pause a integração e retome quando a manutenção terminar.
- **Investigar ruído ou custo.** Uma integração começou a gerar um volume inesperado de eventos e você quer interromper a entrada de dados enquanto analisa a causa, sem remover a integração.
- **Verificar uma integração recém-criada.** Você acabou de conectar um fornecedor e quer confirmar que a primeira coleta aconteceu e que os eventos estão chegando antes de seguir com a configuração de roteamento e mapeamentos.

## Como a coleta automática funciona

Assim que você cria e ativa uma integração, o CentralOps passa a buscar novos eventos dela em ciclos regulares. Cada fornecedor tem um ritmo de coleta próprio, definido pela plataforma para equilibrar atualidade dos dados e estabilidade. Em geral:

- Fornecedores de alta prioridade (como alertas de Sophos e incidentes de Defender) são consultados com mais frequência, na faixa de poucos minutos.
- Fontes mais pesadas ou de menor urgência são consultadas em intervalos maiores.

A frequência de coleta de cada fornecedor é definida pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma.

A cada coleta, a plataforma guarda o ponto onde parou, para que o próximo ciclo continue de onde o anterior terminou e não traga eventos repetidos.

## Acompanhar o status da coleta

Para ver se uma integração está coletando normalmente:

1. Abra o menu **Visão geral -> Integrações**.
2. Localize a integração na lista e observe o indicador de status e a hora da última coleta.

O que os sinais indicam:

| Sinal na tela | O que significa | O que fazer |
|---|---|---|
| Coleta recente, sem erros | A integração está saudável e trazendo eventos no intervalo esperado. | Nada. |
| "Aguardando primeira coleta" | A integração foi criada há pouco e ainda não completou o primeiro ciclo. | Aguarde alguns minutos e atualize a tela. |
| Sem coleta recente / coleta atrasada | Os ciclos pararam de rodar no ritmo esperado. | Veja a seção de problemas abaixo. |
| Erro de autenticação ou de conexão | As credenciais do fornecedor expiraram ou a conexão falhou. | Revise as credenciais da integração na própria tela de **Integrações**. |

Para uma visão mais completa da saúde do processamento (não só da coleta), use o menu **Normalização -> Saúde do Pipeline**.

## Pausar a coleta de uma integração

Pausar interrompe os ciclos de coleta daquela integração sem apagá-la. A configuração e as credenciais são preservadas.

1. Abra o menu **Visão geral -> Integrações**.
2. Clique na integração que deseja pausar.
3. Use a ação de **pausar** a coleta na tela da integração.

Enquanto estiver pausada, a integração para de buscar novos eventos. Os eventos já coletados continuam disponíveis normalmente.

## Retomar a coleta

1. Abra o menu **Visão geral -> Integrações**.
2. Clique na integração pausada.
3. Use a ação de **retomar** a coleta.

A coleta volta a acontecer nos intervalos normais em poucos instantes. O primeiro ciclo após retomar busca os eventos acumulados desde a pausa, sem trazer duplicados.

## Queries agendadas

Além da coleta automática dos fornecedores, administradores podem programar **queries que rodam em horários definidos** — por exemplo, uma busca de "logins de alto risco" executada a cada duas horas. Queries agendadas são **vendor-neutras**: rodam via dialeto nativo de cada integração (OpenSearch DSL para Wazuh, FQL para CrowdStrike, KQL para Defender, etc.), sem amarração a um fornecedor específico.

Quando uma query agendada encontra resultados, ela gera uma **Detecção de 1ª classe** — um alerta consultável, com status de triagem (aberto, reconhecido ou fechado), severidade configurável e deduplicação automática de ruído. Além disso, você pode ativar notificações por e-mail para resultados imediatos.

Para criar ou gerenciar essas execuções programadas, use o menu **Conhecimento → Agendamentos** (disponível apenas para administradores). Os alertas produzidos por queries agendadas seguem as regras de roteamento configuradas — veja a página de Roteamento para entender como direcioná-los aos destinos certos. Para revisar as Detecções geradas, acesse o menu **Operação → Detecções**.

### Acompanhar a saúde de um agendamento

Cada query agendada tem um indicador de **saúde** que mostra se está funcionando:

| Status | O que significa |
|--------|-----------------|
| **Healthy** | A query rodou com sucesso no último ciclo. |
| **Degraded** | A query falhou uma ou poucas vezes, mas segue agendada (ex.: rate limit temporário). |
| **Failing** | A query falha consecutivamente (ex.: credenciais expiradas, query inválida). |

Se uma query agendada ficar em **Failing**, revise as credenciais da integração ou a sintaxe da busca. Um alerta Com falhas consecutivas não gera Detecções — ela está silenciosa e requer atenção.

### Isolamento de organização

Quando uma query agendada encontra resultados e tem a flag **notificar por e-mail** ativada (campo `notify_on_results`), o CentralOps envia a notificação apenas aos **e-mails cadastrados na organização da query**. Uma query do tenant A não notifica e-mails da organização B — isso garante que um resultado não vaze entre tenants em ambientes multi-tenant.

## Resolução de problemas

### Uma integração aparece como "Sem coleta recente"

1. Abra **Visão geral -> Integrações** e confirme se a integração não está **pausada**. Se estiver, retome a coleta.
2. Verifique se há aviso de erro de autenticação ou conexão na integração. Se houver, revise as credenciais do fornecedor.
3. Abra **Normalização -> Saúde do Pipeline** para checar se o atraso afeta só essa integração ou o processamento como um todo.
4. Se a integração continuar sem coletar mesmo ativa e com credenciais válidas, acione o administrador da plataforma ou o suporte.

### A primeira coleta não acontece após criar a integração

A primeira coleta pode levar alguns minutos. Atualize a tela de **Integrações** e confirme o status. Se passar bastante tempo ainda em "Aguardando primeira coleta", revise as credenciais da integração e, persistindo, acione o administrador da plataforma.

### Eventos chegam muito atrasados

Quando muitos eventos chegam de uma vez, pode haver um pequeno atraso de processamento. Acompanhe em **Normalização -> Saúde do Pipeline**; o atraso costuma se normalizar sozinho. Se persistir, fale com o administrador da plataforma.

## Boas práticas

- Para interromper temporariamente uma fonte, **pause a integração** em vez de removê-la — assim você preserva a configuração e retoma com um clique.
- Antes de pausar, confirme com sua equipe que a fonte pode ficar sem coletar no período, para não criar lacunas em investigações.
- Use **Normalização -> Saúde do Pipeline** como painel diário para identificar cedo qualquer integração que pare de coletar.

## Próximos passos

- **Gerenciar integrações e credenciais:** menu **Visão geral -> Integrações**.
- **Programar queries recorrentes (admin):** menu **Conhecimento -> Agendamentos**.
- **Acompanhar a saúde do processamento:** menu **Normalização -> Saúde do Pipeline**.
- **Direcionar eventos aos destinos certos (admin):** veja a página de Roteamento.
