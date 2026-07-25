---
sidebar_position: 4
title: Quickstart — do evento ao destino em 15 minutos
description: Conecte sua primeira fonte de segurança ao CentralOps e confirme que os eventos chegam ao seu SIEM, em poucos minutos pela interface.
---

# Quickstart: do evento ao destino em 15 minutos

O CentralOps coleta telemetria de várias fontes de segurança, normaliza tudo para um formato canônico (OCSF) e entrega a um ou mais destinos (SIEMs, data lakes e afins). Este guia mostra o caminho completo: conectar uma fonte, criar o destino, escrever a rota que liga um ao outro e ver o primeiro evento chegar.

## Quando usar

Use este guia quando você for:

- **Conectar sua primeira fonte de segurança** ao CentralOps e quiser confirmar que os eventos realmente chegam ao seu SIEM.
- **Validar uma nova integração** antes de confiar nela em produção: testar credenciais, coleta e entrega de uma só vez.
- **Fazer uma prova de conceito** em um ambiente de SOC: provar para a equipe que a telemetria flui da fonte até o destino sem precisar mexer em infraestrutura.

Tempo estimado: cerca de 15 minutos. Você precisa de uma conta **administradora** no CentralOps.

## Pré-requisitos

- Você tem conta **administradora** no CentralOps.
- Você tem acesso administrativo ao **Sophos Central** da sua organização.
- Você gerou um **Client ID** e um **Client Secret** no Sophos.

Ainda não tem as credenciais do Sophos? Siga o [Guia de integração Sophos](../integrations/sophos.md) e volte aqui.

:::info O caminho tem quatro peças, não duas
Coletar não basta para entregar. Um evento só chega ao seu SIEM se existir **uma integração** que o traga, **um destino** que o receba e **uma rota** que ligue os dois. Não existe destino de fábrica nem regra que mande tudo para algum lugar por padrão: sem uma rota que case, o evento vai para o destino de fallback ou para a DLQ. Os passos 2, 3 e 4 montam exatamente essas três peças.
:::

## Passo 1: obter as credenciais no Sophos (5 min)

No console do Sophos Central:

1. Abra as configurações de credenciais de API da sua organização.
2. Crie uma nova credencial (ou adicione, se já existir uma).
3. Dê um nome reconhecível, como "CentralOps".
4. Conceda ao menos permissão de leitura de alertas (e de casos, se você usa XDR).
5. Copie o **Client ID** e o **Client Secret** e guarde em local seguro.

Anote também:

| Dado | Onde encontrar |
|------|----------------|
| **Region** | Aparece na URL do seu Sophos (por exemplo, `api-eu...` indica a região EU). |
| **Tenant ID** | Nas configurações de organização do Sophos, no campo de identificação. |

## Passo 2: criar a integração de entrada (4 min)

1. No menu lateral, abra **Coleta → Integrações**.
2. Clique no botão para adicionar uma nova integração.
3. Selecione **Sophos**.
4. Preencha os campos:
   - **Nome**: algo que identifique a fonte, como "EDR - Endpoints Corporativos".
   - **Client ID**, **Client Secret**, **Tenant ID** e **Region**: os dados do Passo 1.
   - **Organização**: selecione a organização correta, se você opera em modo multi-organização.
5. Use a opção de **testar a conexão**. A resposta deve indicar sucesso.
6. **Salve** a integração.

![Lista de integrações do CentralOps](/img/console/console-integracoes.png)

Ao salvar, o CentralOps armazena as credenciais de forma cifrada, valida-as junto ao Sophos e agenda a primeira coleta.

## Passo 3: criar o destino de saída (2 min)

O destino é para onde os eventos normalizados vão. **Uma instalação nova não traz nenhum destino pronto**: você cria o primeiro aqui.

1. No menu lateral, abra **Roteia → Destinos** (visível apenas para administradores).
2. Use o botão para adicionar um destino.
3. Escolha o tipo compatível com o seu SIEM. Para Wazuh, use Syslog RFC 5424 ou JSONL; há também Splunk HEC, S3, Elastic, Kafka e outros.
4. Dê um nome ao destino, como "SIEM Primário".
5. Preencha os dados de conexão (endereço, porta e segurança da conexão).
6. **Teste a conexão** e **salve**.

![Destinos configurados](/img/console/console-destinos.png)

:::tip[Quem configura os dados de conexão do destino?]
O endereço, a porta e os certificados do seu SIEM costumam ser definidos pela equipe de infraestrutura no momento do deploy. Se você não souber esses valores, fale com o administrador da plataforma antes de criar o destino.
:::

## Passo 4: criar a rota que liga fonte e destino (2 min)

A rota é a peça que decide **para onde cada evento vai**. Sem ela, o que você coletou no Passo 2 não chega ao destino do Passo 3.

1. No menu lateral, abra **Roteia → Rotas**.
2. Clique para criar uma nova rota.
3. Dê um nome que descreva a intenção, como "Sophos para o SIEM primário".
4. Na condição, filtre pelo fornecedor da integração que você criou.
5. Selecione o destino do Passo 3.
6. Marque a rota como **final** se ela deve encerrar a avaliação, e salve.

![Rotas de entrega](/img/console/console-rotas.png)

