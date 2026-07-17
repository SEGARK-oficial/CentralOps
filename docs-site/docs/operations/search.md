---
sidebar_position: 4
title: Investigações
description: Busca flexível por eventos já entregues aos seus destinos, com filtros, intervalos de tempo e exportação
---

# Investigações

A tela **Investigações** é onde você procura por eventos de segurança que já foram coletados e entregues aos seus destinos. Você monta a busca combinando filtros (severidade, plataforma de origem, destino, intervalo de tempo) e palavras-chave, e recebe os eventos correspondentes em uma lista paginada.

Para abrir, acesse o menu **Operação → Investigações**.

:::info[Busca ao vivo federada (multi-vendor)]
A tela de Investigações cobre **eventos já entregues** aos seus destinos. Se você precisa rodar uma **busca ao vivo** direto nas suas integrações (Sophos, Wazuh, CrowdStrike, Defender etc.) de forma federada e assíncrona, veja [Busca Federada](./federated-search.md).
:::

## Quando usar

- **Investigar um indicador de comprometimento (IOC)**: chegou um hash, IP ou domínio suspeito e você precisa ver todos os eventos relacionados a ele nos últimos dias.
- **Confirmar entrega ao SIEM**: um alerta crítico foi gerado e você quer confirmar se ele realmente chegou ao Splunk, ao Sentinel ou ao destino esperado — e não ficou retido na fila de reenvio.
- **Montar a linha do tempo de um host**: durante uma resposta a incidente, você precisa de todos os eventos de um mesmo equipamento afetado dentro da janela do incidente, para exportar e anexar ao caso.

## Permissões necessárias

:::note[Rodar query AO VIVO exige Operator ou superior]
A tela de Investigações permite buscar eventos **já entregues aos seus destinos** com filtros e palavras-chave. Se o seu papel é Viewer, você consegue ver os resultados já coletados sem restrição.

Porém, **rodar uma busca AO VIVO na fonte** (que dispara coleta no cliente e custa $) exige papel **Operator** ou superior. Se você tentar buscar e receber erro 403, é porque sua conta está como Viewer. Peça ao administrador para promovê-lo a Operator.
:::

:::note[Salvar query exige Engineer ou superior]
Salvar uma busca para reutilizar depois, ou agendar uma busca para rodar automaticamente, exige papel **Engineer** ou superior. Viewer e Operator conseguem rodar buscas, mas não salvar.
:::

## Como montar uma busca

A tela tem três áreas principais:

1. **Campo de busca** (no topo) — onde você digita os termos. Ele oferece sugestões enquanto você escreve.
2. **Filtros rápidos** (lateral esquerda) — caixas de seleção para refinar sem digitar.
3. **Resultados** (área principal) — a lista de eventos encontrados, paginada.

### Filtros rápidos

Em vez de digitar, você pode clicar nos filtros da lateral esquerda. Cada filtro selecionado restringe ainda mais o resultado (funciona como um "e também"):

- **Severidade**: Crítica, Alta, Média, Baixa.
- **Plataforma de origem**: de onde o evento veio (por exemplo, Sophos ou Wazuh).
- **Destino de entrega**: para onde o evento foi enviado (Wazuh, Splunk, S3, Elastic, Sentinel, Kafka e outros configurados).
- **Rota**: o nome da regra de roteamento que processou o evento.
- **Status de entrega**: se o evento foi aceito, enviado com sucesso, ou ficou na fila de reenvio.
- **Período**: últimas 24 horas, 7 dias, 30 dias ou um intervalo personalizado.

A lista de destinos que aparece como filtro reflete os destinos que o administrador configurou na plataforma.

### Termos de busca

No campo de busca você combina condições no formato `campo:valor`. Use os conectores para refinar:

| Conector | Para que serve | Exemplo |
|----------|----------------|---------|
| `:` | Igual a | `severidade:critica` |
| `AND` | Atende às duas condições | `severidade:critica AND destino:splunk` |
| `OR` | Atende a pelo menos uma | `severidade:critica OR severidade:alta` |
| `NOT` | Exclui o que casar | `NOT severidade:baixa` |
| `[X TO Y]` | Intervalo (datas ou números) | `data:[2026-04-27 TO 2026-04-28]` |
| `*` | Curinga (qualquer continuação) | `titulo:"trojan*"` |
| `~` | Expressão regular (somente administradores) | `titulo:~"trojan.*"` |

O conector `~` (expressão regular) só está disponível para administradores. Se o seu perfil não tiver essa permissão, o recurso não fica acessível na busca.

### Campos que você pode filtrar

| Campo | O que representa |
|-------|------------------|
| **Severidade** | Crítica, Alta, Média ou Baixa |
| **Plataforma de origem** | De onde o evento veio (por exemplo, Sophos, Wazuh) |
| **Destino** | Para onde o evento foi enviado (Wazuh, Splunk, S3, Elastic, Sentinel, Kafka...) |
| **Rota** | Nome da regra de roteamento que processou o evento |
| **Status de entrega** | Aceito, enviado ou na fila de reenvio |
| **Título** | Título ou descrição do evento |
| **Data** | Data e hora em que o evento foi recebido |
| **Host de origem** | Nome do equipamento afetado |
| **IP de origem** | Endereço IP afetado |

> Filtro por organização: visível apenas para administradores que operam múltiplas organizações.

