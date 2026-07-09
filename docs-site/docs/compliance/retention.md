---
sidebar_position: 1
title: Retenção de dados
description: Defina por quanto tempo cada tipo de dado fica guardado antes de ser apagado automaticamente.
---

# Retenção de dados

A retenção de dados define por quanto tempo cada tipo de dado fica guardado na plataforma antes de ser apagado automaticamente. Você configura prazos diferentes para cada tipo de dado (eventos em quarentena, campos novos detectados, histórico, resultados de busca) e a plataforma faz a limpeza sozinha, todos os dias.

Quem opera só pela interface não precisa rodar nenhuma rotina manual: basta definir os prazos e a plataforma cuida do resto.

## Quando usar

- **Cumprir política de descarte da sua organização (LGPD/GDPR)**: sua empresa exige que dados operacionais sejam apagados após um prazo definido. Você ajusta os dias de cada tipo de dado para ficar dentro da política.
- **Reduzir custo de armazenamento**: dados antigos que ninguém mais consulta continuam ocupando espaço. Encurtar o prazo de quarentena ou de resultados de busca libera espaço e reduz custo.
- **Limitar a janela de exposição de dados sensíveis**: quanto menos tempo um evento sensível fica armazenado, menor o risco em caso de incidente. Você reduz o prazo para diminuir essa janela.

## Política padrão

Toda organização nova começa com estes prazos. Eles servem para a maioria dos casos e podem ser ajustados (exceto os logs de auditoria, que têm mínimo obrigatório).

| Tipo de dado | Retenção padrão | Por quê |
|---|---|---|
| **Quarentena** | 7 dias | Eventos que falharam no processamento; histórico longo raramente é útil. |
| **Campos novos detectados (drift)** | 90 dias | Descoberta de novos campos; menos urgente, vale manter por mais tempo. |
| **Histórico** | 30 dias | Registro de atividades da plataforma. |
| **Resultados de busca** | 7 dias | Cache de buscas; pode ser recalculado a qualquer momento. |
| **Logs de auditoria** | 365 dias (mínimo fixo) | Exigência de compliance (LGPD/GDPR): pelo menos 1 ano, e não podem ser apagados antes disso. |

## Configurar a retenção de uma organização

Cada organização pode ter seus próprios prazos, sobrescrevendo os padrões. Esta configuração é de administrador.

### Passo a passo

1. Abra o menu **Visão geral → Organizações**.
2. Clique na organização que deseja ajustar.
3. Abra a área de retenção da organização.
4. Informe os dias de cada tipo de dado:

   | Campo | Limites |
   |---|---|
   | Quarentena (dias) | entre 1 e 3650 |
   | Campos novos detectados (dias) | entre 1 e 3650 |
   | Histórico (dias) | entre 1 e 3650 |
   | Resultados de busca (dias) | entre 1 e 3650 |
   | Logs de auditoria (dias) | fixo em pelo menos 365; não pode ser reduzido |

5. Clique em **Salvar**.

### Regras de validação

- O mínimo é **1 dia** e o máximo é **3650 dias** (10 anos).
- Os **logs de auditoria nunca podem ficar abaixo de 365 dias**, por exigência de compliance.

Os novos prazos passam a valer na próxima limpeza automática (uma vez por dia). Não é preciso fazer mais nada.

## Como a limpeza acontece

A plataforma roda uma limpeza automática **uma vez por dia, em segundo plano**. Em cada execução, ela apaga os dados que já passaram do prazo configurado para cada tipo. Você não precisa iniciar nada manualmente.

A limpeza é rápida e não interrompe o uso da plataforma: você continua buscando, investigando e operando normalmente enquanto ela ocorre.

## Retenção nos destinos de entrega

Além das prazos internos, cada **destino** para onde os dados são entregues pode ter sua própria política de retenção, independente da plataforma. Isso permite manter cópias com prazos diferentes em lugares diferentes.

