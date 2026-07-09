---
sidebar_position: 13
title: Cutover gradual de SIEM (canary)
description: Migrar eventos de um destino para outro aos poucos, sem big-bang e sem perder eventos.
---

# Cutover gradual de SIEM (canary)

O cutover gradual (canary) permite migrar eventos de um destino para outro **aos poucos**, sem desligar o destino antigo de uma só vez. Você escolhe uma porcentagem (de 0% a 100%) dos eventos que devem ir para o destino novo; o restante continua indo para o destino antigo. Conforme você ganha confiança, vai aumentando a porcentagem até chegar a 100%.

O sistema garante que **o mesmo evento sempre segue o mesmo caminho**, inclusive quando precisa ser reenviado após uma falha temporária. Você não precisa se preocupar com eventos "pulando" de um destino para outro nem com duplicatas ao aumentar o percentual.

**Quem usa esta tela**: administradores configuram o cutover; analistas e engenheiros de segurança acompanham o andamento.

## Quando usar

- **Trocar de SIEM** — você está migrando do SIEM atual para um novo (por exemplo, de um Splunk antigo para um Splunk novo) e quer validar a qualidade e o volume dos dados no destino novo antes de desligar o antigo.
- **Testar um destino novo com tráfego real** — você adicionou um destino (um data lake, outro SIEM, um broker de mensagens) e quer enviar uma amostra pequena do tráfego de produção real, sem arriscar perder eventos se algo der errado.
- **Reduzir risco de uma mudança grande** — em vez de redirecionar 100% dos eventos de uma vez, você prefere ir de 10% para 25%, 50% e 100%, observando a saúde do destino a cada etapa e podendo recuar a qualquer momento.

## Como funciona, em uma frase

Uma rota com porcentagem de cutover **menor que 100%** entrega ao destino novo apenas uma fração dos eventos que casam com a condição dela. A fração é estável: aumentar a porcentagem nunca tira eventos que já estavam indo para o destino novo, e reenvios de um mesmo evento sempre tomam o mesmo caminho.

## Configurar um cutover gradual

O cutover é feito na tela de roteamento, em **Operação -> Roteamento** (disponível apenas para administradores). O ponto de partida é uma rota que já entrega 100% dos eventos ao destino antigo.

### Passo 1 — Criar a rota de cutover para o destino novo

1. Vá ao menu **Operação -> Roteamento**.
2. Adicione uma nova rota.
3. Preencha os campos do formulário:

| Campo | O que colocar |
|-------|---------------|
| Nome | Algo claro, ex.: "Splunk novo (canary)" |
| Prioridade | Um número **menor** que o da rota antiga, para que esta rota seja avaliada primeiro |
| Condição | Os mesmos critérios da rota antiga (ex.: eventos do fornecedor Sophos) |
| Destino | O destino **novo** |
| Porcentagem de cutover | Comece **pequeno**, ex.: 10% |
| Encerrar nesta rota | Sim, se você quer que o evento pare aqui (sem envio simultâneo a outros destinos) |

4. Salve a rota.

Pronto: a partir de agora, cerca de 10% dos eventos que casam com a condição vão para o destino novo. Os outros 90% "passam adiante" e continuam sendo atendidos pela rota antiga.

> **Mantenha as duas rotas ativas durante o cutover.** O objetivo do canary é justamente as duas rotas coexistirem: a nova recebe a fração que você definiu e a antiga recebe o restante. Você só desativa a rota antiga no final, quando a porcentagem chegar a 100% (Passo 4).

### Passo 2 — Acompanhar a saúde durante o cutover

Com o cutover rodando, acompanhe se o destino novo está recebendo a fração esperada e se não há falhas de entrega.

1. Vá ao menu **Operação -> Roteamento** e abra a rota de cutover para ver suas métricas.
2. Observe três números:

| Indicador | O que significa |
|-----------|-----------------|
| Eventos que casaram a condição | Total de eventos elegíveis (ex.: todos os eventos Sophos) |
| Eventos entregues à rota nova | Quantos efetivamente foram para o destino novo |
| Reenvios pendentes (fila de reenvio) | Deve ficar em zero ou muito baixo |

O cutover está saudável quando os **eventos entregues** correspondem, aproximadamente, à porcentagem que você configurou (por exemplo, com 10% configurado, cerca de 1 a cada 10 eventos elegíveis), e a **fila de reenvio** está próxima de zero.

Para uma visão do fluxo ponta a ponta — origem, rotas e destinos lado a lado, com a proporção mudando conforme você ajusta o percentual — use o menu **Operação -> Fluxo de dados** (disponível apenas para administradores). A proporção exibida pode levar cerca de um minuto para refletir mudanças recentes.

### Passo 3 — Aumentar a porcentagem aos poucos

Depois de acompanhar por 15 a 30 minutos e confirmar que o destino novo está saudável:

1. Volte a **Operação -> Roteamento** e abra a rota de cutover.
2. Aumente a porcentagem de cutover, por exemplo de 10% para 25%.
3. Salve.

Repita a cada 15 a 30 minutos, subindo de forma gradual: 25% -> 50% -> 75% -> 100%. A cada etapa, confira as métricas do Passo 2 antes de subir de novo. Se algo parecer errado, você pode reduzir a porcentagem para recuar.

### Passo 4 — Concluir e desativar a rota antiga

Quando a porcentagem chega a **100%**, todos os eventos elegíveis já estão indo para o destino novo. A partir daí:

1. Vá a **Operação -> Roteamento** e abra a rota antiga.
2. Desative a rota antiga (recomendado guardar no histórico) ou remova-a, se tiver certeza de que não precisará mais dela.
3. Salve a alteração.

> **Dica:** definir a porcentagem de cutover como 0% tem o mesmo efeito de desativar a rota — nenhum evento vai para ela — mas a rota continua existindo. É uma forma rápida de "pausar" uma rota sem apagá-la.

## Entenda os eventos que "passam adiante"

Quando um evento **não entra** na fração do cutover, ele simplesmente ignora a rota nova e continua sendo avaliado pelas rotas seguintes. É isso que faz com que os 90% restantes cheguem ao destino antigo.

| O evento entrou na fração do cutover? | Encerrar nesta rota? | O que acontece |
|---------------------------------------|----------------------|----------------|
| Sim | Sim | Vai para o destino novo e **para** aqui |
| Sim | Não | Vai para o destino novo e **continua** sendo avaliado (envio simultâneo a outros destinos) |
| Não | Sim ou Não | **Passa adiante** e é avaliado pelas rotas seguintes |

## Rede de segurança: nenhum evento se perde

Se um evento não casar com nenhuma rota — ou se a porcentagem de cutover estiver em 0% e não houver outra rota para atendê-lo — ele não é descartado. O CentralOps mantém um destino de segurança padrão que recebe esses eventos, garantindo que nada seja perdido durante uma migração.

## Diagnosticar rotas que nunca são avaliadas

Se você criar rotas com prioridades inconsistentes, uma rota pode ficar "escondida" atrás de outra e nunca ser avaliada. Por exemplo, se duas rotas têm a mesma condição e ambas encerram a avaliação, a de prioridade maior nunca será alcançada — os eventos sempre param na primeira.

A tela de roteamento sinaliza essas rotas com um aviso de rota inalcançável. Clique na rota para ver o motivo e ajustar as prioridades.

Importante: uma rota com porcentagem de cutover **menor que 100%** nunca esconde outra rota, porque a fração que ela não atende sempre passa adiante para as rotas seguintes.

## Resolução de problemas

### Os eventos não estão entrando na fração de cutover (0% em vez do esperado)

Provável causa: a condição da rota não está casando com os eventos.

1. Confira o campo usado na condição (por exemplo, fornecedor x plataforma).
2. Confira o valor exato esperado, atenção a maiúsculas e minúsculas (ex.: "sophos" x "Sophos").
3. Use a validação de rota em **Operação -> Roteamento** (testar a rota com um evento de exemplo) para ver se a condição casa. Veja [Validar rotas antes de publicar](./routing-dry-run.md).
4. Verifique nas métricas da rota se há eventos casando a condição (o indicador de "eventos que casaram" deve ser maior que zero).

### A rota de cutover ficou em 100% e nada mais chega à rota antiga

Esse é o comportamento esperado quando o cutover está completo e a rota encerra a avaliação: todos os eventos param no destino novo e nada "passa adiante". Se você queria que parte dos eventos continuasse indo também para o destino antigo (envio simultâneo), ajuste a rota de cutover para **não** encerrar nesta rota. Se a intenção era concluir a migração, basta desativar a rota antiga (Passo 4).

Se a rota antiga foi desativada ou removida, os eventos que não são atendidos por nenhuma outra rota seguem para o destino de segurança padrão. Confira em **Operação -> Roteamento** se a rota antiga ainda existe.

### O mesmo evento parece cair em frações diferentes a cada momento

Isso pode acontecer quando os eventos chegam sem um identificador próprio — sem ele, o sistema não consegue distribuí-los de forma estável. Verifique se a integração de origem está fornecendo um identificador para cada evento. Você pode inspecionar um evento na tela de busca em **Operação -> Investigações** para conferir os metadados internos do evento.

## Próximos passos

- **Iniciar um cutover?** Comece com 5% a 10% e acompanhe por 15 minutos antes de subir.
- **Ver métricas da rota?** Vá a **Operação -> Roteamento** e abra a rota.
- **Comparar dois destinos antes de publicar?** Use a validação de rota em **Operação -> Roteamento**. Veja [Validar rotas antes de publicar](./routing-dry-run.md).
- **Entender o roteamento por completo?** Veja [Roteamento por regra](./routing.md) e a [Visão geral de saídas e roteamento](./overview.md).