Campos adicionais que vierem dos seus dados (criados durante a normalização) também passam a aparecer nas sugestões depois da primeira coleta.

## Exemplos práticos

### Eventos críticos de uma plataforma entregues a um destino

> "Todos os eventos críticos da Sophos entregues ao Splunk."

```
severidade:critica AND plataforma:sophos AND destino:splunk
```

### Busca por palavra no título

> "Eventos com 'ransomware' no título."

```
titulo:"ransomware"
```

### Busca por intervalo de tempo

> "Eventos entre ontem e hoje."

Você pode usar o filtro **Período** na lateral e escolher um intervalo personalizado, informando a data inicial e a final. Ou digitar direto no campo de busca:

```
data:[2026-04-26 TO 2026-04-27]
```

### Excluir resultados

> "Eventos críticos que não sejam testes."

```
severidade:critica NOT titulo:"*test*"
```

### Confirmar entrega com sucesso

> "Eventos críticos entregues ao Wazuh com sucesso."

```
severidade:critica AND destino:wazuh AND status:enviado
```

### Busca com várias condições

> "Eventos críticos ou altos da Sophos dos últimos 7 dias, entregues a Splunk ou S3, exceto os que ficaram na fila de reenvio."

```
(severidade:critica OR severidade:alta) AND plataforma:sophos AND (destino:splunk OR destino:s3) AND NOT status:reenvio
```

## Salvar uma busca

Quando você monta uma busca complexa que vai repetir, dê um nome e salve-a para reutilizar depois.

1. Monte a busca no campo.
2. Use a opção de salvar a busca na própria tela.
3. Dê um nome descritivo, por exemplo "Ransomware últimas 24h para o SOC".

As buscas salvas ficam reunidas no menu **Conhecimento → Queries Salvas**, prontas para serem aplicadas novamente com um clique.

## Agendar uma busca

> Disponível para perfis Engineer ou superior, e para administradores.

Você pode pedir para uma busca rodar automaticamente em intervalos definidos (por exemplo, diariamente às 08:00) e receber o resultado por e-mail quando houver correspondência.

Os agendamentos são gerenciados no menu **Conhecimento → Agendamentos** (visível apenas para administradores). Lá você define o nome, a busca e a frequência.

## Exportar resultados

Use a opção de download na tela para baixar o resultado da busca em um destes formatos:

- **CSV** — abre em planilha (Excel, Google Sheets).
- **JSON** — eventos no formato estruturado.
- **PDF** — relatório formatado.

É útil para compartilhar com a liderança do SOC, anexar a um caso de investigação ou guardar para auditoria e conformidade.

## O que esperar dos resultados

| Comportamento | Detalhe |
|---------------|---------|
| **Janela de busca** | A busca cobre os eventos recentes (cerca de 7 dias). Para dados mais antigos, veja abaixo. |
| **Limite por busca** | Há um limite de eventos retornados por busca. Se a busca for muito ampla e ultrapassar o limite, a tela mostra um aviso — refine os filtros (período, severidade, destino) para chegar ao que procura. |
| **Agregações avançadas** | A tela de Investigações não faz somatórios, gráficos ou estatísticas. Para esse tipo de análise, use o painel nativo do destino para onde os eventos foram enviados — por exemplo, Kibana quando o destino é Elastic, ou a interface do Splunk quando o destino é Splunk. A lista de destinos configurados está em **Operação → Destinos** (visível apenas para administradores). |

### Dados anteriores à janela de busca

A tela de Investigações alcança apenas os eventos mais recentes. Para consultar eventos mais antigos, acesse-os diretamente no destino de longa duração que o administrador configurou (por exemplo, um bucket S3 ou um cluster Elastic). A retenção de longo prazo (meses ou anos) depende de haver um destino de armazenamento configurado — a configuração de destinos fica em **Operação → Destinos** (visível apenas para administradores).

> O tempo exato da janela de busca e a retenção de longo prazo são definidos pela equipe de infraestrutura no momento do deploy. Se precisar alterá-los, fale com o administrador da plataforma.

## Casos de uso comuns

| Caso | Como montar a busca |
|------|---------------------|
| **Investigação de IOC** | `titulo:"*hash-suspeito*" OR ip_origem:"1.2.3.4"` combinado com o destino esperado |
| **Procurar testes/falsos positivos** | `plataforma:sophos AND severidade:critica AND titulo:"*test*"` |
| **Linha do tempo de um host** | `host_origem:nome-do-host AND data:[now-7d TO now]` |
| **Relatório de conformidade** | `severidade:critica AND data:[2026-04-01 TO 2026-04-30]` e depois exportar |
| **Entrega que falhou** | filtre por status "na fila de reenvio" e pelo destino — mostra eventos que ainda não chegaram àquele destino |

## Próximos passos

- **Quer triar as detecções geradas por regras e buscas?** Vá a **Operação → Detecções** ([Detecções](./detections.md)).
- **Precisa salvar e reaproveitar buscas?** Vá a **Conhecimento → Queries Salvas**.
- **Quer agendar uma busca recorrente?** Vá a **Conhecimento → Agendamentos** (somente administradores).
- **Precisa revisar para onde os eventos vão?** Os administradores encontram a configuração em **Operação → Destinos** e **Operação → Roteamento**.
- **Os dados que procura estão num destino externo?** Consulte o painel nativo desse destino (por exemplo, Kibana para Elastic, Splunk para Splunk).
