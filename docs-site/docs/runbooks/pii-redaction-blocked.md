---
sidebar_position: 8
title: "Eventos com redação de PII não chegam ao destino"
description: "Por que eventos de uma rota com redação de PII podem parar de chegar ao destino configurado e cair no destino padrão da organização, e como identificar e resolver pela interface."
---

# Eventos com redação de PII não chegam ao destino

Algumas rotas removem ou mascaram dados sensíveis (e-mail, IP, documentos) antes de enviar os eventos a um destino externo — é a **redação de PII**. Ela vem **ligada de fábrica**: numa instalação normal, uma rota com regras de redação aplica a redação e entrega ao destino configurado, como esperado.

Para proteger o dado, a plataforma trabalha em modo "à prova de falha": se a rota tem redação configurada mas a plataforma **não consegue garantir** que ela será aplicada, os eventos **não** saem para o destino externo. Em vez de arriscar vazar dado sensível em claro, ela desvia esses eventos para o **destino marcado como padrão** da organização — qualquer destino pode ter esse papel, não é mais um Wazuh interno fixo. Se a organização não tem nenhum destino padrão, os eventos vão para a **fila de reenvio** com o motivo "sem rota", de onde são visíveis e reprocessáveis. Esse comportamento é proposital e não é o que precisa ser consertado — o que você precisa descobrir é **por que** a garantia falhou.

São duas causas possíveis, e elas não têm o mesmo peso:

| Causa | O que significa |
|---|---|
| A **especificação de redação da rota** não pôde ser interpretada | Rara, porque as portas normais já barram: a validação é do próprio contrato da API — vale para a tela, para chamadas diretas e para o MCP — e a importação de pacote de configuração recusa o pacote inteiro antes de tocar o banco. Validar e aplicar são a **mesma** função, então uma spec que passa na gravação aplica em produção. Sobra escrita direta no banco, dado anterior à validação existir, ou registro corrompido. |
| Alguém **desligou explicitamente** a redação no deploy, com `PII_REDACTION_ENABLED=false` | Excepcional. A chave nasce **ligada**; para chegar nesse estado alguém precisou mudá-la de propósito. Não presuma que é o estado normal do seu ambiente — confirme. |

:::warning[Qualquer uma das duas causas derruba o roteamento da organização inteira]
As duas terminam no mesmo ponto do código: a rota não compila, e a plataforma descarta a lista de rotas daquela organização por inteiro. **Todas** as rotas param — inclusive as que não têm redação nenhuma — e todo o tráfego da organização vai para o destino padrão.

Por isso o sintoma é "vários destinos pararam de receber ao mesmo tempo", e não "uma rota parou". E por isso **o sintoma sozinho não diz qual das duas causas é**: o desvio em massa tem exatamente a mesma cara nos dois casos. Quem discrimina é o passo 1 — perguntar ao administrador se a variável foi desligada.
:::

Na prática, você nota eventos "sumindo" do destino externo e aparecendo no destino padrão. Não houve perda de dados — foi a proteção agindo. Esta página ajuda a identificar e resolver essa situação pela interface.

## Quando usar

- **Eventos pararam de chegar a um destino externo** (um SIEM de terceiros, um data lake, etc.) que você sabe que está configurado, mas continuam aparecendo no destino padrão da organização.
- **Você acabou de configurar redação de PII** em uma rota e os eventos não estão chegando ao destino esperado.
- **Você não consegue salvar uma rota** com regras de redação porque a tela mostra um erro de validação.
- **Picos no contador de desvio (fallback)** aparecem no Dashboard logo após uma mudança de configuração ou uma janela de manutenção.

## Sintoma 1: eventos não chegam ao destino externo, mas chegam ao destino padrão

Os eventos de uma rota com redação de PII deixam de aparecer no destino externo e passam a aparecer no destino marcado como padrão da organização (ou na fila de reenvio, se não houver um).

### O que verificar

**1. Confirme que a rota tem redação de PII configurada**

1. Abra o menu **Operação → Roteamento**.
2. Localize a rota esperada e abra o detalhe dela.
3. Procure a seção de redação de PII. Se houver regras listadas (por exemplo, mascarar o e-mail do usuário ou remover um campo de documento), a rota **tem** redação configurada.

**2. Confirme o desvio no Dashboard**

1. Abra o menu **Visão geral → Dashboard**.
2. Localize as informações de roteamento e o contador de eventos desviados para o destino interno padrão.
3. Se esse contador estiver **crescendo nos últimos minutos**, é sinal de que eventos de uma rota com redação estão sendo desviados — exatamente o comportamento "à prova de falha" descrito acima.

**3. Veja se o desvio atinge só essa rota ou todas**

Confira se os outros destinos externos continuam recebendo. Se **todos** pararam ao mesmo tempo, você confirmou que o roteamento da organização caiu por inteiro — mas **isso não diz qual das duas causas foi**: a variável desligada e a spec inválida produzem o mesmo desvio em massa. Só depois de eliminar a variável no passo 1 é que faz sentido caçar a rota com redação alterada mais recentemente, pelo histórico em **Operação → Roteamento**.

### Como resolver

#### Passo 1: confirme com o administrador se a redação foi desligada no deploy

