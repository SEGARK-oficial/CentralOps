---
sidebar_position: 2
title: Integração ativa não está coletando
description: O que verificar quando uma integração marcada como ativa para de trazer eventos
---

# Integração ativa não está coletando

Quando uma integração aparece como **Ativo**, o CentralOps deveria buscar eventos dela de tempos em tempos automaticamente. Esta página ajuda você a entender o que olhar quando uma integração ativa para de trazer dados (por exemplo, a última coleta foi há mais de 15 minutos) e a decidir o que resolver pela interface e quando acionar o administrador da plataforma.

## Quando usar

- A integração da **Sophos** (ou de outro fornecedor) está como **Ativo**, mas você percebe que nenhum evento novo apareceu na tela de **Investigações** há um bom tempo.
- Em uma investigação você nota um "buraco" na linha do tempo — eventos de uma fonte simplesmente sumiram a partir de certo horário, mesmo a integração continuando ativa.
- O painel inicial mostra uma fonte com o indicador de coleta atrasado e você precisa saber se é problema de credencial, de destino lento ou algo que só a equipe de plataforma resolve.

## Como o fluxo funciona (para entender o sintoma)

O CentralOps trabalha como uma esteira: **coleta** os eventos na origem, **normaliza** (padroniza os campos), **roteia** por regra e **entrega** nos destinos.

Por isso "coletou" e "entregou" são coisas diferentes. Uma integração pode estar coletando normalmente e, ainda assim, os eventos demorarem a chegar ao destino final porque a etapa de entrega está lenta. Saber em qual etapa está o atraso ajuda a escolher o caminho certo abaixo.

## Passo 1: confirme o estado da integração

1. Abra o menu **Visão geral -> Integrações**.
2. Clique na integração que parou de coletar.
3. Verifique dois campos:
   - **Estado**: deve estar **Ativo**. Se estiver **Pausado** ou **Erro**, esse é o problema principal.
   - **Última coleta**: deve ser recente (poucos minutos). Se estiver muito atrasada, siga para os cenários abaixo.

## Passo 2: confira a saúde geral da coleta

Abra o menu **Normalização -> Saúde do Pipeline**. Essa tela mostra, de forma consolidada, se o processamento em segundo plano está fluindo e se há acúmulo de eventos esperando para serem entregues. Use-a para distinguir:

- **Problema só naquela integração** (credencial, fonte indisponível) -> trate na própria integração, veja os cenários abaixo.
- **Acúmulo geral / entrega lenta para vários destinos** -> provável gargalo na entrega; veja o Cenário C e, se persistir, acione o administrador.

## Cenários e o que fazer

### Cenário A: a integração está em Erro ou Pausada

| Estado | O que significa | O que fazer pela UI |
| --- | --- | --- |
| **Pausado** | A coleta foi suspensa manualmente. | Em **Visão geral -> Integrações**, abra a integração e reative a coleta. |
| **Erro** | O CentralOps tentou coletar e falhou (em geral, problema de conexão ou credencial). | Veja o Cenário B. |

### Cenário B: credenciais expiradas ou inválidas

**Sintoma**: a integração fica em **Erro**, normalmente com uma mensagem de não autorizado (por exemplo, "401").

**Como resolver pela UI**:

1. Abra **Visão geral -> Integrações** e clique na integração.
2. Edite as credenciais e informe os novos dados (por exemplo, um novo segredo de cliente da Sophos).
3. Use a opção de **testar a conexão** antes de salvar.
4. Se o teste passar, salve.

O estado volta para **Ativo** e a coleta costuma retomar em pouco tempo. Se o teste continuar falhando mesmo com credenciais novas, confirme com quem administra a conta do fornecedor se o acesso ainda é válido.

### Cenário C: coleta funciona, mas os eventos demoram a chegar ao destino

**Sintoma**: a **Última coleta** está recente (ou seja, a integração está coletando), mas os eventos processados aparecem atrasados nos destinos.

Isso aponta para a etapa de **entrega**, não de coleta. Um destino lento ou indisponível segura o envio e gera acúmulo.

**Como investigar pela UI**:

1. Abra **Normalização -> Saúde do Pipeline** e veja se há acúmulo de eventos aguardando entrega.
2. (Apenas administrador) Abra **Operação -> Destinos** e verifique se algum destino está com a proteção contra destino instável acionada ou com a fila de reenvio crescendo. Nessa tela é possível acompanhar e reprocessar os eventos que ficaram retidos.
3. (Apenas administrador) Abra **Operação -> Roteamento** para confirmar para quais destinos os eventos daquela fonte estão sendo enviados.

Se o destino (por exemplo, Wazuh, Splunk ou um armazenamento em nuvem) estiver indisponível ou recusando os eventos, a entrega só normaliza quando o destino voltar. Enquanto isso, os eventos ficam na **fila de reenvio** e são reenviados automaticamente. Se a fila não diminuir sozinha após o destino voltar, acione o administrador da plataforma.

### Cenário D: a integração sumiu da lista

**Sintoma**: a integração que coletava não aparece mais em **Visão geral -> Integrações**.

**Como resolver pela UI**:

1. Recadastre a integração em **Visão geral -> Integrações**, preenchendo novamente os dados de acesso.
2. Salve. Em poucos instantes o CentralOps volta a agendar a coleta dessa fonte automaticamente.

### Cenário E: nada acima resolve

Se o estado está **Ativo**, as credenciais estão válidas, não há destino travado e mesmo assim a coleta não retoma, o problema provavelmente está na infraestrutura de processamento em segundo plano (fora do alcance da interface).

Nesse caso, acione o administrador da plataforma com estas informações:

- Nome da integração afetada e horário aproximado em que a coleta parou.
- O que você já verificou (estado, credenciais, destinos).
- Uma captura da tela **Normalização -> Saúde do Pipeline**.

O agendamento da coleta e o processamento em segundo plano são definidos pela equipe de infraestrutura no momento do deploy. Se precisar alterá-los, fale com o administrador da plataforma.

## Prevenção

- Acompanhe regularmente a tela **Normalização -> Saúde do Pipeline** para perceber atrasos antes que virem buracos na coleta.
- Mantenha as credenciais das integrações renovadas: muitos fornecedores expiram segredos periodicamente.
- Quando o produto roteia uma fonte para vários destinos ao mesmo tempo, lembre que um único destino lento pode atrasar a entrega — por isso vale checar **Operação -> Destinos** (administrador) ao investigar atrasos.

## Próximos passos

- **Acompanhar a saúde do processamento?** Abra **Normalização -> Saúde do Pipeline**.
- **Revisar uma integração com problema?** Abra **Visão geral -> Integrações**.
- **Conferir para onde os eventos vão?** (administrador) Abra **Operação -> Roteamento** e **Operação -> Destinos**.