| Tipo de destino | Quem controla a retenção |
|---|---|
| **Armazenamento de longa duração** (object-store / S3, usado como cópia "fria" e completa) | A plataforma aplica a limpeza automática com base nos dias configurados para aquele destino. |
| **SIEMs e barramentos** (Sentinel, Kafka, Splunk, Elastic, syslog, etc.) | O próprio sistema de destino gerencia a retenção do seu lado. A plataforma não apaga dados lá. |

### Padrão comum: cópia curta no SIEM, cópia longa no armazenamento

Um arranjo típico usa o **Roteamento** para enviar o mesmo dado a destinos com prazos diferentes:

- Eventos mais críticos vão para um **SIEM** com retenção curta (por exemplo, 30 dias), para análise imediata.
- Os demais eventos vão para um **armazenamento de longa duração** com retenção longa (por exemplo, 2 anos), como arquivo histórico.

Para montar esse arranjo, configure as regras na tela de **Roteamento** e o prazo de cada destino na tela de **Destinos**. A remoção de dados sensíveis (redação de PII) também é configurada por rota — veja [Roteamento](../outputs/routing.md).

> Para definir o prazo de retenção de um destino de armazenamento, abra **Operação → Destinos**, selecione o destino e ajuste o prazo de retenção. Destinos sem essa opção (SIEMs, barramentos) gerenciam a retenção pelo próprio lado.

## Conferir que a política está ativa e funcionando

### Confirmar os prazos configurados

1. Abra **Visão geral → Organizações** e selecione a organização.
2. Verifique os dias mostrados na área de retenção. São esses os prazos que a limpeza diária aplica.

### Ver quem alterou a retenção e quando

Toda mudança de prazo fica registrada. Para auditar:

1. Abra **Operação → Histórico**.
2. Localize os registros de alteração da configuração de retenção. Cada registro mostra **quem** alterou, **qual organização** e os **novos valores**.

### Se dados antigos ainda aparecem

Se você esperava que certos dados já tivessem sumido e eles continuam aparecendo:

1. **Confira o prazo configurado** em **Visão geral → Organizações**. Um prazo maior do que o esperado (por exemplo, 365 em vez de 30 dias) faz os dados ficarem mais tempo. Ajuste e salve.
2. **Lembre-se de que a limpeza roda uma vez por dia.** Dados que acabaram de vencer só somem na próxima execução diária.
3. **Para objetos antigos em um destino de armazenamento**, confirme em **Operação → Destinos** que aquele destino tem um prazo de retenção definido. Sem prazo definido, a plataforma não apaga nada lá.
4. Se mesmo assim os dados persistirem além do prazo, fale com o administrador da plataforma.

## Retenção e compliance

### LGPD / GDPR

- **Direito ao esquecimento**: ao atender a um pedido de exclusão, os dados são removidos rapidamente, tanto na plataforma quanto nos destinos de armazenamento que a plataforma controla. Veja [LGPD/GDPR](./lgpd-gdpr.md).
- **Trilha de auditoria**: os logs de auditoria não podem ser apagados antes de 1 ano e ficam protegidos contra alteração.
- **Várias cópias, vários prazos**: cada destino tem sua própria janela de retenção. Nos destinos de armazenamento, o prazo é o que você configurou; nos SIEMs e barramentos, a retenção é responsabilidade do próprio sistema de destino.
- **Residência de dados**: destinos podem ser marcados com a região onde os dados podem ficar (por exemplo, "UE" ou "EUA"). Eventos não são entregues a destinos com região incompatível. Essa marcação é definida pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma.

### PCI DSS

- **Dados de portador de cartão**: caso existam (raro neste contexto), a retenção mínima é de 1 ano.
- **Retenção de logs**: 1 ano, conforme o padrão.

## Próximos passos

- **Configurar prazos de uma organização?** Veja [Organizações](../administration/organizations.md).
- **Atender a um pedido de exclusão (LGPD/GDPR)?** Veja [LGPD/GDPR](./lgpd-gdpr.md).
- **Destinos e roteamento?** Veja [Destinos](../outputs/destinations.md) e [Roteamento](../outputs/routing.md).
- **Auditar alterações?** Veja [Histórico](../operations/history-audit.md).