Este é o **primeiro** item a eliminar, porque é rápido e porque é o estado excepcional: `PII_REDACTION_ENABLED` nasce ligada. Pergunte ao administrador da plataforma, com estas palavras, se **alguém setou `PII_REDACTION_ENABLED` para `false`** no ambiente.

- **Se a resposta for sim**, essa é a causa. E o alcance é maior do que parece: basta **uma** rota da organização ter redação configurada para que **todas** as rotas dela parem — as com redação e as sem — e todo o tráfego caia no destino padrão enquanto a chave estiver desligada. A correção é religá-la (ou remover a variável, já que o padrão é ligada), o que exige um novo deploy.
- **Se a resposta for não** — o caso mais comum —, a chave está ligada e o desvio vem da especificação de redação de alguma rota. Siga para o passo 2.

> A definição dessa variável de ambiente é feita pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma.

#### Passo 2: procure a rota com a especificação de redação problemática

1. Em **Operação → Roteamento**, liste as rotas que têm regras de redação de PII.
2. Olhe o **histórico** de cada uma e identifique qual foi alterada por último, principalmente se a alteração veio de fora da tela (importação de pacote de configuração, chamada de API, carga inicial). A tela recusa uma especificação inválida na hora de salvar; as outras portas de entrada é que produzem o problema.
3. Abra a rota suspeita e **salve a especificação de redação novamente pela tela**. A validação roda no salvamento e aponta a regra fora do formato — a tabela do Sintoma 2 lista os erros mais comuns.
4. Se preferir voltar ao estado anterior, **restaure uma versão do histórico**. A restauração revalida a rota antes de aplicá-la — destinos, especificação de redação e chave de supressão —, então uma versão antiga que já era inválida é recusada em vez de recolocar a rota quebrada no ar.

#### Se precisar escalar

Ao falar com o administrador, informe:

- O **nome da rota** afetada (visível em **Operação → Roteamento**).
- O **destino** que deveria receber os eventos.
- A **contagem de eventos desviados** observada no Dashboard e desde quando ela está subindo.
- Se o desvio atinge **uma rota ou todas** as rotas da organização.

Corrigida a causa, os **eventos novos** voltam a seguir a rota normalmente e a chegar ao destino externo.

> **Eventos que já foram desviados** ficaram guardados com segurança no destino padrão da organização (ou na fila de reenvio, se não houver um) — não foram perdidos. O reenvio desses eventos antigos ao destino correto é um procedimento de auditoria que precisa ser conduzido pela equipe da plataforma, e não uma ação imediata na interface.

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

## Sintoma 3: o evento chega ao destino com a marca de redação aplicada

Um evento aparece no destino trazendo, junto, uma lista das redações que foram aplicadas a ele (por exemplo, indicando que o e-mail foi mascarado e que um campo de documento foi removido).

### Como resolver

Isso é **esperado e correto** — nenhuma ação é necessária.

- O evento foi redigido conforme as regras da rota.
- A lista de redações aplicadas serve como registro de auditoria (mostra exatamente o que foi mascarado ou removido).
- O destino recebe a versão reduzida do evento, sem os dados sensíveis originais.

## Como evitar o problema

Antes de colocar uma rota com redação de PII em produção:

- **Crie e edite a redação sempre pela tela** de criação/edição da rota (veja o Sintoma 2). A tela valida antes de salvar; é o caminho que não deixa passar uma especificação inválida. Quando a rota vier de uma importação de pacote de configuração ou de uma chamada de API, abra-a na tela depois e salve uma vez para confirmar que ela é válida.
- **Teste com volume pequeno** usando o modo canário da rota (uma fração baixa do tráfego), em **Operação → Roteamento**, antes de direcionar todo o fluxo.
- **Acompanhe o Dashboard** em **Visão geral → Dashboard** para confirmar que os eventos estão chegando ao destino e que o contador de desvio não está subindo.

> A redação de PII vem ligada por padrão. Se, mesmo assim, você quiser confirmar que ninguém a desligou no seu ambiente (`PII_REDACTION_ENABLED=false`), peça ao administrador da plataforma — é uma variável definida no deploy, fora da interface.

## Quando escalar

Se o problema continuar depois das verificações acima, reúna as informações visíveis na interface e envie ao administrador da plataforma ou ao suporte:

- O **nome da rota** afetada (em **Operação → Roteamento**).
- O **destino** que deveria receber os eventos.
- A **contagem de eventos desviados** mostrada no Dashboard e há quanto tempo ela está crescendo.
- A **mensagem de erro exibida na tela**, caso o problema seja ao salvar a rota (Sintoma 2).
- A resposta do administrador sobre `PII_REDACTION_ENABLED` (ligada, que é o padrão, ou desligada no deploy).

Com essas informações, a equipe da plataforma consegue separar os dois cenários: uma chave desligada no ambiente, que se resolve religando-a no deploy, ou uma especificação de redação que a plataforma não consegue interpretar, que se resolve na própria rota.

## Páginas relacionadas

- [Rotas e roteamento](../outputs/routing.md)
- [Eventos desviados para o Wazuh interno](../runbooks/dispatcher.md)
