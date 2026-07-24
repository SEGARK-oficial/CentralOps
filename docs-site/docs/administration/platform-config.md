---
sidebar_position: 3
title: Configuração da plataforma
description: O que o administrador configura na interface — coletores, destinos, roteamento, identidade e segredos
---

# Configuração da plataforma

A configuração do CentralOps é feita em telas separadas, cada uma com sua própria função: coleta de eventos, destinos de saída, regras de roteamento e identidade (login). Esta página explica o que cada tela faz e como o administrador opera por ela.

**Público**: somente administradores da plataforma. Todas as ações descritas aqui são feitas pela interface web — você nunca precisa de terminal nem de comandos.

## Quando usar

- **Conectar um SIEM novo**: você acabou de contratar um Splunk (ou Elastic, S3, Sentinel) e quer que os eventos comecem a chegar lá, sem parar o envio atual para o Wazuh.
- **Separar dados sensíveis**: precisa mandar logs de endpoint para o time de detecção e, ao mesmo tempo, uma cópia anonimizada (sem PII) para um data lake, atendendo LGPD/GDPR.
- **Habilitar login corporativo**: o SOC vai parar de usar senhas locais e passar a entrar com a conta Microsoft da empresa (Microsoft 365 / Entra).

## Onde fica cada coisa

Não existe uma única tela "Configuração" que reúna tudo. Cada área tem seu próprio lugar no menu lateral:

| O que você quer fazer | Onde ir (menu lateral) |
|-----------------------|------------------------|
| Configurar a coleta e o envio dos eventos | **Operação → Collectors** |
| Cadastrar para onde os eventos vão (SIEM, lake, etc.) | **Operação → Destinos** |
| Definir as regras de quem recebe o quê | **Operação → Roteamento** |
| Ver o fluxo dos dados ponta a ponta | **Operação → Fluxo de dados** |
| Configurar login corporativo (Microsoft) | **Administração → Configurações** |
| Inspecionar ao vivo o que está entrando e saindo, evento a evento | **Administração → Configurações → Captura ao vivo** |

As telas **Destinos**, **Roteamento** e **Fluxo de dados** só aparecem para administradores.

## Collectors — coleta e envio

Na tela **Operação → Collectors** você controla como o CentralOps recebe os eventos das integrações e como os entrega ao destino principal.

O que você pode ajustar por essa tela:

| Parâmetro | O que significa |
|-----------|-----------------|
| Endereço e porta de saída | Para onde o fluxo principal é enviado (normalmente o seu Wazuh). |
| Conexão segura (TLS) | Liga a criptografia no transporte dos eventos. |
| Modo de envio | **Direto** (envio contínuo ao destino), **arquivo local** (grava em disco como reserva) ou **ambos** (redundância). |
| Tamanho do lote e intervalo | Quantos eventos agrupar antes de enviar e de quanto em quanto tempo. |
| Remoção de duplicados | Por quanto tempo eventos idênticos são considerados repetidos e descartados. |
| Limites de taxa por fornecedor | Quantos eventos por minuto cada integração (Sophos, Microsoft Defender, NinjaOne, etc.) pode enviar. |

**Testar antes de salvar**: a tela permite validar a conexão com o destino na hora, sem reiniciar nada. Faça isso sempre que mudar endereço, porta ou TLS.

:::note[Limites de taxa]
Os limites de taxa por fornecedor têm um valor padrão definido pela equipe de infraestrutura no momento do deploy. Se precisar de um limite diferente, fale com o administrador da plataforma.
:::

## Destinos — para onde os eventos vão

Na tela **Operação → Destinos** você cadastra cada lugar que vai receber os eventos. Um destino é uma saída: um SIEM, um lake, uma fila ou um coletor de observabilidade.

Tipos de destino disponíveis hoje:

| Tipo | Uso típico |
|------|-----------|
| Syslog (formato moderno ou legado) | Enviar para SIEM ou concentrador que fala Syslog. |
| Arquivo JSON Lines | Gravar eventos em arquivo (local ou em S3). |
| Splunk | Enviar via HTTP Event Collector do Splunk. |
| Elasticsearch | Indexar via Bulk API. |
| Amazon S3 / MinIO | Arquivar em bucket de objeto. |
| Microsoft Sentinel | Enviar para a tabela do Sentinel. |
| Apache Kafka | Publicar em um tópico Kafka. |
| OpenTelemetry (OTLP) | Encaminhar logs/traces/métricas a um coletor OTel. |

