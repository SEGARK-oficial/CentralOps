---
sidebar_position: 8
title: "Destino: Apache Kafka"
description: Encaminhe eventos normalizados para um tópico Apache Kafka (ou compatível) como destino do pipeline.
---

# Destino: Apache Kafka

O destino **Apache Kafka** publica os eventos já normalizados do CentralOps em um tópico Kafka, onde outras ferramentas podem consumi-los em tempo real. Funciona com Apache Kafka e plataformas compatíveis: Redpanda, AWS MSK, Confluent Cloud e Azure Event Hubs (no endpoint Kafka).

Cada evento vira uma mensagem no tópico. As mensagens são entregues de forma idempotente, ou seja, o CentralOps evita duplicar o mesmo evento no tópico mesmo quando precisa reenviar um lote.

:::info[Quem configura]
Criar e configurar destinos é uma tarefa de **administrador** da plataforma. As telas citadas aqui (**Destinos**, **Roteamento**, **Fluxo de dados**) só aparecem para administradores.
:::

## Quando usar

| Cenário de SOC | Por que usar Kafka como destino |
|----------------|---------------------------------|
| **Alimentar um data lake** | Você quer guardar todos os eventos normalizados em um data lake (por exemplo S3/Iceberg) para investigações de longo prazo. O Kafka serve de buffer de streaming entre o CentralOps e a ferramenta que grava no lake. |
| **Centralizar várias regiões/SIEMs** | Você tem coletores em mais de uma região e quer consolidar tudo em um único fluxo. Várias integrações enviam para o mesmo tópico Kafka, e um dashboard único enxerga os dados de todas elas. |
| **Disparar automação em tempo real (SOAR)** | Você quer reagir a alertas críticos no momento em que chegam. Uma ferramenta de orquestração consome o tópico e dispara ações (abrir chamado, notificar plantão, executar playbook). |

## O que você vai precisar informar

Ao cadastrar o destino, o CentralOps pede os dados de conexão com o cluster Kafka. O administrador do cluster fornece esses valores.

| Campo | O que é |
|-------|---------|
| **Servidores (bootstrap)** | Endereços dos brokers Kafka, separados por vírgula (ex.: `b1.kafka.local:9092,b2.kafka.local:9092`). |
| **Tópico** | Nome do tópico que vai receber os eventos (ex.: `security-events`). O tópico precisa existir no cluster. |
| **Protocolo de segurança** | Como a conexão é protegida: com criptografia (TLS) e/ou autenticação (SASL). O recomendado em produção é com criptografia **e** autenticação. |
| **Mecanismo de autenticação** | Quando há autenticação, qual método o cluster usa (por exemplo SCRAM-SHA-256). O recomendado é SCRAM em vez de senha em texto puro. |
| **Usuário** | Nome de usuário fornecido pelo administrador do cluster Kafka (quando há autenticação). |
| **Senha** | Senha do usuário. Fica guardada de forma cifrada no cofre da plataforma. |
| **Verificar certificado (TLS)** | Mantenha **ativado** em produção para validar o certificado do broker. |
| **Confirmações (acks)** | Nível de confirmação de gravação. O recomendado é **todas** (`all`), que garante maior durabilidade. |

:::tip[Não tem esses dados?]
Endereços dos brokers, tópico, protocolo, usuário e senha são fornecidos por quem administra o cluster Kafka. Se você ainda não os tem, peça ao administrador do cluster Kafka antes de cadastrar o destino. A criação do tópico e dos usuários de autenticação é feita pela equipe que opera o Kafka — não é feita pela interface do CentralOps.
:::

## Como cadastrar o destino

### 1. Abrir a tela de destinos

No menu lateral, vá em **Operação → Destinos** e clique para adicionar um novo destino.

### 2. Escolher o tipo

Selecione **Apache Kafka** na lista de tipos de destino e avance.

### 3. Preencher a configuração

Preencha os campos descritos na tabela acima:

- Dê um **nome** ao destino (ex.: `Kafka Produção`).
- Informe os **servidores (bootstrap)** e o **tópico**.
- Escolha o **protocolo de segurança** e, se houver autenticação, o **mecanismo** e o **usuário**.
- Mantenha **Verificar certificado (TLS)** ativado e **Confirmações** em **todas**.

### 4. Cadastrar a senha

Se o cluster exige autenticação, informe a **senha** do usuário no campo indicado. Ela é guardada de forma cifrada no cofre da plataforma e não fica visível depois de salva.

### 5. Testar a conexão

Use o botão de **testar conexão**. O CentralOps tenta se conectar ao broker e confirmar que o tópico existe. Se estiver tudo certo, ele mostra uma confirmação com o tópico e o número de partições encontradas.

