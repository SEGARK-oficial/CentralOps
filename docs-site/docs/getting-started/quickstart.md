---
sidebar_position: 4
title: Quickstart — do evento ao destino em 15 minutos
description: Conecte sua primeira fonte de segurança ao CentralOps e confirme que os eventos chegam ao seu SIEM, em poucos minutos pela interface.
---

# Quickstart: do evento ao destino em 15 minutos

O CentralOps coleta telemetria de várias fontes de segurança, normaliza tudo para um formato canônico (OCSF) e entrega a um ou mais destinos (SIEMs, data lakes etc.). Este guia mostra, de ponta a ponta, como conectar uma fonte (Sophos), confirmar para onde os eventos são entregues e ver o primeiro evento chegar no seu destino.

## Quando usar

Use este guia quando você for:

- **Conectar sua primeira fonte de segurança** ao CentralOps e quiser confirmar que os eventos realmente chegam ao seu SIEM.
- **Validar uma nova integração Sophos** antes de confiar nela em produção — testar credenciais, coleta e entrega de uma só vez.
- **Fazer uma prova de conceito** em um ambiente de SOC: provar para a equipe que a telemetria flui da fonte até o destino sem precisar mexer em infraestrutura.

Tempo estimado: cerca de 15 minutos. Você precisa de uma conta **administradora** no CentralOps.

## Pré-requisitos

- Você tem conta **administradora** no CentralOps.
- Você tem acesso administrativo ao **Sophos Central** da sua organização.
- Você gerou um **Client ID** e um **Client Secret** no Sophos.

Ainda não tem as credenciais do Sophos? Siga o [Guia de integração Sophos](../integrations/sophos.md) e volte aqui.

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

## Passo 2: criar a integração de entrada no CentralOps (5 min)

1. No menu lateral, abra **Visão geral → Integrações**.
2. Clique no botão para adicionar uma nova integração.
3. Selecione **Sophos**.
4. Preencha os campos:
   - **Nome**: algo que identifique a fonte, como "Sophos - Acme Corp".
   - **Client ID**, **Client Secret**, **Tenant ID** e **Region**: os dados do Passo 1.
   - **Organização**: deixe em branco ou selecione a organização correta, se você opera em modo multi-organização.
5. Use a opção de **testar a conexão**. A resposta deve indicar sucesso.
6. **Salve** a integração.

Ao salvar, o CentralOps:

- Armazena as credenciais de forma cifrada.
- Valida as credenciais junto ao Sophos.
- Agenda automaticamente a primeira coleta (acontece nos próximos minutos).

## Passo 3: conferir o destino de saída padrão (1 min)

O CentralOps já vem com um destino padrão para o Wazuh configurado. É para lá que os eventos são entregues por padrão.

Para confirmar que o destino está ativo:

1. No menu lateral, abra **Operação → Destinos** (visível apenas para administradores).
2. Localize o destino padrão do Wazuh na lista.
3. Confirme que o status está **Ativo**.

Se o destino não existir ou estiver desativado, crie um novo:

- Use o botão para adicionar um destino.
- Escolha o tipo compatível com o seu Wazuh (Syslog RFC 5424 ou JSONL — ambos funcionam).
- Dê um nome ao destino.
- Preencha os dados de conexão do seu Wazuh (endereço, porta e segurança da conexão).
- **Teste a conexão** e **salve**.

:::tip Quem configura os dados de conexão do destino?
O endereço, a porta e os certificados do seu Wazuh costumam ser definidos pela equipe de infraestrutura no momento do deploy. Se você não souber esses valores, fale com o administrador da plataforma antes de criar o destino.
:::

## Passo 4: aguardar a primeira coleta (3 min)

Logo após salvar, a integração fica no estado de **aguardando a primeira coleta**.

Aguarde de 2 a 3 minutos. O sistema agenda a primeira coleta automaticamente. Quando o Sophos retornar alertas:

- O estado da integração passa para **Ativo**.
- O indicador de saúde fica verde.
- Os eventos são normalizados e encaminhados para os destinos.

Você acompanha tudo isso na tela **Visão geral → Integrações**: clique na integração para ver o estado, a saúde e o histórico de coletas.

## Passo 5: verificar a entrega no destino (2 min)