### Como cadastrar um destino

1. Vá em **Operação → Destinos** e abra o formulário de novo destino.
2. Escolha o tipo (Splunk, S3, etc.). Os campos mudam conforme o tipo.
3. Preencha endereço, credenciais e demais opções.
4. Use o botão de **testar conexão** — ele faz um envio de prova ao vivo, sem salvar.
5. Salve quando o teste passar.

**Credenciais ficam protegidas**: tokens, chaves de API e segredos que você informa nunca são exibidos de volta na tela nem em registros. A tela mostra apenas um indicador de **configurado / não configurado** para você saber se a credencial já está salva.

**Rota automática**: ao criar um destino, o CentralOps gera automaticamente uma regra que envia tudo para ele. Você pode refinar essa regra depois na tela de **Roteamento**.

Para detalhes de cada tipo de destino, veja [Destinos](../outputs/destinations.md).

## Roteamento — quem recebe o quê

Na tela **Operação → Roteamento** você define as regras que decidem para quais destinos cada evento vai. As regras são avaliadas em ordem, de cima para baixo, e a primeira que combina vence.

Cada regra tem:

| Campo | O que faz |
|-------|-----------|
| Ordem (prioridade) | Define quem é avaliado primeiro. |
| Condição | O critério de combinação (ex.: por fornecedor, tipo de evento, IP de origem). |
| Ação | **Enviar** ao destino, **descartar** o evento, ou **copiar** para vários destinos (envio simultâneo). |
| Destinos | A lista de destinos que recebem o evento quando a regra combina. |
| Parar ou continuar | Se a regra encerra a avaliação ou deixa o evento seguir para as próximas (envio simultâneo a vários destinos). |
| Anonimização (PII) | Mascarar, pseudonimizar ou remover campos sensíveis antes da entrega. |

**Rede de segurança (catch-all que você configura)**: o catch-all não é fixo em nenhum produto — você o define. Há duas formas, e qualquer destino (Elastic, S3, Splunk, Sentinel, syslog, Wazuh…) pode assumir o papel: uma **regra pega-tudo** (condição vazia, menor prioridade) ou marcar um **destino como padrão**. Se um evento não combina com regra alguma e não há catch-all configurado, ele **não some nem vai para um destino oculto**: cai na fila de reenvio (DLQ) com o motivo `unrouted`, visível e reprocessável depois que você criar a regra pega-tudo ou marcar um destino como padrão.

### Caso de uso: cópia anonimizada para um lake

1. Crie o destino do lake em **Operação → Destinos** (por exemplo, um bucket S3).
2. Em **Operação → Roteamento**, crie uma regra que copia os eventos de endpoint para esse destino.
3. Na própria regra, ative a anonimização para mascarar os campos sensíveis (PII) antes da entrega.
4. Os mesmos eventos continuam indo, íntegros, para o seu SIEM principal pela regra correspondente (ou pelo catch-all que você configurou como destino padrão).

**Anonimização de PII**: quando você ativa a anonimização, os campos sensíveis são mascarados antes de sair, atendendo LGPD/GDPR. A proteção é "à prova de falha": se a anonimização não puder ser aplicada com segurança, o evento não sai em texto puro — ele cai na regra final e segue íntegro apenas para o destino padrão. Se a tela recusar salvar uma regra com anonimização, é sinal de que esse recurso não está habilitado neste ambiente; nesse caso, fale com o administrador da plataforma.

### Validar antes de aplicar

Antes de ativar uma regra nova, use o recurso de **simulação** na tela de Roteamento: ele mostra para quais destinos um evento de exemplo iria, sem entregar de verdade. Se algo sair errado depois, a tela também permite reverter a última alteração.

Para mais detalhes, veja [Roteamento](../outputs/routing.md), [Simulação de roteamento](../outputs/routing-dry-run.md) e [Anonimização de PII](../outputs/pii-redaction.md).

## Identidade e login corporativo (Microsoft Entra)

Na tela **Administração → Configurações** o administrador habilita o login com a conta Microsoft da empresa (Microsoft 365 / Entra), no lugar de senhas locais.

O que você informa por essa tela:

