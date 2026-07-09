---
sidebar_position: 15
title: Redação de dados pessoais por rota (LGPD/GDPR)
description: Mascarar dados pessoais para um destino enquanto outro recebe o evento completo, atendendo LGPD/GDPR
---

# Redação de dados pessoais por rota (LGPD/GDPR)

Cada rota de envio pode **mascarar dados pessoais (PII)** antes de entregar o evento ao destino dela. Assim, o **mesmo evento** pode chegar **completo** a um destino de retenção (data lake) e **mascarado** ao seu SIEM, atendendo LGPD/GDPR sem perder a capacidade de investigação.

O ponto central: a redação é configurada **por rota**, não no evento inteiro. Uma rota mascara; outra preserva. A origem é a mesma, só o que cada destino enxerga muda.

:::info Quem usa esta tela
A configuração de rotas e de redação fica em telas de administrador. Operadores e analistas conseguem **auditar** o resultado (ver quais campos foram mascarados), mas a edição das regras é feita por um administrador da plataforma.
:::

---

## Quando usar

- **Tiering econômico com conformidade**: você quer guardar o evento bruto e completo no data lake (para investigação forense e retenção legal), mas o mesmo evento precisa chegar ao SIEM **sem e-mails, sem IP completo e sem senhas**, porque mais pessoas têm acesso ao SIEM.
- **MSSP / múltiplos clientes**: cada cliente tem seu próprio destino. Antes de entregar os eventos de um cliente ao SIEM dele, você remove campos sensíveis (senhas, documentos) e pseudonimiza nomes de usuário.
- **Direito ao esquecimento (GDPR)**: certos eventos não podem trafegar com o dado pessoal em claro para um destino específico — você aplica mascaramento naquela rota e mantém o restante do fluxo intacto.

---

## Como funciona

A mesma entrada gera saídas diferentes conforme a rota:

| Origem | Rota "Data Lake" (sem redação) | Rota "SIEM" (com redação) |
| --- | --- | --- |
| E-mail do usuário | mantido | mascarado |
| IP de origem | mantido | parcial (só os primeiros blocos) |
| Senha no corpo | mantida | removida |
| Nome do usuário | mantido | pseudonimizado |

O evento original nunca é alterado: a redação é aplicada a uma cópia, que segue para o destino daquela rota. Quando uma rota não tem redação, o destino recebe o evento exatamente como entrou.

Cada evento entregue carrega uma marca interna indicando **quais campos foram mascarados naquela entrega**. É isso que permite a auditoria de conformidade descrita mais abaixo.

---

## As quatro formas de mascarar um campo

Para cada campo sensível, você escolhe **uma** das ações abaixo.

| Ação | O que faz | Quando usar |
| --- | --- | --- |
| **Substituir** | Troca o valor inteiro por um marcador (ex.: `[REDACTED]` ou uma sequência de `*`). Pode usar tamanho fixo para não revelar o comprimento original. | E-mails, nomes, qualquer valor que o destino não precisa ver. |
| **Pseudonimizar** | Substitui o valor por um código irreversível, sempre o mesmo para o mesmo valor. O destino consegue **correlacionar** ("foi o mesmo usuário") sem ver quem é. | Nome ou identificador de usuário que você precisa rastrear entre eventos, mas sem expor a identidade. |
| **Mostrar parcial** | Mantém só parte do valor: os primeiros blocos de um IP, ou o início/fim de um texto. O meio é mascarado. | IPs (manter a rede, ocultar o host), documentos, nomes parciais. |
| **Remover o campo** | Apaga a chave por completo — o destino nem sabe que o campo existia. | Senhas, tokens, campos que **nunca** devem sair. |

### Detalhes que valem saber

- **Pseudonimizar é estável**: o mesmo valor sempre vira o mesmo código, o que permite correlação. Evite usá-la em campos de baixa variação (ex.: um PIN de 4 dígitos), porque um valor com poucas combinações pode ser deduzido por tentativa e erro. Para esses casos prefira **Substituir** ou **Remover o campo**.
- **Mostrar parcial é fail-safe**: se a configuração revelar demais ou o valor tiver um formato inesperado, a plataforma mascara o campo inteiro em vez de arriscar um vazamento.
- **Remover é diferente de zerar**: o campo é apagado de verdade — não vira um valor vazio que ainda denunciaria que ali havia algo.
- **Listas são tratadas com segurança**: se o campo apontado for uma lista de itens, a lista inteira é mascarada, em vez de a plataforma tentar adivinhar item a item.

### O que não pode ser mascarado

