---
sidebar_position: 12
title: "Destino: CrowdStrike Falcon Next-Gen SIEM"
description: Envie eventos normalizados ao Falcon Next-Gen SIEM pela tela de Destinos.
---

# Destino: CrowdStrike Falcon Next-Gen SIEM

O destino **CrowdStrike Falcon Next-Gen SIEM** entrega os eventos já normalizados pelo CentralOps diretamente para o seu Falcon Next-Gen SIEM, usando o conector HEC (HTTP Event Collector). Você cria e gerencia tudo pela interface, na tela de Destinos.

Esta tela só aparece para administradores da plataforma.

:::note[Não confunda com a integração CrowdStrike]
O CrowdStrike tem dois papéis no CentralOps:
- **Integração (fonte):** coleta detecções do Falcon para o CentralOps. Veja [CrowdStrike Falcon](../integrations/crowdstrike.md).
- **Destino:** envia eventos normalizados do CentralOps para o Falcon Next-Gen SIEM. Este documento.
:::

## Quando usar

- **Centralizar eventos no Falcon Next-Gen SIEM.** Você coleta de Sophos, Wazuh e outras fontes e quer que todos os eventos chegem normalizados no Falcon para análise e correlação num só lugar.
- **Alimentar detecção nativa do Falcon.** Os eventos saem do CentralOps já normalizados e enriquecidos, prontos para as regras de detecção e correlação do Falcon.
- **Manter um feed contínuo de dados terceiros.** Você tem múltiplas fontes de segurança e quer que o Falcon as centralize com seus dados de endpoint.

## O que você precisa antes de começar

Para criar o destino, tenha em mãos:

- **URL do conector HEC no Falcon** — a URL que você obtém em **Next-Gen SIEM → Data connectors → HEC**. Exemplo: `https://api.crowdstrike.com/data-collection/v1/...`.
- **Token de ingestão HEC** — a credencial gerada no mesmo local no Falcon. O CentralOps guarda esse token de forma criptografada; ele nunca aparece em tela depois de salvo.

> O URL e o token HEC são gerados no Falcon pela equipe que administra aquele ambiente. No CentralOps você apenas informa esses valores ao criar o destino.

## Criar o destino

1. No menu lateral, abra **Operação → Destinos**.
2. Use a opção de criar um novo destino.
3. Escolha o tipo **CrowdStrike Falcon Next-Gen SIEM**.
4. Preencha os campos abaixo.

| Campo | O que informar |
|-------|----------------|
| **Nome** | Um nome que ajude a identificar este destino (ex.: "Falcon Next-Gen SIEM - Acme"). |
| **Endpoint** | A URL completa do conector HEC, conforme exibida no Falcon (ex.: `https://api.crowdstrike.com/data-collection/v1/...`). |
| **Credencial — ingest_token** | O token de ingestão copiado do Falcon. Fica criptografado após salvar. |
| **Sourcetype** | O tipo com que os eventos serão marcados no Falcon (padrão: `centralops`). |
| **Source** | Identifica a origem dos dados (opcional). |
| **Headers adicionais** | Qualquer header HTTP extra necessário (opcional). |
| **Verificar TLS** | Mantenha ativado para garantir uma conexão segura (padrão: ligado). |

### Sobre a verificação TLS e certificados próprios

Mantenha **Verificar TLS** ativado sempre que possível — ele garante que o CentralOps está mesmo falando com o seu Falcon.

Se o seu ambiente usa um certificado próprio (autoassinado ou de uma autoridade interna), a confiança nesse certificado é definida pela equipe de infraestrutura no momento do deploy. Se a conexão segura falhar por causa do certificado, fale com o administrador da plataforma.

### Testar e salvar

Antes de salvar, use o botão **Testar conexão**. O CentralOps verifica, de uma só vez:

- se consegue alcançar o endpoint do HEC no Falcon;
- se o token é aceito pelo Falcon;
- se o formato dos eventos é aceito.

Se o teste passar, salve. O destino já fica **ativo** (badge verde) e começa a receber os eventos roteados para ele.

## Como os eventos são entregues

Você não precisa configurar nada do funcionamento interno — ele já vem ajustado para entrega eficiente. Vale apenas entender o comportamento:

- **Formato NDJSON no HEC.** Os eventos são enviados como JSON Lines (um JSON por linha), encapsulados no formato HEC esperado pelo Falcon.
- **Envio em lotes.** Os eventos são agrupados e enviados em blocos, o que é mais eficiente.
- **Nova tentativa automática.** Se o Falcon recusar ou ficar indisponível por um instante, o CentralOps tenta reenviar sozinho.
- **Isolamento por item.** Se um evento falhar individualmente, ele é isolado; os demais continuam sendo entregues.
- **Entrega ao menos uma vez.** Em uma queda no momento errado, um evento pode chegar duplicado. Cada evento carrega um identificador único.

Os parâmetros finos desse comportamento são definidos pela equipe de infraestrutura. Se precisar ajustá-los, fale com o administrador da plataforma.

## Acompanhar a saúde do destino

Abra **Operação → Destinos** e selecione o destino CrowdStrike Falcon Next-Gen SIEM.

O badge de saúde mostra a situação atual:

| Cor | Significado |
|-----|-------------|
| Verde | Eventos sendo entregues normalmente, sem itens na fila de reenvio. |
| Amarelo | Eventos chegando, mas há itens parados na fila de reenvio. |
| Vermelho | Envio pausado ou Falcon indisponível. |
| Cinza | Destino desativado. |

Na visão do destino você acompanha as métricas em tempo real:

- **Eventos por segundo** — ritmo de entrega na última hora.
- **Volume** — quanto dado está saindo na última hora.
- **Latência média** — quanto o Falcon leva para responder.
- **Itens na fila de reenvio (24h)** — quantos eventos foram recusados no último dia.

Para ver os eventos que não puderam ser entregues, abra a **fila de reenvio** na visão do destino. Cada item mostra o identificador do evento, o motivo da recusa, o horário e o conteúdo exato — útil para entender e corrigir a causa.

## Resolver problemas comuns

| Sintoma | O que verificar |
|---------|-----------------|
| **Não conecta ao Falcon** | O endpoint está completo e correto conforme copiado do Falcon? O Falcon está no ar? Se houver firewall entre as redes, o time de infraestrutura precisa liberar o acesso. |
| **Token recusado (erro 401 ou 403)** | O token foi colado por inteiro? Ele pode ter sido revogado ou expirado no Falcon. Peça à equipe do Falcon para confirmar ou gerar um novo, e atualize o destino. |
| **Endpoint inválido** | Confira que você copiou a URL completa do conector HEC (**Next-Gen SIEM → Data connectors → HEC**), não uma URL diferente do Falcon. |
| **Eventos recusados por formato inválido** | Abra a fila de reenvio e veja o conteúdo recusado. Pode ser um evento maior que o limite aceito ou um campo incompatível. Ajuste na plataforma e os próximos envios voltam a passar. |
| **Falha de certificado seguro (TLS)** | Acontece quando o Falcon usa um certificado próprio que a plataforma ainda não reconhece. Essa confiança é configurada no deploy — fale com o administrador da plataforma. |

## Próximos passos

- **Confirmar que os dados estão chegando:** abra **Operação → Destinos**, selecione o Falcon Next-Gen SIEM e veja as métricas de eventos por segundo.
- **Investigar eventos recusados:** abra a fila de reenvio na visão do destino.
- **Adicionar outros destinos:** veja a [visão geral de destinos](./overview.md).
- **Decidir quais eventos vão para cada destino:** use a tela de [Roteamento](./routing.md).