As rotas são avaliadas em ordem de prioridade, e **vale a primeira regra que casa**. Uma rota não final continua a avaliação, o que permite entregar o mesmo evento em mais de um lugar.

:::tip
Use o botão **Simular** antes de confiar na regra em produção. Ele roda a condição contra eventos de exemplo e mostra em quais destinos cada um cairia, sem entregar nada.
:::

## Passo 5: aguardar a primeira coleta (2 min)

Logo após salvar, a integração fica **aguardando a primeira coleta**. Aguarde de 2 a 3 minutos. Quando o Sophos retornar alertas, os eventos são normalizados e encaminhados segundo as rotas.

Acompanhe em **Visão geral → Saúde do pipeline**: cada fonte mostra o volume por minuto, quando foi a última coleta e quantos eventos caíram em drift ou quarentena nas últimas 24 horas.

![Saúde do pipeline por fonte](/img/console/console-saude-pipeline.png)

## Passo 6: verificar a entrega (2 min)

Você pode confirmar o sucesso de três formas, da mais definitiva para a de diagnóstico.

### Opção A — no seu destino final (critério de sucesso de ponta a ponta)

Acesse a interface do seu SIEM e procure pelos eventos recém-chegados, filtrando pelo fornecedor "Sophos" ou por um carimbo de data e hora recente. Se o evento aparece normalizado lá, o pipeline está funcionando de ponta a ponta.

### Opção B — pelo Fluxo de dados (se o destino estiver indisponível agora)

Abra **Roteia → Fluxo de dados**. A tela mostra a topologia ao vivo: fontes à esquerda, rotas no meio, destinos à direita, com o volume passando por cada aresta. É o jeito mais rápido de ver onde o fluxo parou.

![Topologia do fluxo de dados](/img/console/console-fluxo.png)

Os quatro cartões do topo separam o que foi **coletado**, **roteado**, **descartado** e **entregue**. Se coletado é maior que zero e entregue é zero, o problema está entre a rota e o destino, não na coleta.

### Opção C — pela Quarentena (somente se algo deu errado)

Se os eventos não aparecem em lugar nenhum:

1. Abra **Normaliza → Quarentena**.
2. Procure por eventos que falharam (erro de mapeamento ou de validação).
3. Abra um evento para ver os detalhes do erro e entender a causa.

Em condições normais, a Quarentena vazia é sinal de que o pipeline está saudável.

## Status esperado ao final

| Item | Status esperado |
|------|-----------------|
| **Integração** | Ativa, sem erro |
| **Destino** | Criado e testado com sucesso |
| **Rota** | Habilitada, ligando a fonte ao destino |
| **Coleta** | Eventos chegando a cada ciclo |
| **Normalização** | Eventos convertidos para OCSF |
| **Entrega** | Eventos chegando ao destino |
| **Quarentena** | Vazia (ou apenas com erros de configuração, não de entrega) |

O volume de eventos por ciclo depende inteiramente do que a sua fonte está gerando: não há um número fixo esperado.

## Próximos passos

**Explorar múltiplos destinos e roteamento:**

- [Destinos](../outputs/destinations.md) para configurar S3, Splunk, Elastic, Kafka e outros.
- [Roteamento](../outputs/routing.md) para criar regras mais finas, como eventos críticos no SIEM principal e logs verbosos num destino mais barato.

**Aprofundar coleta e normalização:**

- [Dashboard](../operations/dashboard.md) para indicadores, volume e saúde.
- [Quarentena](../operations/quarantine.md) para análise e reprocessamento de eventos com erro.
- [Mapeamentos](../normalization/overview.md) para ajustar as regras de normalização.
- [Mais integrações](../integrations/overview.md) para adicionar outras fontes.

## Solução de problemas

### A integração não coleta

1. Abra **Coleta → Integrações** e clique na integração.
2. Verifique o estado e o indicador de saúde.
3. Abra os detalhes de erro para entender por que a coleta falhou (por exemplo, credenciais rejeitadas ou limite de requisições atingido).

### Erro de autenticação (credenciais rejeitadas)

O Sophos recusou as credenciais. Verifique se o **Client ID**, o **Client Secret**, o **Tenant ID** e a **Region** estão corretos, e se as credenciais continuam ativas no Sophos Central. Para corrigir, edite a integração em **Coleta → Integrações** e salve novamente.

### Os eventos são coletados, mas não chegam ao destino

Esta é a falha mais comum de quem está começando, e quase sempre é a rota.

1. Abra **Roteia → Rotas** e confirme que existe uma rota habilitada cuja condição case com os eventos dessa fonte. Use **Simular** para verificar.
2. Se a rota está certa, abra **Roteia → Destinos**, clique no destino e veja as métricas de entrega. Há erro de conexão?
3. Use **testar a conexão** para validar endereço e credenciais.
4. Confirme com o administrador que a rede entre o CentralOps e o destino está liberada.

### A Quarentena está cheia de eventos

Isso indica falha na normalização ou na validação. Consulte a [solução de problemas de normalização](../normalization/troubleshooting.md).

### Nenhum evento em lugar nenhum

A primeira coleta pode levar alguns minutos. Se continuar vazio após 5 a 10 minutos, reveja as credenciais (Passo 2) e confirme em **Visão geral → Saúde do pipeline** se a fonte registra alguma coleta.
