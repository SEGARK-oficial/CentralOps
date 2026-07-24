---
sidebar_position: 14
title: Testar rotas, ver o fluxo e reverter mudanças
description: Simule uma mudança de rota antes de ativar, visualize o caminho Coleta → Roteamento → Destinos e volte a uma versão anterior em caso de problema
---

# Testar rotas, ver o fluxo e reverter mudanças

Antes de uma regra de roteamento entrar no ar, você pode simular o que ela faria com eventos reais, acompanhar pela tela de **Fluxo de dados** para onde tudo está indo, e — se algo der errado — voltar a rota a uma versão anterior. São três ferramentas que tornam mudanças de roteamento seguras: **simular (teste)**, **visualizar o fluxo** e **reverter**.

**Quem usa**: estas ferramentas estão disponíveis apenas para administradores da plataforma. As telas de **Roteamento**, **Fluxo de dados** e **Destinos** aparecem somente no perfil de administrador.

## Quando usar

- **Antes de publicar uma regra nova**: um analista criou uma regra para mandar só alertas críticos do Sophos para o SIEM. Simule primeiro para confirmar que os eventos certos batem na regra e que nada importante cai no destino padrão por engano.
- **Investigar um destino que parou de receber**: o SOC percebe que o Splunk não recebe eventos há uma hora. Abra o **Fluxo de dados** para ver se os eventos estão chegando à regra certa ou desviando para outro destino.
- **Recuperar de uma mudança ruim**: alguém ajustou a condição de uma regra e o volume no destino padrão disparou. Em vez de remontar a regra na mão, reverta para a última versão que funcionava.

## Simular: teste antes de publicar

A simulação avalia como uma regra (nova, editada ou a atual) rotearia eventos **sem entregar nada de verdade**. Você vê para qual destino cada evento iria e quais regras nunca seriam alcançadas.

:::note[A simulação avalia só a condição e o destino]
O que a simulação responde é "esta regra pega este evento, e para onde ele vai?". Ela **não** aplica amostragem, **não** aplica supressão e **não** aplica o descarte do evento bruto (`drop_raw`) — todas essas etapas ficam de fora do teste. Uma rota pode passar limpa na simulação e ainda assim descartar boa parte do tráfego em produção.

Para ver o **desfecho real** de um evento — inclusive **Suprimido**, **Amostrado para fora** ou **Em quarentena** —, a ferramenta é a [captura ao vivo](../operations/live-capture.md), que grava tráfego real por um tempo determinado e mostra o que aconteceu com cada evento.
:::

### Como simular

1. Abra o menu **Operação → Roteamento**.
2. Crie uma regra nova ou abra uma existente para editar.
3. Preencha a regra: nome, condição (qual evento ela pega), ação (encaminhar ou descartar), destino(s), prioridade e percentual de canário.
4. Antes de salvar, use a opção de **simular** a regra. O CentralOps reúne uma amostra de eventos recentes e mostra para onde cada um iria.

### Lendo o resultado da simulação

O resultado resume o que aconteceria com a amostra de eventos:

| O que aparece | Significado | O que observar |
|---|---|---|
| Total avaliado | Quantos eventos foram testados | Se for zero, não havia eventos recentes para simular; tente novamente após o pipeline receber tráfego |
| Encaminhados | Eventos que bateram em alguma regra de encaminhamento | Idealmente maior que zero |
| Descartados | Eventos que bateram em uma regra de descarte | Esperado se a regra existe para cortar ruído |
| Caíram no destino padrão | Eventos que **não** bateram em nenhuma regra e foram para o destino padrão | Atenção: número alto indica que alguma regra não está pegando o que deveria |
| Distribuição por destino | Quantos eventos foram para cada destino | Confira a proporção; muito volume no destino padrão sugere condição mal configurada |
| Regras inalcançáveis | Regras que nunca chegariam a executar | Crítico: uma regra listada aqui **nunca** vai rodar (veja "Regra que nunca executa" abaixo) |
| Exemplos por evento | Detalhe de eventos individuais e o destino de cada um | Use para inspecionar um caso concreto que foi parar no destino padrão |

A simulação pode usar **eventos reais recentes** (o CentralOps relê o tráfego que passou há pouco) — o teste reflete a realidade, mas o conjunto muda a cada execução. Repita a simulação algumas vezes para ter confiança no resultado.

## Visualizar o fluxo: do que entra ao que sai

A tela de **Fluxo de dados** mostra, em tempo real, o caminho dos eventos: quais regras estão batendo e quais destinos estão recebendo. É a forma de responder perguntas como "por que os eventos não chegam ao Splunk?" ou "algum destino está sobrecarregado?".

### Como abrir

Abra o menu **Operação → Fluxo de dados**.

Você vê um diagrama com:

- **Nós de regra** — cada regra de roteamento ativa, com a quantidade de eventos que ela está pegando por minuto.
- **Nós de destino** — cada destino, colorido pela saúde (saudável, ocioso ou desativado).
- **Setas** ligando regras a destinos, com a taxa de eventos por segundo.
- **Zoom, arrasto e detalhes ao passar o mouse** para inspecionar qualquer parte do fluxo.

### Interpretando o fluxo

Ao olhar uma **regra** no diagrama:

