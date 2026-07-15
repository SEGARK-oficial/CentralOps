---
sidebar_position: 2
title: Alertas
description: Lista de eventos de segurança normalizados, com filtros, busca e detalhe por evento
---

# Alertas

A tela **Alertas** mostra a lista de eventos de segurança já normalizados pela plataforma. É o ponto de partida diário do analista para ver o que está acontecendo: cada linha é um evento (um alerta de antivírus, uma detecção de ransomware, um login suspeito etc.), com severidade, origem e horário. Você filtra, busca por palavra-chave e abre o detalhe de qualquer evento.

Para acessar, use o menu **Operação → Alertas**.

**Quem pode ver:** todos os perfis com acesso de leitura ou superior.

:::note[Alertas vs. Detecções]
Esta tela mostra **eventos normalizados coletados** de suas integrações (antivírus, EDR, SIEM, etc.). Se você quer analisar **detecções de 1ª classe geradas por correlação ou buscas federadas**, vá em **Operação → Detecções**. Alertas = eventos brutos; Detecções = análise feita por regras e queries.
:::

## Quando usar

- **Triagem do início do plantão.** Filtre por severidade **Critical** nas últimas 24h para ver, em segundos, se há algo grave em andamento e se vale abrir um caso de resposta a incidente.
- **Investigar uma campanha ou IOC.** Recebeu um alerta de ransomware ou um indicador de comprometimento (hash, nome de família de malware)? Busque por palavra-chave e veja se vários eventos parecidos apareceram em poucos minutos — sinal de ataque ativo.
- **Avaliar ruído de uma fonte.** Quer saber se uma integração específica está gerando muitos falsos positivos? Filtre por aquela plataforma e palavra-chave e observe o padrão repetido.

## Permissões necessárias

A tela de Alertas é acessível a todos os perfis (Viewer e acima). Você consegue filtrar por severidade, plataforma, data e palavra-chave, e ver os eventos que já foram coletados e normalizados.

:::note[Filtro por palavra-chave em alertas]
Se você digitar um termo na busca de **Palavra-chave** e receber erro 403 (acesso negado), é porque sua conta é Viewer. Esse filtro dispara uma busca AO VIVO na fonte (custa $), que só Operator e superior conseguem fazer. 

Para investigação com palavras-chave, peça a um Operator que rode a busca pela tela de **Investigações**, ou solicite ao administrador para promovê-lo a Operator.
:::

## A tela

### Filtros disponíveis

| Filtro | Como funciona |
|--------|---------------|
| **Severidade** | Critical, High, Medium, Low (cada uma com uma cor). Selecione uma para ver só os eventos daquele nível. |
| **Data** | Calendário com atalhos ("últimas 24h", "últimos 7 dias") ou intervalo personalizado. O padrão é as últimas 24h. |
| **Plataforma** | Lista as fontes que coletam eventos (por exemplo, Sophos). Permite escolher mais de uma ao mesmo tempo. |
| **Palavra-chave** | Campo de texto livre. Busca no título e na descrição do evento. |

Os filtros se combinam: ao escolher mais de um, a lista mostra apenas os eventos que atendem a **todos** ao mesmo tempo.

### Colunas da lista

| Coluna | O que mostra |
|--------|--------------|
| **Horário** | Quando o evento foi registrado. |
| **Plataforma** | A fonte que originou o evento (por exemplo, Sophos). |
| **Severidade** | O nível de risco, com cor de destaque. |
| **Título** | Resumo curto do evento (ex.: "Trojan.X detectado"). |
| **Ação** | Abre o detalhe do evento e oferece atalhos como copiar o identificador. |

Por padrão, a lista vem ordenada do mais recente para o mais antigo e mostra um bloco de eventos por página; use os botões de paginação para avançar.

## Como cada evento é entregue

Todo evento normalizado passa por uma etapa de roteamento, que decide para quais destinos ele será enviado.

- **Sem regra específica:** o evento segue para o destino padrão da plataforma.
- **Com regras ativas:** o mesmo evento pode ser entregue a vários destinos ao mesmo tempo (por exemplo, um SIEM e um repositório de arquivamento), conforme as regras que combinarem.
- **Mascaramento por destino:** uma regra pode esconder dados sensíveis (como e-mails) antes de enviar a um destino, mantendo o dado íntegro em outro.