Se o teste falhar, veja a seção [Se algo der errado](#se-algo-der-errado).

### 6. Salvar

Salve o destino. Ele já fica disponível para ser usado no roteamento.

## Como enviar eventos para o destino

Cadastrar o destino não envia eventos por si só. Você precisa criar uma regra de roteamento que aponte os eventos desejados para esse destino.

1. No menu lateral, vá em **Operação → Roteamento** e crie uma nova regra.
2. Selecione a **origem** (o coletor ou a integração de onde vêm os eventos).
3. Escolha o **destino** Kafka que você cadastrou.
4. Defina um **filtro** se quiser enviar apenas parte dos eventos (por exemplo, somente os de severidade mais alta).
5. Ative a regra.

A partir daí, os eventos que casam com o filtro passam a ser publicados no tópico.

### Acompanhar o fluxo

Para visualizar os eventos saindo do CentralOps em direção ao Kafka, use a tela **Operação → Fluxo de dados**, que mostra o caminho dos eventos da origem até cada destino.

A conferência das mensagens dentro do próprio tópico Kafka é feita por quem opera o cluster, com as ferramentas de consumo do Kafka — isso fica fora da interface do CentralOps.

## Garantias de entrega

| Garantia | O que significa na prática |
|----------|----------------------------|
| **Sem duplicar no reenvio** | Se o CentralOps precisar reenviar um lote (por exemplo após uma instabilidade), o mesmo evento não é duplicado no tópico dentro de uma sessão de envio. |
| **Ordem preservada por evento** | Eventos relacionados a uma mesma chave caem sempre na mesma partição, preservando a ordem entre eles. |
| **Maior durabilidade** | Com **Confirmações = todas**, o broker só confirma a gravação depois que as réplicas registram a mensagem, reduzindo o risco de perda quando o cluster tem várias réplicas. |

:::note[Sobre "não duplicar"]
A proteção contra duplicados acontece no envio do CentralOps para o Kafka. Ela **não** garante automaticamente que a sua ferramenta consumidora processe cada evento uma única vez. Quem consome o tópico deve tratar a possibilidade de reprocessamento do lado do consumidor (por exemplo, ignorando um evento já visto). Configure isso com a equipe que opera o consumidor.
:::

## Acompanhar a saúde do destino

Para ver se o destino está saudável, vá em **Normalização → Saúde do Pipeline** e localize o destino Kafka. Ali você acompanha indicadores como:

- **Eventos aceitos** — quantos eventos foram publicados com sucesso.
- **Eventos rejeitados** — quantos falharam (por exemplo, por autenticação ou broker indisponível).
- **Taxa de rejeição** — percentual de eventos rejeitados em relação ao total.
- **Latência** — quanto tempo leva, em média, para publicar um evento.

Um número crescente de eventos rejeitados costuma indicar problema de conexão ou de credencial — veja abaixo.

## Se algo der errado

A tabela abaixo separa o que **você resolve pela interface** do que precisa do **administrador do cluster Kafka**.

| Sintoma | O que fazer |
|---------|-------------|
| **Falha ao conectar / broker inalcançável** no teste de conexão | Confirme se os endereços dos servidores (bootstrap) e a porta estão corretos. Se estiverem corretos e ainda assim não conecta, o broker pode estar fora do ar ou bloqueado por firewall/rede — **contate o administrador do cluster Kafka**. |
| **Tópico não existe** | Confirme o nome do tópico no campo de configuração. Se o nome estiver certo, o tópico ainda não foi criado no cluster — **peça ao administrador do cluster Kafka** para criá-lo, depois teste a conexão de novo. |
| **Credencial inválida / falha de autenticação** | Verifique se o usuário e o mecanismo de autenticação estão corretos para esse cluster. Cadastre novamente a senha e clique em testar conexão. Se persistir, confirme as credenciais com o administrador do cluster Kafka. |
| **Destino inativo até cadastrar a senha** | O destino fica inativo enquanto não houver uma credencial válida. Cadastre a senha no campo indicado e teste a conexão para reativá-lo. |
| **Eventos não estão chegando ao destino** | Verifique se existe uma regra de roteamento ativa apontando para esse destino (**Operação → Roteamento**) e se o filtro não está excluindo os eventos. Acompanhe o caminho em **Operação → Fluxo de dados**. |

:::caution[Quando precisar da equipe de infraestrutura]
A criação do tópico, dos usuários e a disponibilidade do cluster Kafka são responsabilidade de quem opera o Kafka. Esses pontos são definidos fora da plataforma. Se precisar deles, fale com o administrador da plataforma ou com o administrador do cluster Kafka.
:::

## Próximos passos

- **Eventos parando antes de chegar ao destino?** Veja [Quarentena](../operations/quarantine.md).
- **Quer ajustar quais eventos vão para o Kafka?** Veja [Roteamento](./routing.md).
- **Quer ver o caminho dos eventos até o destino?** Use a tela **Operação → Fluxo de dados**.
