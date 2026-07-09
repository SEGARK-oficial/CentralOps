---
sidebar_position: 3
title: Wazuh
description: Colete alertas do Wazuh e, se quiser, envie eventos de volta para o Wazuh como destino.
---

# Wazuh

O CentralOps conecta-se ao seu **Wazuh** para coletar alertas automaticamente, normalizá-los e disponibilizá-los para busca, investigação e roteamento. O **Wazuh é uma FONTE** de detecções (o CentralOps puxa do Indexer) e **opcionalmente também um DESTINO**, recebendo de volta eventos já normalizados.

## Quando usar

- **Centralizar vários ambientes Wazuh** — sua organização tem mais de um Wazuh (por região, cliente ou unidade) e você quer ver todos os alertas em um só lugar, com um painel único.
- **Normalizar antes de investigar** — os alertas do Wazuh chegam em formatos variados; o CentralOps padroniza os campos (data/hora, severidade, título, etc.) para que o analista busque e correlacione sem se preocupar com o formato de origem.
- **Reaproveitar o Wazuh como destino** — depois de normalizar os eventos, você quer reenviá-los para o Wazuh (ou para outros destinos como Splunk e S3 ao mesmo tempo) para manter uma cópia consistente.

## O que você precisa antes de começar

Você vai precisar dos dados de acesso ao Wazuh, que normalmente são fornecidos pelo seu time de infraestrutura. Solicite-os antes de iniciar:

| Item | Para que serve |
| --- | --- |
| **URL do Manager** + usuário e senha | Permite ao CentralOps consultar agentes e índices. |
| **URL do Indexer** + usuário e senha | É de onde os alertas são lidos. |
| **Certificado TLS** (opcional) | Necessário apenas se o Wazuh usar um certificado próprio (não público). |

:::info
Os certificados, endereços de rede e credenciais de serviço são definidos pela equipe de infraestrutura no momento do deploy. Se algum desses dados precisar ser ajustado, fale com o administrador da plataforma.
:::

## Passo 1: Criar a integração de coleta

1. Acesse o menu **Visão geral → Integrações**.
2. Clique em **Adicionar integração**.
3. No seletor de plataforma, escolha **Wazuh** e avance.
4. Preencha os dados do **Manager**:
   - **Nome** da integração (ex.: `Wazuh - Produção`).
   - **URL do Manager** e as credenciais de acesso.
   - Deixe a verificação de SSL **marcada** (recomendado).
5. Preencha os dados do **Indexer**:
   - **URL do Indexer** e as credenciais de acesso.
   - O campo de certificado pode ficar vazio quando o Wazuh usa um certificado público. Se o seu ambiente usa um certificado próprio, peça orientação ao administrador da plataforma, pois esse certificado é provisionado no momento do deploy.
6. Clique em **Testar conexão**. O CentralOps verifica:
   - Se consegue alcançar o Manager e o Indexer.
   - Se as credenciais estão corretas.
   - Se o certificado é válido.
7. Com o teste bem-sucedido, clique em **Salvar**.

## Passo 2: Confirmar que a coleta está funcionando

1. Acesse **Normalização → Saúde do Pipeline** e localize a integração Wazuh. O status deve aparecer como **Ativo**.
2. Acesse **Operação → Investigações** (a tela de busca) e filtre por `platform:wazuh`.
3. Você verá os alertas coletados do Wazuh já normalizados no formato canônico do CentralOps.

**O que o CentralOps coleta:** a cada poucos minutos, ele lê os alertas mais recentes do Wazuh Indexer e os normaliza automaticamente — convertendo campos como data/hora, severidade e título para o formato padronizado da plataforma. Você não precisa configurar nada para isso: a coleta começa assim que a integração é salva e validada.

:::warning Anti-loop: Wazuh como fonte e destino
Um evento **coletado de um Wazuh nunca é reenviado para o mesmo Wazuh**. Internamente, o CentralOps suprime eventos de fonte Wazuh quando seriam entregues ao destino padrão de segurança (wazuh-default) ou a qualquer destino syslog que aponte de volta ao manager do qual o evento foi coletado. Sem essa supressão, o evento seria reindexado no Wazuh → recoletado como novo → entregue novamente, causando um loop infinito.

Se você roteá-lo para um syslog cujo host coincida com o host do manager Wazuh, ele será suprimido (contabilizado como `loop_blocked` em log). Isso é **intencional e não é uma perda** — é um design para quebrar o acoplamento bidirecional. Se você quiser redirecionar eventos coletados de um Wazuh A para outro Wazuh B, use endereços diferentes.
:::

## Passo 3 (opcional): Usar o Wazuh como destino

A partir da versão 2.0, o CentralOps roteia eventos normalizados para **vários destinos em paralelo** (Wazuh, Splunk, S3, Sentinel e outros). O Wazuh é apenas um dos destinos possíveis — não o destino único.

