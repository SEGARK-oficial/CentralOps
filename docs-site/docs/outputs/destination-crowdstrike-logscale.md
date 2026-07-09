---
sidebar_position: 13
title: "Destino: CrowdStrike Falcon LogScale"
description: Envie eventos normalizados ao LogScale pela tela de Destinos, sem sair da interface do CentralOps.
---

# Destino: CrowdStrike Falcon LogScale

O destino **CrowdStrike Falcon LogScale** entrega os eventos já normalizados pelo CentralOps diretamente para o seu LogScale (antigo Humio), usando um ingesta de dados de alta performance. Você cria e gerencia tudo pela interface, na tela de Destinos.

Esta tela só aparece para administradores da plataforma.

## Quando usar

- **Indexar eventos em LogScale para buscas em tempo real.** Colete de múltiplas integrações (Falcon, Sophos, Wazuh) e agregue todos no LogScale para buscar, filtrar e montar dashboards num só lugar.
- **Correlacionar ameaças do CrowdStrike com outros dados.** Os eventos saem do CentralOps já normalizados e enriquecidos, prontos para o LogScale correlacionar com seus alertas e análises comportamentais.
- **Manter um repositório escalável de logs de segurança.** O LogScale foi projetado para alta escala. Você redireciona seus eventos normalizados e deixa a retenção e busca sob controle do LogScale.

## O que você precisa antes de começar

Para criar o destino, tenha em mãos:

- **URL de ingestão do LogScale** — o endpoint HEC do seu ambiente LogScale. Você encontra na consola do LogScale (por exemplo, `https://cloud.us.humio.com/api/v1/ingest/hec` para cloud US). Quem administra o LogScale fornece esse endereço.
- **Token de ingestão** — uma credencial gerada no LogScale. O CentralOps guarda esse token de forma criptografada; ele nunca aparece em tela depois de salvo.
- **Tipo de origem (sourcetype)** — opcional. Ajuda o LogScale a categorizar seus eventos.
- **Fonte (source)** — opcional. Atribui a origem dos dados (host ou integração).

> O token e a URL de ingestão são gerados no LogScale pela equipe que administra aquele ambiente. No CentralOps você apenas informa esses valores ao criar o destino.

## Criar o destino

1. No menu lateral, abra **Visão geral -> Integrações** para confirmar que suas fontes já estão coletando, depois vá em **Operação -> Destinos**.
2. Use a opção de criar um novo destino.
3. Escolha o tipo **CrowdStrike Falcon LogScale**.
4. Preencha os campos abaixo.

| Campo | O que informar |
|-------|----------------|
| **Nome** | Um nome que ajude a identificar este destino (ex.: "LogScale Produção"). |
| **Endpoint** | A URL de ingestão HEC completa (ex.: `https://cloud.us.humio.com/api/v1/ingest/hec`). |
| **Credencial — ingest_token** | O token de ingestão copiado do LogScale. Fica criptografado após salvar. |
| **Sourcetype** | O tipo de origem com que os eventos serão marcados no LogScale (opcional). |
| **Source** | Identifica a origem dos dados no LogScale (opcional). |
| **Headers adicionais** | Cabeçalhos HTTP customizados, se necessário (opcional). |
| **Verificar TLS** | Mantenha ativado para garantir uma conexão segura. |

### Sobre a verificação TLS e certificados próprios

Mantenha **Verificar TLS** ativado sempre que possível — ele garante que o CentralOps está mesmo falando com seu LogScale.

Se o LogScale usa um certificado próprio (autoassinado ou de uma autoridade interna), a confiança nesse certificado é definida pela equipe de infraestrutura no momento do deploy. Se a conexão segura falhar por causa do certificado, fale com o administrador da plataforma para que ele registre o certificado correto. Você não precisa lidar com arquivos de certificado pela interface.

### Testar e salvar

Antes de salvar, use o botão **Testar conexão**. O CentralOps verifica, de uma só vez:

- se consegue alcançar o endpoint de ingestão;
- se o token é aceito pelo LogScale;
- se o formato dos eventos é aceito.

Se o teste passar, salve. O destino já fica **ativo** (badge verde) e começa a receber os eventos roteados para ele.