| Campo | O que é |
|-------|---------|
| Identificador do tenant e do aplicativo | Os dados do registro do CentralOps no Entra. |
| Segredo do aplicativo | A credencial do app. Fica criptografada e nunca é exibida de volta — a tela mostra apenas **configurado / não configurado**. |
| Mapeamento de papéis | Quais grupos do Entra viram administrador, engenheiro, etc. dentro do CentralOps. |
| Papel padrão | Qual papel um usuário recebe se não cair em nenhum grupo mapeado. |

**Testar a conexão**: a tela tem uma ação para validar a configuração com o Entra na hora, confirmando que tenant, aplicativo e segredo estão corretos antes de liberar o login para todos.

**Como fica para o usuário**: com o login corporativo habilitado, a tela de entrada passa a oferecer a opção "Entrar com Microsoft".

**Sincronizar usuários do Entra**: a importação automática de usuários a partir do Microsoft Entra está no roadmap e ainda não está disponível para uso geral. Por enquanto, gerencie contas em **Administração → Usuários**.

## Captura ao vivo (diagnóstico de entrega)

Na aba **Administração → Configurações → Captura ao vivo** você grava, por uma janela definida, uma amostra do tráfego real que passa pelo pipeline. A sessão **expira sozinha** ao fim da janela — não fica gravando esquecida.

Para cada evento a tela mostra o payload **como o fornecedor mandou** e **como está sendo entregue** (já normalizado), além do **desfecho**: entregue, descartado por regra, sem rota, em quarentena — e também **suprimido** e **amostrado para fora**, que não aparecem nos contadores de evento da regra (Bateram / Enviados / Descartados) — deles só o volume agregado, em bytes, aparece no card **Redução de volume & custo** do Fluxo de dados. É o caminho para responder "por que esse evento não chegou no meu SIEM?" sem depender de log de servidor.

A sessão pode ser baixada em **CSV** ou **NDJSON**, com os dados pessoais mascarados por padrão. A tela e a exportação são **exclusivas do administrador**.

Detalhes de uso, limites e privacidade em [Captura ao vivo](../operations/live-capture.md).

## Segredos e criptografia (visão do administrador)

O CentralOps guarda todas as credenciais sensíveis (tokens de destino, segredos de SSO) criptografadas. Você **não** gerencia chaves de criptografia pela interface — o que você vê nas telas é apenas o indicador de **configurado / não configurado** ao lado de cada credencial, mostrando se ela já foi salva com segurança.

A escolha do mecanismo de criptografia (cofre local ou um serviço de chaves corporativo como o HashiCorp Vault) e a chave-mestra da plataforma são definidas pela equipe de infraestrutura no momento do deploy. Não há, e não deve haver, opção de "rotacionar chave-mestra" na interface — isso é uma operação de infraestrutura.

:::note
A configuração de criptografia, da chave-mestra da plataforma e do cofre de segredos é definida pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma.
:::

## Sessão e segurança de acesso

O tempo de expiração da sessão (logout por inatividade), as proteções de cookie e o bloqueio temporário após várias tentativas de login falhas são definidos pela equipe de infraestrutura no momento do deploy. Não há ajuste dessas opções pela interface.

:::note
Estas configurações são definidas pela equipe de infraestrutura no momento do deploy. Se precisar alterá-las, fale com o administrador da plataforma.
:::

## Problemas comuns

| Sintoma | O que verificar pela interface |
|---------|-------------------------------|
| Um destino não está recebendo eventos | Em **Operação → Roteamento**, confirme se existe uma regra que envia para esse destino e se ela está ativa. Use a **simulação** para ver para onde um evento de exemplo iria. |
| Não consigo salvar uma regra com anonimização de PII | Esse recurso não está habilitado neste ambiente. Fale com o administrador da plataforma. |
| O teste de conexão de um destino falha | Confira endereço, porta e credenciais no formulário do destino. Use o botão de **testar conexão** para repetir a prova. |
| O login com Microsoft não aparece na tela de entrada | Em **Administração → Configurações**, confirme que a identidade está habilitada e que o teste de conexão com o Entra passou. |
| A plataforma está fora do ar ou retornando erros internos | Isso indica um problema de infraestrutura. Fale com o administrador da plataforma. |

## Próximos passos

- **Gerenciar usuários e papéis** → [Usuários](./users-and-roles.md)
- **Gerenciar organizações** → [Organizações](./organizations.md)
- **Exclusão de dados (LGPD)** → [Conformidade: LGPD/GDPR](../compliance/lgpd-gdpr.md)
- **Acompanhar a saúde e a observabilidade do fluxo** → [Observabilidade](../outputs/observability.md)
