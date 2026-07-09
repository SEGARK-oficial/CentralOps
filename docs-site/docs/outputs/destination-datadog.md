---
sidebar_position: 18
title: "Destino: Datadog Logs"
description: Encaminhe eventos para observabilidade — alimentar Datadog amplia seu TAM além do SOC.
---

# Destino: Datadog Logs

O destino **Datadog Logs** entrega seus eventos normalizados ao Datadog Logs, a plataforma de observabilidade do Datadog. Use-o quando seu time de operações e segurança precisam correlacionar eventos de segurança com métricas e logs operacionais, mantendo tudo em um único painel.

Esta tela só aparece para administradores da plataforma.

## Quando usar

- **Ampliar observabilidade para além do SOC.** Você coleta alertas de segurança (Wazuh, Sophos) e quer que o time de operações e SRE os veja no contexto mais amplo de saúde da aplicação no Datadog.
- **Correlação entre segurança e infraestrutura.** Um pico de alertas coincide com uma degradação de performance? O Datadog ajuda a conectar esses pontos.
- **Conformidade com histórico único.** Manter todos os eventos centralizados em um repositório de observabilidade que já atende a auditorias.

## O que você precisa antes de começar

- **API key do Datadog** — gerada no console Datadog. O CentralOps guarda criptografada; ela nunca aparece em tela depois de salva.
- **Site do Datadog** (padrão: `datadoghq.com`):
  - `datadoghq.com` — EUA (padrão).
  - `datadoghq.eu` — Europa.
  - `us3.datadoghq.com` — EUA (zona 3).
  - Consulte a [documentação Datadog](https://docs.datadoghq.com/getting_started/site/) para sua região.
- **Service e ddsource** (opcional): campos de metadados para organizar os logs no Datadog.

> A API key é gerada por quem administra a instância Datadog. No CentralOps você apenas informa o valor ao criar o destino.

## Criar o destino

1. No menu lateral, abra **Operação → Destinos**.
2. Use a opção de criar um novo destino.
3. Escolha o tipo **Datadog Logs**.
4. Preencha os campos abaixo.

| Campo | O que informar |
|-------|----------------|
| **Nome** | Um nome que ajude a identificar este destino (ex.: "Datadog Prod"). |
| **Site** | O site Datadog da sua região (padrão: `datadoghq.com`). |
| **Service** | Nome do serviço (padrão: `centralops`). Aparece como campo `service` nos logs. |
| **DdSource** | Identificador da origem dos logs (padrão: `centralops`). Ajuda o Datadog a detectar o parser. |
| **Tags** | Tags Datadog em formato `chave:valor,chave2:valor2` (ex.: `env:prod,team:soc`). Opcional. |
| **API Key** | A chave API copiada do Datadog. Fica criptografada após salvar. |

### Testar e salvar

Antes de salvar, use o botão **Testar conexão**. O CentralOps verifica:

- se consegue alcançar o Datadog Logs Intake;
- se a API key é válida;
- se a configuração é aceita.

Se o teste passar, salve. O destino fica **ativo** (badge verde).

## Como os eventos são entregues

- **Envio em lotes.** Os eventos são agrupados para eficiência.
- **Formato OCSF aninhado.** O evento normalizado chega em um campo `ocsf` dentro da entrada de log do Datadog. O `service` e `ddsource` ajudam o Datadog a processar e rotear.
- **Nova tentativa automática.** Falhas transitórias (timeouts, 5xx, 429) disparam reenvio automático.
- **Entrega ao menos uma vez.** Em caso de queda, um evento pode chegar duplicado no Datadog. Use o ID único em `_centralops.event_id` para deduplicar.
- **Proteção contra destino instável.** Se o Datadog falhar persistentemente, o CentralOps pausa o envio e retoma automaticamente.

## Acompanhar a saúde do destino

Abra **Operação → Destinos** e selecione o seu destino Datadog.

O badge de saúde mostra:

| Cor | Significado |
|-----|-------------|
| Verde | Eventos sendo entregues normalmente. |
| Amarelo | Eventos chegando, mas há itens na fila de reenvio. |
| Vermelho | Envio pausado ou Datadog indisponível. |
| Cinza | Destino desativado. |

Na visão do destino você acompanha:

- **Eventos por segundo** — taxa de entrega na última hora.
- **Latência média** — tempo de resposta do Datadog.
- **Itens na fila de reenvio (24h)** — eventos recusados.

Para ver o que não foi entregue, abra a **fila de reenvio**. Cada item mostra o motivo exato e o conteúdo do evento, útil para debugar.

## Resolver problemas comuns

| Sintoma | O que verificar |
|---------|-----------------|
| **"API key ausente" / destino inativo** | A API key não foi resolvida. Confirme que a credencial foi guardada no cofre. Use o botão testar a conexão. |
| **"401 Unauthorized" / chave rejeitada** | A API key está incorreta, expirou ou foi revogada. Gere uma nova chave no Datadog e atualize o destino. |
| **"Site inválido"** | Confirme que o site está correto (ex.: `datadoghq.eu` para Europa, não `datadoghq.com`). |
| **Eventos chegando, mas desaparecem do Datadog** | Confirme que o `service` e `ddsource` não colidem com outras integrações. Se há parsing incorreto, revise os campos no Datadog Logs Intake. |
| **Muitos eventos na fila de reenvio** | O Datadog pode estar sobrecarregado ou você atingiu a cota de ingestão. Verifique a latência média. Se persistir, acione o suporte Datadog para revisar capacidade. |

## Próximos passos

- **Confirmar que os dados estão chegando:** abra **Operação → Destinos**, selecione o Datadog e veja as métricas de eventos por segundo.
- **Investigar eventos recusados:** abra a fila de reenvio na visão do destino.
- **Adicionar outros destinos:** veja a [visão geral de destinos](./overview.md).
- **Decidir quais eventos vão para cada destino:** use a tela de [Roteamento](./routing.md).