> O teste de conexão substitui qualquer verificação manual: não é preciso checar conectividade por fora da plataforma.

## Como os eventos são entregues

Você não precisa configurar nada do funcionamento interno — ele já vem ajustado para entrega eficiente ao LogScale. Vale apenas entender o comportamento:

- **Envio em lotes.** Os eventos são agrupados e enviados em blocos, o que é mais eficiente do que enviar um a um.
- **Nova tentativa automática.** Se o LogScale recusar ou ficar indisponível por um instante, o CentralOps tenta reenviar sozinho, esperando um pouco mais entre cada tentativa.
- **Proteção contra destino instável.** Se o LogScale começar a falhar de forma persistente, o CentralOps pausa o envio por um curto período e volta a tentar automaticamente, evitando inundar um destino com problema.
- **Entrega ao menos uma vez.** Em uma queda no momento errado, um evento pode chegar duplicado ao LogScale. Cada evento carrega um identificador único, então você pode remover duplicados no próprio LogScale com uma busca por esse identificador.

Os parâmetros finos desse comportamento (tamanho de lote, número de tentativas, limites da proteção contra destino instável) são definidos pela equipe de infraestrutura no momento do deploy. Se precisar ajustá-los, fale com o administrador da plataforma.

## Acompanhar a saúde do destino

Abra **Operação -> Destinos** e selecione o destino LogScale.

O badge de saúde mostra a situação atual:

| Cor | Significado |
|-----|-------------|
| Verde | Eventos sendo entregues normalmente, sem itens na fila de reenvio. |
| Amarelo | Eventos chegando, mas há itens parados na fila de reenvio. |
| Vermelho | Envio pausado pela proteção contra destino instável ou LogScale indisponível. |
| Cinza | Destino desativado. |

Na visão do destino você acompanha as métricas em tempo real:

- **Eventos por segundo** — ritmo de entrega na última hora.
- **Volume** — quanto dado está saindo na última hora.
- **Latência média** — quanto o LogScale leva para responder.
- **Itens na fila de reenvio (24h)** — quantos eventos foram recusados no último dia.

Para ver os eventos que não puderam ser entregues, abra a **fila de reenvio** na visão do destino. Cada item mostra o identificador do evento, o motivo da recusa informado pelo LogScale, o horário e o conteúdo exato que foi rejeitado — útil para entender e corrigir a causa.

Para uma visão mais ampla de como os dados percorrem a plataforma até os destinos, use **Operação -> Fluxo de dados** e **Normalização -> Saúde do Pipeline**.

## Resolver problemas comuns

| Sintoma | O que verificar |
|---------|-----------------|
| **Não conecta ao LogScale** | A URL está completa, com `https://` e o caminho correto? O LogScale está no ar? Se houver firewall entre as redes, o time de infraestrutura precisa liberar o acesso. |
| **Token recusado (erro de autenticação — 401 ou 403)** | O token foi colado por inteiro? Ele pode ter sido revogado ou expirado no LogScale. Peça à equipe do LogScale para confirmar ou gerar um novo, e atualize o destino. |
| **Eventos recusados por formato inválido** | Abra a fila de reenvio e veja o conteúdo recusado. Em geral é um evento maior que o limite aceito ou um campo incomum. Ajuste no LogScale e os próximos envios voltam a passar. |
| **Envio pausado / "muitas requisições"** | O LogScale pode estar sobrecarregado ou ter atingido a cota de ingestão. A proteção contra destino instável volta a tentar sozinha após um curto intervalo; se persistir, acione a equipe do LogScale para revisar capacidade ou cota. |
| **Falha de certificado seguro (TLS)** | Acontece quando o LogScale usa um certificado próprio que a plataforma ainda não reconhece. Essa confiança é configurada no deploy — fale com o administrador da plataforma. |

## Próximos passos

- **Confirmar que os dados estão chegando:** abra **Operação -> Destinos**, selecione o LogScale e veja as métricas de eventos por segundo.
- **Investigar eventos recusados:** abra a fila de reenvio na visão do destino.
- **Adicionar outros destinos:** veja a [visão geral de destinos](./overview.md).
- **Decidir quais eventos vão para cada destino:** use a tela de [Roteamento](./routing.md).