| O que aparece | Significado |
|---|---|
| Eventos por minuto que batem na regra | Volume alto = regra ativa; zero = a condição não está pegando nada |
| Eventos efetivamente encaminhados | Numa regra de encaminhamento é **sempre igual** ao número acima: os dois saem da mesma contagem de casamento. Nem o canário nem a amostragem separam um do outro — o evento fora do canário não chega a contar como casado nessa regra. A diferença aparece só quando a ação é descartar, e aí este número fica zerado |
| Eventos descartados por minuto | Normal se a regra existe para cortar ruído |

Ao olhar um **destino** no diagrama:

| O que aparece | Significado | O que fazer |
|---|---|---|
| Saúde do destino | Saudável, ocioso ou desativado | "Ocioso" = nenhum evento chegando; verifique a regra que deveria alimentá-lo |
| Eventos por segundo entregues | Vazão atual do destino | Compare com o esperado para a sua operação |
| Volume por minuto | Quanto dado está saindo para o destino | Use para detectar picos e planejar capacidade |

## Reverter: voltar uma regra a uma versão anterior

Toda alteração em uma regra de roteamento fica registrada no histórico daquela regra. Reverter restaura uma versão anterior sem precisar apagar e recriar a regra — útil para sair rápido de uma configuração ruim.

### Quando reverter

- Uma mudança de condição fez o volume no destino padrão disparar.
- Você trocou o destino de uma regra e ele passou a dar erro; volte ao destino antigo.
- Uma mudança de prioridade fez uma regra parar de executar; desfaça.
- Um teste de canário correu mal e você quer voltar a regra ao estado anterior.

### Como reverter

1. Abra o menu **Operação → Roteamento** e selecione a regra que deseja reverter.
2. Abra o histórico de versões da regra. Cada entrada é um ponto de restauração com **data, hora e quem fez a alteração**, da mais recente para a mais antiga.
3. Identifique a versão que estava funcionando (por exemplo, a anterior à mudança que causou o problema).
4. Confirme a reversão dessa versão. A regra passa a valer com aquela configuração, e a reversão entra no histórico como uma **nova** entrada — nada é apagado, então a trilha de mudanças fica completa para auditoria.

### Confirme o resultado

Logo após reverter, faça uma nova **simulação** da regra (mesmo passo a passo da seção "Simular") para confirmar que o comportamento voltou ao esperado — por exemplo, que o volume no destino padrão normalizou.

## Ciclo seguro de mudança de rota

Para qualquer alteração de roteamento, siga este ciclo:

1. **Simule** a regra e veja para onde os eventos iriam.
2. **Salve** a regra.
3. **Aguarde alguns minutos** para o pipeline acumular tráfego real.
4. **Abra o Fluxo de dados** e confira: as taxas estão saudáveis? O volume no destino padrão está normal?
5. **Se algo estiver errado**, abra o histórico da regra e **reverta** para a versão anterior.
6. **Simule de novo** para confirmar que a reversão resolveu.

## Problemas comuns

### Muitos eventos caindo no destino padrão

**O que você vê**: a simulação ou o **Fluxo de dados** mostra que a maioria dos eventos está indo para o destino padrão, em vez das regras específicas.

**Causas prováveis**:
- A condição da regra está restritiva demais e não casa com os eventos reais (por exemplo, foi escrita para um fornecedor, mas os eventos vêm de outro).
- Não existe nenhuma regra "pega-tudo" no fim da lista para servir de rede de segurança.
- Uma regra de descarte está cortando eventos sem querer.

**Como corrigir**:
- Revise a condição simulando com eventos reais e olhando os exemplos por evento, para ver por que eles não batem.
- Garanta uma regra final, sem condição, que sirva de rede de segurança para tudo que não foi tratado antes.

### Uma regra que nunca executa

**O que você vê**: a simulação lista uma regra como **inalcançável**.

**Causa**: uma regra anterior, marcada como **final**, já pega esses eventos e interrompe a avaliação antes de chegar à sua regra.

**Exemplo**: uma regra de prioridade alta sem condição (pega tudo) e marcada como final impede que qualquer regra de prioridade mais baixa rode.

**Como corrigir**:
- Reordene as regras, colocando a mais específica **antes** da que pega tudo (prioridade menor executa primeiro).
- Ou desmarque a opção "final" na regra que pega tudo, para que a avaliação continue nas regras seguintes.

### Um destino aparece com erro ou sem receber eventos

**O que você vê**: no **Fluxo de dados**, um destino aparece como desativado, ocioso ou com erro de conexão.

**Causa provável**: o destino foi desativado, a credencial expirou ou o endereço está incorreto.

**Como corrigir**:
1. Abra o menu **Operação → Destinos** e clique no destino.
2. Use o botão de **testar conexão** para validar credencial e endereço.
3. Se falhar, corrija os dados e salve.
4. Volte e faça uma **simulação** para confirmar que a regra ainda aponta para um destino válido.

:::note
Endereços técnicos, credenciais e parâmetros de baixo nível dos destinos são definidos pela equipe de infraestrutura no momento do deploy. Se precisar alterá-los, fale com o administrador da plataforma.
:::

## Próximos passos

- **Quer criar ou ajustar regras?** Veja [Roteamento por regra](./routing.md).
- **Testar uma mudança em uma fração do tráfego?** Veja [Roteamento canário](./routing-canary.md).
- **Precisa do desfecho real de um evento, e não da simulação?** Veja [Captura ao vivo](../operations/live-capture.md).
- **Um destino não está recebendo?** Veja [Saídas & Roteamento (visão geral)](./overview.md).
- **Precisa do histórico completo para auditoria?** Veja [Histórico e auditoria](../operations/history-audit.md).
