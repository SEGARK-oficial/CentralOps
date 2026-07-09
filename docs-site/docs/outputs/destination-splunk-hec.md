---
sidebar_position: 4
title: "Destino: Splunk HEC"
description: Envie eventos normalizados ao Splunk pela tela de Destinos, sem sair da interface do CentralOps.
---

# Destino: Splunk HEC

O destino **Splunk HEC** entrega os eventos já normalizados pelo CentralOps diretamente para o seu Splunk, usando o canal de ingestão de alta performance do Splunk (HTTP Event Collector). Você cria e gerencia tudo pela interface, na tela de Destinos.

Esta tela só aparece para administradores da plataforma.

## Quando usar

- **Centralizar vários coletores em um SIEM único.** Você coleta de Sophos Central, Wazuh e outras fontes em regiões diferentes e quer que todos os eventos cheguem normalizados ao mesmo Splunk para investigar e montar painéis num só lugar.
- **Alimentar correlação e enriquecimento no Splunk.** Os eventos saem do CentralOps já normalizados e enriquecidos, prontos para o Splunk correlacionar com seus alertas e regras existentes.
- **Manter um índice dedicado para conformidade.** Você precisa reter um volume grande de logs (por exemplo, para PCI-DSS ou LGPD) e quer direcionar os eventos para um índice de retenção longa no Splunk.

## O que você precisa antes de começar

Para criar o destino, tenha em mãos:

- **Endereço do HEC no Splunk** — a URL e a porta da sua instância (por exemplo, `https://splunk.exemplo.com:8088`). Quem administra o Splunk fornece esse endereço.
- **Token HEC** — uma credencial gerada no Splunk. O CentralOps guarda esse token de forma criptografada; ele nunca aparece em tela depois de salvo.
- **Índice de destino** (opcional) — o índice do Splunk onde os eventos devem cair. Se deixar em branco, o CentralOps usa o índice padrão do próprio token.

> O token e o endereço do HEC são gerados no Splunk pela equipe que administra aquele ambiente. No CentralOps você apenas informa esses valores ao criar o destino.

## Criar o destino

1. No menu lateral, abra **Visão geral -> Integrações** para confirmar que suas fontes já estão coletando, depois vá em **Operação -> Destinos**.
2. Use a opção de criar um novo destino.
3. Escolha o tipo **Splunk HEC**.
4. Preencha os campos abaixo.

| Campo | O que informar |
|-------|----------------|
| **Nome** | Um nome que ajude a identificar este destino (ex.: "Splunk Produção"). |
| **URL** | O endereço completo do HEC, com `https://` e a porta (ex.: `https://splunk.exemplo.com:8088`). |
| **Token HEC** | O token copiado do Splunk. Fica criptografado após salvar. |
| **Índice** | O índice de destino no Splunk. Deixe vazio para usar o índice padrão do token. |
| **Sourcetype** | O tipo de origem com que os eventos serão marcados no Splunk (opcional). |
| **Source** | Identifica a origem dos dados no Splunk (opcional). |
| **Host** | Identifica qual coletor enviou os dados (opcional). |
| **Verificar TLS** | Mantenha ativado para garantir uma conexão segura. |

### Sobre a verificação TLS e certificados próprios

Mantenha **Verificar TLS** ativado sempre que possível — ele garante que o CentralOps está mesmo falando com o seu Splunk.

Se o seu Splunk usa um certificado próprio (autoassinado ou de uma autoridade interna), a confiança nesse certificado é definida pela equipe de infraestrutura no momento do deploy. Se a conexão segura falhar por causa do certificado, fale com o administrador da plataforma para que ele registre o certificado correto. Você não precisa lidar com arquivos de certificado pela interface.

### Testar e salvar

Antes de salvar, use o botão **Testar conexão**. O CentralOps verifica, de uma só vez:

- se consegue alcançar o endereço do HEC;
- se o token é aceito pelo Splunk;
- se o formato dos eventos é aceito.

Se o teste passar, salve. O destino já fica **ativo** (badge verde) e começa a receber os eventos roteados para ele.

> O teste de conexão substitui qualquer verificação manual: não é preciso checar conectividade por fora da plataforma.