Você pode confirmar o sucesso de três formas, da mais definitiva para a de diagnóstico.

### Opção A — no seu destino final (critério de sucesso de ponta a ponta)

Acesse a interface do seu destino (Wazuh, Splunk, Elastic etc.) e procure pelos eventos recém-chegados, filtrando pelo fornecedor "Sophos" ou por um carimbo de data/hora recente com a origem CentralOps.

Se o evento coletado do Sophos aparece normalizado no seu destino, o pipeline está funcionando de ponta a ponta.

### Opção B — pelo Dashboard do CentralOps (se o destino estiver indisponível agora)

1. No menu lateral, abra **Visão geral → Dashboard**.
2. Verifique o card de **últimos eventos normalizados** para confirmar que eventos foram processados.
3. Verifique o card de **saúde dos destinos** para ver se houve algum erro de entrega.

### Opção C — pela Quarentena (somente se algo deu errado)

Se os eventos não aparecem em lugar nenhum:

1. No menu lateral, abra **Normalização → Quarentena**.
2. Procure por eventos que falharam (erro de mapeamento ou de validação).
3. Abra um evento para ver os detalhes do erro e entender a causa.

Em condições normais, a Quarentena vazia é sinal de que o pipeline está saudável.

## Status esperado após 15 minutos

| Item | Status esperado |
|------|-----------------|
| **Integração Sophos** | Ativa, com saúde verde |
| **Destino padrão do Wazuh** | Ativo e testado |
| **Coleta** | Eventos chegando a cada ciclo de coleta |
| **Normalização** | Eventos convertidos para OCSF |
| **Entrega** | Eventos chegando ao destino (Wazuh, Splunk, Elastic etc.) |
| **Quarentena** | Vazia (ou apenas com erros de configuração, não de entrega) |

> O volume de eventos por ciclo depende inteiramente do que a sua fonte está gerando — não há um número fixo esperado.

## Próximos passos

**Explorar múltiplos destinos e roteamento:**

- [Destinos](../outputs/destinations.md) — configurar S3, Splunk, Elastic, Kafka e outros.
- [Roteamento](../outputs/routing.md) — criar regras (por exemplo, eventos críticos para o SIEM principal; logs verbosos para um destino mais barato).

**Aprofundar coleta e normalização:**

- [Dashboard](../operations/dashboard.md) — indicadores, volume e saúde.
- [Quarentena](../operations/quarantine.md) — análise e reprocessamento de eventos com erro.
- [Mappings](../normalization/overview.md) — ajustar as regras de normalização.
- [Mais integrações](../integrations/overview.md) — adicionar outras fontes (Wazuh, Defender etc.).

## Solução de problemas

### A integração não coleta

1. Abra **Visão geral → Integrações** e clique na integração.
2. Verifique o estado e o indicador de saúde.
3. Abra os detalhes de erro da integração para entender por que a coleta falhou (por exemplo, credenciais rejeitadas ou limite de requisições atingido).

### Erro de autenticação (credenciais rejeitadas)

O Sophos recusou as credenciais. Verifique:

- O **Client ID** e o **Client Secret** estão corretos?
- O **Tenant ID** está correto?
- A **Region** está correta?
- As credenciais ainda estão ativas no Sophos Central?

Para corrigir, edite a integração em **Visão geral → Integrações** e salve novamente com os dados corretos.

### Aparecem eventos no Dashboard, mas não chegam ao destino

1. Em **Operação → Destinos**, clique no destino e veja suas métricas de entrega (há erros de conexão?).
2. Se houver erro, use a opção de **testar a conexão** para validar o endereço e as credenciais.
3. Confirme com o administrador da plataforma que a conexão de rede entre o CentralOps e o destino está liberada.

### A Quarentena está cheia de eventos

Isso indica falha na normalização ou na validação dos eventos. Consulte a [solução de problemas de normalização](../normalization/troubleshooting.md).

### Nenhum evento no Dashboard

A primeira coleta pode levar alguns minutos. Se continuar vazio após 5 a 10 minutos:

- A integração exibe algum erro? Reveja as credenciais (Passo 2).
- A saúde da integração está verde? Se não, veja a seção acima.
- O destino foi testado com sucesso? Volte ao Passo 3.