Apenas os campos de dados do evento (o conteúdo bruto e o conteúdo normalizado) podem ser redigidos. Os **metadados internos do evento** (informações que a plataforma usa para roteamento, rastreamento e auditoria) nunca são tocados — mascará-los quebraria a auditoria. A tela de configuração não permite selecioná-los.

---

## Como configurar (administrador)

A redação faz parte da configuração de uma rota de envio.

1. Abra o menu **Operação → Roteamento**.
2. Crie uma nova rota (ou edite uma existente) que aponte para o destino que deve receber os dados mascarados.
3. Para cada campo sensível, adicione uma regra de redação escolhendo o campo e uma das quatro ações acima.
4. Salve. A plataforma valida a configuração: regras inválidas (campo inexistente ou ação desconhecida) são recusadas no momento de salvar, com uma mensagem indicando o problema.

:::note Edição avançada de regras
Dependendo da versão do seu ambiente, as regras de redação podem ser editadas em um formulário guiado por campo ou em um editor de regras mais técnico dentro da tela de **Roteamento**. Se você não encontrar a opção de redação ao editar a rota, fale com o administrador da plataforma — o recurso pode não estar habilitado neste ambiente.
:::

### Rota de controle (sem redação)

Para que um destino receba o evento **completo**, basta criar a rota apontando para esse destino e **não adicionar nenhuma regra de redação**. Esse é o caminho típico do data lake.

---

## Verificar que a redação está sendo aplicada

Depois que os eventos começarem a fluir:

1. Abra **Normalização → Saúde do Pipeline**.
2. Selecione o destino que deveria estar recebendo os dados mascarados.
3. Consulte a auditoria de entregas daquele destino. Eventos que passaram pela redação trazem a marca de quais campos foram mascarados.

Se os eventos chegam mascarados ao SIEM e completos ao data lake, a configuração está correta.

---

## Se a redação não estiver sendo aplicada

Se um destino que deveria receber dados mascarados está recebendo o evento completo, verifique nesta ordem:

1. **A rota tem regras de redação?** Em **Operação → Roteamento**, confirme que a rota daquele destino tem ao menos uma regra.
2. **O evento está caindo nesta rota?** Confira a condição da rota — o evento precisa satisfazer o filtro para ser processado por ela.
3. **O destino está realmente recebendo os eventos?** Em **Normalização → Saúde do Pipeline**, confirme que o destino está ativo e recebendo entregas.
4. **O recurso está habilitado no ambiente?** A funcionalidade de redação de PII pode ser ligada ou desligada na configuração da plataforma. Essa configuração é definida pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma. **Importante**: por segurança, se a redação estiver desligada no ambiente mas uma rota exigir mascaramento, os eventos **não são entregues em claro** — eles são desviados para a entrega padrão interna, sem vazar dados.

---

## Limites importantes

- **A entrega padrão (catch-all) nunca mascara.** Eventos que não casam com nenhuma rota seguem para o destino padrão **completos**, por design — esse caminho é a rede de segurança que garante que nenhum evento seja perdido. Se um conjunto de eventos precisa ser mascarado, garanta que ele tenha uma rota própria com regras de redação.
- **Um campo, uma regra.** Cada regra aponta para um campo específico. Não há curingas nem expressões; para mascarar vários campos, adicione várias regras.
- **Reprocessamento mantém a redação.** Se um evento for reentregue (por exemplo, após uma falha temporária do destino), a mesma rota aplica exatamente a mesma redação — sem mascarar duas vezes.

---

## Checklist de conformidade

- [ ] **Identificar PII**: liste quais campos são pessoais (e-mail, IP, documento, nome, senha).
- [ ] **Mapear destinos**: defina qual destino precisa de dados mascarados (SIEM) e qual recebe tudo (data lake).
- [ ] **Criar rotas**: uma rota por nível de exposição (interno completo vs. externo mascarado).
- [ ] **Escolher a ação por campo**: substituir, pseudonimizar, mostrar parcial ou remover.
- [ ] **Validar a entrega**: confirme em **Saúde do Pipeline** que os eventos chegam mascarados ao destino certo.
- [ ] **Auditar**: use a marca de campos redigidos para comprovar a execução em auditorias.

---

## Próximos passos

- **Entender roteamento?** Veja [Saídas & Roteamento](./overview.md).
- **Configurar destinos?** Veja [Visão geral de destinos](./overview.md#tipos-de-destino-disponíveis).
- **Auditar conformidade?** Veja [LGPD/GDPR](../compliance/lgpd-gdpr.md).