## Como os eventos são entregues

Você não precisa configurar nada do funcionamento interno — ele já vem ajustado para entrega eficiente ao Splunk. Vale apenas entender o comportamento:

- **Envio em lotes.** Os eventos são agrupados e enviados em blocos, o que é mais eficiente do que enviar um a um.
- **Nova tentativa automática.** Se o Splunk recusar ou ficar indisponível por um instante, o CentralOps tenta reenviar sozinho, esperando um pouco mais entre cada tentativa.
- **Proteção contra destino instável.** Se o Splunk começar a falhar de forma persistente, o CentralOps pausa o envio por um curto período e volta a tentar automaticamente, evitando inundar um destino com problema.
- **Entrega ao menos uma vez.** Em uma queda no momento errado, um evento pode chegar duplicado ao Splunk. Cada evento carrega um identificador único, então você pode remover duplicados no próprio Splunk com uma busca por esse identificador.

Os parâmetros finos desse comportamento (tamanho de lote, número de tentativas, limites da proteção contra destino instável) são definidos pela equipe de infraestrutura no momento do deploy. Se precisar ajustá-los, fale com o administrador da plataforma.

## Acompanhar a saúde do destino

Abra **Operação -> Destinos** e selecione o destino Splunk HEC.

O badge de saúde mostra a situação atual:

| Cor | Significado |
|-----|-------------|
| Verde | Eventos sendo entregues normalmente, sem itens na fila de reenvio. |
| Amarelo | Eventos chegando, mas há itens parados na fila de reenvio. |
| Vermelho | Envio pausado pela proteção contra destino instável ou Splunk indisponível. |
| Cinza | Destino desativado. |

Na visão do destino você acompanha as métricas em tempo real:

- **Eventos por segundo** — ritmo de entrega na última hora.
- **Volume** — quanto dado está saindo na última hora.
- **Latência média** — quanto o Splunk leva para responder.
- **Itens na fila de reenvio (24h)** — quantos eventos foram recusados no último dia.

Para ver os eventos que não puderam ser entregues, abra a **fila de reenvio** na visão do destino. Cada item mostra o identificador do evento, o motivo da recusa informado pelo Splunk, o horário e o conteúdo exato que foi rejeitado — útil para entender e corrigir a causa.

Para uma visão mais ampla de como os dados percorrem a plataforma até os destinos, use **Operação -> Fluxo de dados** e **Normalização -> Saúde do Pipeline**.

## Resolver problemas comuns

| Sintoma | O que verificar |
|---------|-----------------|
| **Não conecta ao Splunk** | A URL está completa, com `https://` e a porta correta? O Splunk está no ar? Se houver firewall entre as redes, o time de infraestrutura precisa liberar o acesso à porta do HEC. |
| **Token recusado (erro de autenticação)** | O token foi colado por inteiro? Ele pode ter sido revogado ou expirado no Splunk. Peça à equipe do Splunk para confirmar ou gerar um novo, e atualize o destino. |
| **Eventos recusados por formato inválido** | Abra a fila de reenvio e veja o conteúdo recusado. Em geral é o índice que não existe/está desabilitado no Splunk, ou um evento maior que o limite aceito. Ajuste no Splunk e os próximos envios voltam a passar. |
| **Envio pausado / "muitas requisições"** | O Splunk pode estar sobrecarregado ou ter atingido a cota de ingestão do token. A proteção contra destino instável volta a tentar sozinha após um curto intervalo; se persistir, acione a equipe do Splunk para revisar capacidade ou cota. |
| **Falha de certificado seguro (TLS)** | Acontece quando o Splunk usa um certificado próprio que a plataforma ainda não reconhece. Essa confiança é configurada no deploy — fale com o administrador da plataforma. |

## Próximos passos

- **Confirmar que os dados estão chegando:** abra **Operação -> Destinos**, selecione o Splunk HEC e veja as métricas de eventos por segundo.
- **Investigar eventos recusados:** abra a fila de reenvio na visão do destino.
- **Adicionar outros destinos:** veja a [visão geral de destinos](./overview.md).
- **Decidir quais eventos vão para cada destino:** use a tela de [Roteamento](./routing.md).