:::note Disponível apenas para administradores
As telas **Destinos**, **Roteamento** e **Fluxo de dados** só aparecem para administradores da plataforma. Se você não as vê no menu, fale com um administrador.
:::

### 3.1 Criar o destino Wazuh

1. Acesse **Operação → Destinos**.
2. Clique em **Adicionar destino**.
3. Escolha o tipo de envio: **Syslog RFC 5424** (recomendado para Wazuh) ou **JSONL** como alternativa.
4. Preencha:
   - **Nome** do destino (ex.: `Wazuh Produção`).
   - **Host** e **Porta** do Wazuh.
   - **Protocolo**: TLS, com a verificação de TLS marcada (recomendado).
5. Clique em **Testar conexão** e, em seguida, em **Salvar**.

### 3.2 Criar a regra de roteamento

A regra de roteamento define **quais eventos** vão para o destino Wazuh.

1. Acesse **Operação → Roteamento**.
2. Clique em **Adicionar rota**.
3. Configure a regra:
   - **Nome** (ex.: `Enviar ao Wazuh`).
   - **Condição**: deixe vazia para enviar todos os eventos, ou defina um filtro (ex.: apenas eventos de severidade alta).
   - **Destino**: selecione o destino Wazuh criado no passo anterior.
   - Marque a opção de **rota final** se quiser que o processamento pare após esta rota.
4. Clique em **Salvar**.

A partir daí, todos os eventos que atenderem à condição serão enviados ao Wazuh.

### 3.3 Verificar a entrega

No Wazuh, procure pelos alertas mais recentes. Os eventos enviados pelo CentralOps incluem um bloco de **metadados internos do evento**, com informações de origem como a integração de coleta, a plataforma de origem e o horário da coleta. Esses metadados ajudam a identificar que o evento passou pelo CentralOps.

## Enviar para vários destinos ao mesmo tempo

O CentralOps permite **envio simultâneo a vários destinos** — o mesmo evento pode ir para o Wazuh e para outros destinos. Basta criar mais de uma rota.

**Exemplo 1 — Wazuh e Splunk juntos**

1. Crie o destino Wazuh (conforme o Passo 3).
2. Crie o destino Splunk (veja a [documentação do Splunk](../outputs/destination-splunk-hec.md)).
3. Crie **duas rotas** sem condição (todos os eventos): uma para o Wazuh (não final) e outra para o Splunk (final).
4. Resultado: todos os eventos vão para os dois destinos.

**Exemplo 2 — Wazuh só para alertas críticos; S3 para tudo**

1. Crie o destino Wazuh.
2. Crie o destino S3.
3. Crie **duas rotas**: a primeira com a condição de severidade alta enviando ao Wazuh (final); a segunda sem condição enviando ao S3 (captura tudo).
4. Resultado: alertas de severidade alta vão para o Wazuh; todo o restante vai para o S3.

Para mais detalhes sobre condições e roteamento, veja [Roteamento](../outputs/routing.md) e [Destinos](../outputs/destinations.md).

## Solução de problemas

| Sintoma | O que fazer |
| --- | --- |
| **Falha ao conectar** ("connection refused") | Confirme que a **URL** está correta (incluindo `https://`). Se o erro persistir, pode haver bloqueio de rede entre o CentralOps e o Wazuh — fale com o administrador da plataforma. |
| **Credenciais inválidas** (não autorizado) | Verifique se o usuário e a senha estão corretos e se o usuário tem permissão de leitura no Wazuh. Se a senha foi alterada recentemente, atualize-a na integração. |
| **Erro de certificado SSL** | Se o Wazuh usa um certificado próprio, o certificado correto precisa estar provisionado pela equipe de infraestrutura — fale com o administrador da plataforma. Não desative a verificação de SSL em produção. |
| **Indexer sobrecarregado** (muitas requisições ou cluster instável) | Esse é um limite do lado do Wazuh, geralmente por falta de espaço ou capacidade. Acione o administrador do Wazuh para liberar espaço ou aumentar a capacidade do Indexer. |
| **Eventos coletados, mas não chegam no Wazuh como destino** | Em **Operação → Roteamento**, confirme que existe uma rota apontando para o destino Wazuh e que a condição inclui os seus eventos. Em **Operação → Destinos**, confirme que o destino Wazuh está saudável. Para mais sinais, veja **Normalização → Saúde do Pipeline**. |

## Próximos passos

- **Confirmar que os dados estão chegando?** Veja o [Dashboard](../operations/dashboard.md) (menu **Visão geral → Dashboard**).
- **Configurar roteamento condicional?** Veja [Roteamento](../outputs/routing.md).
- **Explorar outros destinos?** Veja [Destinos](../outputs/destinations.md).
- **Alertas em quarentena?** Veja [Solução de problemas de normalização](../normalization/troubleshooting.md).
- **Adicionar mais integrações?** Veja a [Visão geral de Integrações](./overview.md).
