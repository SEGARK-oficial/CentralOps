---
sidebar_position: 10
title: "A plataforma não está respondendo? O que fazer"
description: "Guia para o usuário identificar sinais de que o CentralOps está fora do ar ou degradado e saber quando acionar o administrador da plataforma."
---

# A plataforma não está respondendo? O que fazer

Às vezes o CentralOps pode demorar a carregar, mostrar erros ao abrir uma tela, ou não exibir dados que você sabe que existem. Esta página ajuda você a reconhecer esses sinais, fazer as verificações simples que estão ao seu alcance pela interface e saber **quando** e **como** acionar o administrador da plataforma.

> Nada nesta página exige terminal, comandos ou conhecimento técnico. Tudo é feito pela interface web ou consiste em avisar a pessoa certa.

## Quando usar

- **Logo após uma janela de manutenção ou atualização**: você tenta entrar e a tela de login não carrega, ou entra mas as telas aparecem vazias. Antes de abrir um chamado urgente, vale confirmar se o problema é só com você ou com toda a plataforma.
- **No meio de um plantão do SOC**: você abre **Operação → Alertas** ou **Visão geral → Dashboard** e os dados não atualizam há um tempo. Você precisa decidir rápido se isso é uma falha da plataforma ou apenas ausência de eventos novos.
- **Ao receber relatos de colegas**: vários analistas dizem que "o CentralOps caiu". Você quer fazer uma checagem rápida e padronizada antes de escalar para a infraestrutura.

## Sinais de que algo está errado

| O que você observa | O que geralmente significa |
| --- | --- |
| A tela de login não abre ou fica girando sem terminar | A plataforma pode estar reiniciando ou indisponível |
| Você consegue logar, mas as telas aparecem em branco ou com erro ao carregar | A interface subiu, mas o serviço por trás dela ainda não está pronto |
| Uma tela específica falha (ex.: **Integrações** não lista nada), mas o resto funciona | Pode ser um problema pontual daquela área, não da plataforma toda |
| Os números do **Dashboard** ou de **Alertas** estão "congelados" há bastante tempo | Pode ser falta de eventos novos **ou** o processamento em segundo plano parado |

## Passo a passo (tudo pela interface)

Faça estas verificações na ordem. A maioria dos casos se resolve ou se esclarece nas duas primeiras.

### 1. Recarregue a página e tente de novo

Atualize a página no navegador e aguarde alguns instantes. Logo após uma manutenção, é comum a plataforma levar um curto período para ficar totalmente disponível. Espere um a dois minutos e tente novamente antes de concluir que está fora do ar.

### 2. Confirme se é só com você

- Tente acessar de outra aba ou de outro navegador.
- Pergunte a um colega se ele também está sem acesso.

Se **só você** está afetado, pode ser sessão expirada (faça logout e login de novo) ou uma questão de rede local sua. Se **todos** estão afetados, provavelmente é a plataforma — siga para o passo 3.

### 3. Veja se é a plataforma inteira ou só uma tela

Se você consegue logar, abra algumas telas de áreas diferentes do menu lateral, por exemplo:

- **Visão geral → Dashboard**
- **Operação → Alertas**
- **Visão geral → Integrações**

- Se **todas** falham em carregar, o problema é geral.
- Se **apenas uma** falha e as outras funcionam, o problema é localizado naquela área.

Anote quais telas funcionam e quais não — isso ajuda muito quem for investigar.

### 4. Verifique se os dados estão apenas parados (não ausentes)

Se as telas abrem, mas os números parecem "congelados":

- Em **Normalização → Saúde do Pipeline**, confira se o processamento dos eventos está acontecendo normalmente.
- Em **Operação → Alertas** e **Visão geral → Dashboard**, observe os horários dos eventos mais recentes.

Se os eventos mais recentes pararam num mesmo horário e não voltam a chegar, registre esse horário. Isso indica que o processamento em segundo plano (que recebe e trata os eventos) pode ter parado — e é uma informação importante para o administrador.

## Quando e como acionar o administrador

Acione o administrador da plataforma quando:

- A plataforma continua indisponível após você esperar alguns minutos e recarregar.
- **Todos** os usuários estão afetados, não só você.
- Os dados estão claramente parados (eventos novos deixaram de aparecer) e não voltam.
- Uma tela essencial para a sua operação segue falhando depois das verificações acima.

Para que o atendimento seja rápido, descreva o que você observou:

- **O que você tentou fazer** (ex.: "abrir a tela de Alertas").
- **O que aconteceu** (ex.: "fica girando e nunca carrega" ou "mostra erro ao carregar").
- **Quem está afetado** (só você ou todos os colegas).
- **Desde quando** você notou o problema, com horário aproximado.
- **Quais telas funcionam e quais não**, se você fez essa verificação.
- Se aplicável, **a partir de que horário os eventos pararam** de chegar.

> A recuperação da plataforma (subir os serviços, restabelecer o banco de dados e o processamento) é feita pela equipe de infraestrutura. Esse trabalho não é feito pela interface do CentralOps e não cabe ao usuário. O seu papel é reconhecer o sintoma, fazer as verificações simples acima e repassar essas informações ao administrador.

## O que **não** fazer

- Não fique recarregando a página dezenas de vezes em poucos segundos — isso não acelera a recuperação.
- Não conclua que "perdeu dados" só porque uma tela está vazia: muitas vezes é apenas a plataforma ainda subindo. Confirme com o administrador antes de assumir perda.
- Não tente "reiniciar" nada por conta própria; não há nada na interface para isso, e a recuperação é responsabilidade da infraestrutura.

## Próximos passos

- **Os eventos de uma integração específica pararam de chegar?** Veja [Coletores](../pipelines/collectors.md).
- **A plataforma está no ar, mas você quer entender a saúde do processamento?** Veja [Saúde do Pipeline](../operations/pipeline-health.md).