A configuração de roteamento e de mascaramento é feita por administradores, nas telas **Operação → Roteamento** e **Operação → Destinos** (visíveis apenas para o perfil de administrador). Para entender o caminho de saída de um evento, consulte:

- [Roteamento por regra](../outputs/routing.md) — como as regras decidem o destino.
- [Destinos](../outputs/destinations.md) — catálogo de integrações de saída.
- [Mascaramento de dados sensíveis](../outputs/pii-redaction.md) — como dados pessoais são protegidos na entrega.

## Passo a passo

### Encontrar os eventos críticos do dia

1. No menu, abra **Operação → Alertas**.
2. No filtro **Severidade**, escolha **Critical**.
3. No filtro **Data**, escolha **últimas 24h**.
4. Confirme a busca. A lista passa a mostrar apenas os eventos críticos do período.

### Abrir o detalhe de um evento

1. Clique no evento na lista (ou use a ação de abrir detalhe).
2. Um painel lateral mostra as informações do evento. Os campos principais são:
   - **Severidade** — o nível de risco atribuído.
   - **Título** — o resumo do evento.
   - **Fonte** — a integração que coletou o evento.
   - **Horário** — quando ocorreu.
   - **Dados brutos** — todos os campos originais vindos da fonte (pode ser uma lista longa).

### Identificar um possível ataque em andamento

1. No filtro **Plataforma**, selecione a fonte que quer investigar (por exemplo, Sophos).
2. No filtro **Palavra-chave**, digite o termo relevante (ex.: "ransomware" ou o nome de um indicador de comprometimento).
3. Observe os resultados, do mais recente para o mais antigo.
4. Se vários eventos parecidos aparecerem em poucos minutos, trate como um possível ataque em andamento e abra um caso na tela **Operação → Investigações**.

### Rastrear o host ou ativo afetado

1. Abra o detalhe do evento.
2. Localize o nome do host ou o endereço IP envolvido nos dados do evento.
3. Copie esse valor.
4. Vá em **Operação → Investigações** e busque por esse host ou IP para ver todos os eventos relacionados a ele e montar a linha do tempo.

## Casos de uso rápidos

| Pergunta | Como responder |
|----------|----------------|
| Houve algum evento crítico hoje? | Filtre Severidade = Critical e Data = últimas 24h. Se aparecer algo, inicie a resposta. |
| Qual fonte está gerando mais alertas? | Filtre por plataforma e compare a quantidade de eventos de cada uma. |
| Isso parece falso positivo? | Filtre pela plataforma e por uma palavra-chave. Se o mesmo padrão se repete, vale revisar a regra de roteamento (com um administrador). |
| Este ativo foi atacado recentemente? | Copie o host/IP do detalhe e busque na tela de Investigações para ver a linha do tempo. |
| Para qual destino este evento foi enviado? | Abra o detalhe e consulte as regras ativas na tela de [Roteamento](../outputs/routing.md). |

## O que esperar (e limites)

- **Apenas leitura.** Esta tela serve para visualizar e investigar; não é possível descartar nem reprocessar um evento por aqui. Para tratar eventos retidos, vá em **Normalização → Quarentena**.
- **Janela de tempo.** A lista mostra os eventos do período recente disponível. Eventos mais antigos são preservados nos destinos configurados para arquivamento — consulte com o administrador quais destinos guardam o histórico de longo prazo. Veja [Destinos](../outputs/destinations.md).
- **Busca por palavra-chave é simples.** Ela procura trechos de texto no título e na descrição. Para montar consultas mais flexíveis sobre os eventos já entregues, use a tela **Operação → Investigações** (veja [Investigações](./search.md)).

## Próximos passos

- **Precisa tratar um evento retido?** Vá em **Normalização → Quarentena** ([Quarentena](./quarantine.md)).
- **Quer uma investigação mais profunda ou montar uma consulta?** Vá em **Operação → Investigações** ([Investigações](./search.md)).
- **Precisa ajustar para onde os eventos são enviados?** Fale com um administrador ou veja [Roteamento](../outputs/routing.md).
- **Precisa proteger dados sensíveis na entrega?** Veja [Mascaramento de dados sensíveis](../outputs/pii-redaction.md).
