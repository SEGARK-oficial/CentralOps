---
sidebar_position: 8
title: "Eventos com redação de PII não chegam ao destino"
description: "Por que eventos de uma rota com redação de PII podem aparecer no Wazuh em vez do destino configurado, e como identificar e resolver pela interface."
---

# Eventos com redação de PII não chegam ao destino

Algumas rotas removem ou mascaram dados sensíveis (e-mail, IP, documentos) antes de enviar os eventos a um destino externo — é a **redação de PII**. Para proteger esses dados, o CentralOps trabalha em modo "à prova de falha": se a redação está configurada na rota mas, por qualquer motivo, a plataforma não consegue garantir que ela será aplicada, os eventos **não** são enviados ao destino externo. Em vez de arriscar vazar dado sensível sem redação, a plataforma desvia esses eventos para o destino interno padrão (Wazuh), onde ficam guardados de forma segura, sem exposição.

Na prática, isso significa que você pode notar eventos "sumindo" de um destino externo e aparecendo no Wazuh. Não houve perda de dados — foi uma proteção agindo. Esta página ajuda a identificar e resolver essa situação pela interface.

## Quando usar

- **Eventos pararam de chegar a um destino externo** (um SIEM de terceiros, um data lake, etc.) que você sabe que está configurado, mas continuam aparecendo no Wazuh interno.
- **Você acabou de configurar redação de PII** em uma rota e os eventos não estão chegando ao destino esperado.
- **Você não consegue salvar uma rota** com regras de redação porque a tela mostra um erro de validação.
- **Picos no contador de desvio (fallback)** aparecem no Dashboard logo após uma mudança de configuração ou uma janela de manutenção.

## Sintoma 1: eventos não chegam ao destino externo, mas chegam ao Wazuh

Os eventos de uma rota com redação de PII deixam de aparecer no destino externo e passam a aparecer no Wazuh interno.

### O que verificar

**1. Confirme que a rota tem redação de PII configurada**

1. Abra o menu **Operação → Roteamento**.
2. Localize a rota esperada e abra o detalhe dela.
3. Procure a seção de redação de PII. Se houver regras listadas (por exemplo, mascarar o e-mail do usuário ou remover um campo de documento), a rota **tem** redação configurada.

**2. Confirme o desvio no Dashboard**

1. Abra o menu **Visão geral → Dashboard**.
2. Localize as informações de roteamento e o contador de eventos desviados para o destino interno padrão.
3. Se esse contador estiver **crescendo nos últimos minutos**, é sinal de que eventos de uma rota com redação estão sendo desviados — exatamente o comportamento "à prova de falha" descrito acima.

### Como resolver

A redação de PII pode estar desativada na plataforma. Essa é uma configuração de nível de plataforma, e não algo que se liga ou desliga por rota na interface.

> Esta configuração é definida pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma.

Ao falar com o administrador, informe:

- O **nome da rota** afetada (visível em **Operação → Roteamento**).
- O **destino** que deveria receber os eventos.
- A **contagem de eventos desviados** observada no Dashboard e desde quando ela está subindo.

Depois que a redação de PII for reativada pela equipe de infraestrutura, os **eventos novos** voltam a seguir a rota normalmente e a chegar ao destino externo.

> **Eventos que já foram desviados** ficaram guardados com segurança no Wazuh interno — não foram perdidos. O reenvio desses eventos antigos ao destino correto é um procedimento de auditoria que precisa ser conduzido pela equipe da plataforma, e não uma ação imediata na interface.

## Sintoma 2: a rota não salva por causa de uma regra de redação inválida

Ao criar ou editar uma rota com redação de PII, a tela exibe um erro de validação e não permite salvar.

### O que verificar

A plataforma valida as regras de redação **antes** de salvar, então o erro indica que alguma regra está fora do formato aceito. Os motivos mais comuns são:

| Problema | Como corrigir |
|----------|---------------|
| A regra aponta para um campo de uso interno do evento | Aponte a regra apenas para campos de dados do evento (o conteúdo bruto ou normalizado), nunca para os metadados internos do evento. |
| A regra aponta para o evento inteiro, e não para um campo específico | Indique o campo exato a ser redigido (por exemplo, o e-mail do usuário), não o evento como um todo. |
| A ação escolhida não é suportada | Use uma das ações disponíveis: mascarar, gerar hash, mostrar parcialmente ou remover o campo. |
| Parâmetros numéricos inválidos | Ao mascarar com tamanho fixo, use um número inteiro positivo. Ao mostrar parcialmente, defina quantas partes manter (início e/ou fim). |

### Como resolver

1. Ajuste a regra apontada na mensagem de erro conforme a tabela acima.
2. Salve novamente. Se ainda houver erro, a mensagem indicará a próxima regra a corrigir.

## Sintoma 3: o evento chega ao Wazuh com a marca de redação aplicada

Um evento aparece no Wazuh interno trazendo, junto, uma lista das redações que foram aplicadas a ele (por exemplo, indicando que o e-mail foi mascarado e que um campo de documento foi removido).

### Como resolver

Isso é **esperado e correto** — nenhuma ação é necessária.

- O evento foi redigido conforme as regras da rota.
- A lista de redações aplicadas serve como registro de auditoria (mostra exatamente o que foi mascarado ou removido).
- O destino recebe a versão reduzida do evento, sem os dados sensíveis originais.

## Como evitar o problema

Antes de colocar uma rota com redação de PII em produção:

- **Valide as regras de redação** na tela de criação/edição da rota (veja o Sintoma 2). A plataforma só salva se o formato estiver correto.
- **Teste com volume pequeno** usando o modo canário da rota (uma fração baixa do tráfego), em **Operação → Roteamento**, antes de direcionar todo o fluxo.
- **Acompanhe o Dashboard** em **Visão geral → Dashboard** para confirmar que os eventos estão chegando ao destino e que o contador de desvio não está subindo.

> A ativação da redação de PII na plataforma é definida pela equipe de infraestrutura no momento do deploy. Se precisar confirmar se ela está ativa, fale com o administrador da plataforma.

## Quando escalar

Se o problema continuar depois das verificações acima, reúna as informações visíveis na interface e envie ao administrador da plataforma ou ao suporte:

- O **nome da rota** afetada (em **Operação → Roteamento**).
- O **destino** que deveria receber os eventos.
- A **contagem de eventos desviados** mostrada no Dashboard e há quanto tempo ela está crescendo.
- A **mensagem de erro exibida na tela**, caso o problema seja ao salvar a rota (Sintoma 2).

Com essas informações, a equipe da plataforma consegue confirmar se a redação de PII está ativa e reativá-la, se for o caso.

## Páginas relacionadas

- [Rotas e roteamento](../outputs/routing.md)
- [Eventos desviados para o Wazuh interno](../runbooks/dispatcher.md)
