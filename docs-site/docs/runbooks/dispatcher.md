---
sidebar_position: 1
title: Entrega aos destinos
description: O que fazer quando os eventos param de chegar a um destino (Wazuh, Splunk, S3, Kafka, etc.) ou a fila de reenvio começa a encher.
---

# Entrega aos destinos

Depois que um evento é coletado e normalizado, o CentralOps o envia aos destinos configurados — Wazuh, Splunk, Elastic, S3, Sentinel, Kafka, OTLP, syslog, JSONL. Cada destino tem regras de roteamento, uma proteção contra destino instável e uma fila de reenvio para o que falha. Esta página ajuda você a diagnosticar e resolver problemas de entrega usando apenas a interface web.

:::note
As telas **Operação → Destinos**, **Operação → Roteamento** e **Operação → Fluxo de dados** só aparecem para administradores da plataforma. As telas **Saúde do Pipeline** e **Quarentena** (em **Normalização**) ficam disponíveis para operadores.
:::

## Quando usar

Use esta página quando perceber qualquer um destes cenários:

- **Eventos pararam de chegar a um destino** — por exemplo, o Splunk ou o Wazuh deixou de receber alertas, mas a coleta continua normal.
- **Um destino aparece em vermelho na Saúde do Pipeline** — sinal de que a entrega para aquele destino está bloqueada.
- **A fila de reenvio está enchendo** — eventos que falharam ao serem entregues estão se acumulando e podem chegar à Quarentena.

## Passo a passo: nenhum evento chega a nenhum destino

Quando parece que nada está sendo entregue, confirme em qual etapa o problema está.

### 1. Os eventos estão sendo coletados?

Abra o menu **Visão geral → Dashboard** e observe os indicadores de eventos normalizados recentes.

- **Há eventos recentes**: a coleta está funcionando. Siga para o passo 2.
- **Nenhum evento recente**: o problema é de coleta, não de entrega. Verifique a tela **Operação → Collectors**.

### 2. Como estão os destinos?

Abra o menu **Normalização → Saúde do Pipeline** e observe o status de cada destino:

| Cor | Significado | O que fazer |
|-----|-------------|-------------|
| Verde | Conectado e entregando normalmente. | Nada a fazer. |
| Amarelo | Conectado, mas com lentidão ou fila cheia. | Acompanhe; pode normalizar sozinho. Se persistir, veja a seção do destino abaixo. |
| Vermelho | Entrega bloqueada (proteção contra destino instável ativada) ou falha crítica. | Vá para a seção do destino específico mais abaixo. |

### 3. Todos os destinos estão vermelhos ou inativos?

Quando todos os destinos aparecem com problema ao mesmo tempo, normalmente é uma falha geral de entrega, e não de um destino isolado.

- Confirme no **Dashboard** se os eventos continuam sendo coletados e normalizados.
- Confira na **Saúde do Pipeline** se a indisponibilidade afeta todos os destinos ou só alguns.
- O processamento em segundo plano que entrega os eventos é mantido pela equipe de infraestrutura. Se nenhum destino estiver recebendo eventos e a coleta estiver normal, fale com o administrador da plataforma para que ele verifique o serviço de entrega.

## Um destino específico está falhando

Quando apenas um destino aparece em vermelho na **Saúde do Pipeline** e os demais seguem verdes, o problema está naquele destino. Abra **Operação → Destinos** (admin) para revisar a configuração e as credenciais do destino afetado.

### Wazuh

**Sintoma**: o destino Wazuh aparece em vermelho; os eventos chegam ao pipeline, mas não ao Wazuh.

Causas e ações mais comuns:

| Causa provável | O que fazer |
|----------------|-------------|
| Conexão de rede bloqueada entre o CentralOps e o Wazuh. | Esta conectividade é definida pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma. |
| Certificado de segurança inválido ou expirado. | Esta configuração é definida pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma. |
| Wazuh fora do ar. | Verifique a disponibilidade do próprio Wazuh; ele é um sistema externo ao CentralOps. |
| Proteção contra destino instável ativada após várias falhas. | Aguarde alguns segundos: o CentralOps tenta reconectar automaticamente assim que o destino volta. |

### Splunk

**Sintoma**: eventos caindo na fila de reenvio com mensagens como "token inválido" ou erro 4xx.

| Causa provável | O que fazer |
|----------------|-------------|
| Token de acesso inválido ou expirado (401/403). | Gere um novo token no Splunk e atualize-o na tela **Operação → Destinos** (admin), editando o destino Splunk. |
| Formato de evento rejeitado (400). | Verifique o mapeamento daquele fluxo em **Normalização → Mappings**. |
| Splunk sobrecarregado (429/503). | Costuma normalizar sozinho. Se persistir, ajuste o limite de envio do destino em **Operação → Destinos** (admin). |
| Tempo de resposta esgotado. | Ajuste o tempo limite do destino em **Operação → Destinos** (admin). |

### Elastic, S3, Kafka, Sentinel, OTLP e syslog

O diagnóstico é semelhante para os demais destinos:

| Causa provável | O que fazer |
|----------------|-------------|
| Credenciais inválidas. | Atualize a credencial do destino em **Operação → Destinos** (admin). |
| Endpoint indisponível ou inacessível. | Verifique se o serviço de destino está no ar. A conectividade de rede é responsabilidade da equipe de infraestrutura. |
| Limite de taxa atingido (429/503). | Costuma normalizar sozinho. Se persistir, ajuste o limite de envio do destino em **Operação → Destinos** (admin). |
| Recurso de destino não existe (bucket, tópico, índice). | Crie o recurso no serviço de destino (por exemplo, o bucket no S3 ou o tópico no Kafka) e tente novamente. |

## A fila de reenvio está enchendo

Quando a entrega falha repetidamente, os eventos vão para a fila de reenvio e, em seguida, podem aparecer em **Normalização → Quarentena**. Uma fila crescendo é sinal de que algum destino não está aceitando os eventos.

### Diagnóstico

1. Abra **Normalização → Saúde do Pipeline** e identifique qual destino está em vermelho ou amarelo.
2. Decida se a falha é temporária (uma instabilidade de rede que se resolve sozinha) ou permanente (credencial errada, recurso inexistente).
3. Abra **Normalização → Quarentena** para ver os eventos retidos e o motivo da falha de cada um.

### Ações

- **Falhas concentradas em um destino**: resolva a causa daquele destino seguindo a seção [Um destino específico está falhando](#um-destino-específico-está-falhando).
- **Pausar a entrega para um destino problemático** (admin): em **Operação → Roteamento**, desative a regra que envia para o destino com problema enquanto investiga. Assim você evita que a fila continue crescendo.
- **Reprocessar eventos retidos**: depois de corrigir a causa, reprocesse os eventos em **Normalização → Quarentena**. O reprocessamento é uma ação manual e deliberada — a fila de reenvio existe para investigação, não para repetição automática.

:::caution
A capacidade do processamento em segundo plano e os limites de memória da fila são definidos pela equipe de infraestrutura no momento do deploy. Se a fila cresce porque o volume de eventos é simplesmente maior do que a plataforma comporta, fale com o administrador da plataforma.
:::

## Um evento na fila de reenvio com erro

Abra **Normalização → Quarentena** para ver o motivo da falha de cada evento retido. Os motivos mais comuns e o que fazer:

| Mensagem | Causa | O que fazer |
|----------|-------|-------------|
| Conexão recusada | Destino fora do ar | Verifique a disponibilidade do serviço de destino. |
| 401 / 403 | Credencial inválida | Gere uma nova credencial e atualize o destino em **Operação → Destinos** (admin). |
| 400 (requisição inválida) | Formato do evento rejeitado | Revise o mapeamento em **Normalização → Mappings**. |
| 429 / 503 | Destino sobrecarregado | Aguarde; costuma normalizar. Se persistir, ajuste o limite de envio do destino. |
| Recurso não encontrado (bucket / tópico) | Recurso não existe no destino | Crie o recurso no serviço de destino. |
| Tempo esgotado | Destino lento | Ajuste o tempo limite do destino em **Operação → Destinos** (admin). |
| Proteção contra destino instável ativada | Várias falhas seguidas | Aguarde a reconexão automática ou corrija a causa raiz no destino. |

Depois de resolver a causa, reprocesse os eventos diretamente na tela **Normalização → Quarentena**.

## Como funciona a entrega

Entender o caminho do evento ajuda a saber onde olhar quando algo falha.

1. **Coleta** — o CentralOps puxa os dados das fontes configuradas (Sophos Central, Wazuh, etc.).
2. **Normalização** — o evento é convertido para o formato padrão (OCSF).
3. **Roteamento** — o CentralOps avalia as regras de roteamento, na ordem, e usa a primeira que combina.
4. **Envio simultâneo a vários destinos** — o mesmo evento é enviado a todos os destinos da regra.
5. **Entrega** — o processamento em segundo plano entrega o evento a cada destino, com novas tentativas e proteção contra destino instável.
6. **Fila de reenvio** — se a entrega falhar mesmo após as tentativas, o evento fica retido para revisão manual em **Normalização → Quarentena**.

### Proteção contra destino instável

Se um destino falha várias vezes seguidas, o CentralOps interrompe temporariamente os envios para ele, para não acumular falhas. Após um curto intervalo, a plataforma tenta reconectar automaticamente. O estado de cada destino fica visível em **Normalização → Saúde do Pipeline**.

## Antes de adicionar um novo destino

Antes de colocar um destino novo em produção, confirme pela interface:

- [ ] **Normalização → Saúde do Pipeline** mostra os destinos existentes em verde ou amarelo.
- [ ] **Normalização → Quarentena** não está acumulando eventos novos.
- [ ] Um evento de teste chegou a pelo menos um destino.

## Próximos passos

- **Evento em quarentena?** Veja [Quarentena](../operations/quarantine.md).
- **A coleta parou?** Veja [Collectors](../pipelines/collectors.md).
- **Configurar um destino?** Veja [Destinos](../outputs/destinations.md).
- **Configurar uma regra de roteamento?** Veja [Roteamento](../outputs/routing.md).
