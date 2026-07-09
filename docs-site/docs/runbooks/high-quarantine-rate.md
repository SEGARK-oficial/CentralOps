---
sidebar_position: 3
title: Resolver taxa alta de quarentena
description: O que fazer quando muitos eventos param em quarentena por erro de validação, parse ou mapeamento.
---

# Resolver taxa alta de quarentena

A **quarentena** guarda os eventos que chegaram de uma integração mas não puderam ser normalizados: JSON inválido, campo obrigatório faltando, tipo de campo errado ou uma regra do mapeamento que quebrou. Esta página mostra como identificar a causa e limpar a fila pela interface.

> A quarentena é sobre eventos que **entraram** mas falharam na normalização. Falha ao **entregar** um evento já normalizado para um destino (Wazuh, Splunk, S3, etc.) não é quarentena — é a fila de reenvio. Veja [Operação de destinos](../outputs/destination-operations.md).

## Quando usar

- **Pico súbito de quarentena.** O card de quarentena saltou de quase zero para dezenas ou centenas por hora e você precisa descobrir o porquê.
- **Você editou um mapeamento e os eventos começaram a falhar.** Uma alteração recente pode ter quebrado uma regra; é preciso confirmar e reverter.
- **Um fornecedor mudou a API.** As respostas vieram com campos novos, renomeados ou com outro tipo, e o mapeamento atual não os reconhece mais.

## Onde ver o problema

| Tela | O que ela mostra |
|------|------------------|
| Menu **Visão geral -> Dashboard**, card de quarentena | Total de eventos em quarentena crescendo em tempo real. |
| Menu **Normalização -> Saúde do Pipeline**, card de quarentena | Visão consolidada da saúde da normalização; o card de quarentena fica vermelho quando o volume sobe. |
| Menu **Normalização -> Quarentena** | A fila em si: lista de eventos, fornecedor, mensagem de erro e ações de reprocesso/descarte. |

Compare o card de quarentena com o card da **fila de reenvio** na mesma tela de **Saúde do Pipeline**:

- **Só a quarentena sobe:** o problema é de entrada (parse, mapeamento ou validação). A causa está no fornecedor ou no mapeamento.
- **Só a fila de reenvio sobe:** o problema é de entrega a um destino (conectividade, cota, formato). Veja [Operação de destinos](../outputs/destination-operations.md).
- **As duas sobem:** há mais de um problema. Investigue por fornecedor e por destino separadamente.

## Diagnóstico em 3 passos

### 1. O que mudou recentemente?

Antes de mexer em qualquer coisa, pergunte-se:

- O fornecedor mudou a API (resposta diferente)?
- Algum mapeamento foi editado e pode ter quebrado uma regra?
- Uma integração nova foi criada?
- Houve aumento de tráfego (cliente novo, ataque em andamento, teste de carga)?

### 2. Qual fornecedor está gerando os erros?

1. Vá ao menu **Normalização -> Quarentena**.
2. Sem filtro, observe a coluna de plataforma/fornecedor.
3. Identifique quais fornecedores concentram os eventos.

Por exemplo, se 80% dos eventos em quarentena são de um único fornecedor, é ele que está enviando dados que o mapeamento não consegue tratar.

### 3. Qual é o tipo de erro?

Ainda em **Normalização -> Quarentena**, abra um ou dois eventos do fornecedor problemático e leia a mensagem de erro. Os casos mais comuns:

| Mensagem (exemplos) | O que significa |
|---------------------|-----------------|
| Campo obrigatório ausente (ex.: falta o horário do evento) | O fornecedor parou de enviar um campo ou o renomeou. |
| Erro de tipo (esperava texto, recebeu número) | O fornecedor mudou o tipo de um campo. |
| Erro de parse de JSON | O fornecedor devolveu um JSON malformado. |
| Evento truncado no armazenamento | O evento era grande demais e foi reduzido para caber no limite; o original não pode ser reprocessado. |

## Ações por tipo de erro

### Campo obrigatório faltando

**Causa:** o fornecedor removeu ou renomeou um campo.

1. Vá ao menu **Normalização -> Mappings** e abra o mapeamento do fornecedor.
2. No editor de mapeamento, localize a regra do campo que ficou faltando.
3. Aponte a regra para o novo nome do campo enviado pelo fornecedor.
4. Salve. Isso cria uma nova versão do mapeamento.
5. Volte ao menu **Normalização -> Quarentena** e reprocesse os eventos do fornecedor.
6. Aguarde cerca de 1 minuto. Os erros devem desaparecer e a quarentena voltar a um valor baixo.

### Mudança de tipo de campo

**Causa:** o fornecedor passou a enviar um campo com outro tipo (por exemplo, texto no lugar de número).

1. Vá ao menu **Normalização -> Mappings** e abra o mapeamento do fornecedor.
2. No editor de mapeamento, localize a regra do campo afetado.
3. Ajuste a regra para converter o valor ao tipo esperado pelo modelo normalizado.
4. Salve para gerar a nova versão.
5. Volte ao menu **Normalização -> Quarentena** e reprocesse os eventos.

> Se você não tem certeza de como ajustar a conversão de tipo, use o recurso de pré-visualização do editor de mapeamento (teste a regra com uma amostra) antes de salvar. Assim você confirma o resultado sem afetar a produção.

### JSON inválido

**Causa:** o fornecedor devolveu um JSON malformado. É raro e quase sempre indica um problema do lado do fornecedor.

1. Vá ao menu **Normalização -> Quarentena** e expanda um evento com esse erro.
2. Inspecione o conteúdo bruto do evento para confirmar que o JSON realmente está quebrado (aspas ou escape incorretos).
3. Se os eventos estão corrompidos e não há como repará-los, descarte-os pela tela de Quarentena.
4. Abra um chamado com o suporte do fornecedor informando que a resposta da API está vindo com JSON inválido.

### Evento truncado no armazenamento

**Causa:** o evento era grande demais e ultrapassou o limite de tamanho da quarentena. O conteúdo foi reduzido para caber e o original foi perdido, então **não há como reprocessar** esse evento.

1. Na tela de **Quarentena**, ao expandir o evento, ele aparece marcado como truncado.
2. Aceite que esse evento específico não pode ser recuperado.
3. Investigue por que o evento é tão grande:
   - O fornecedor está enviando payloads gigantes (possível problema na coleta)?
   - Alguma regra de mascaramento está deixando a estrutura inchada?

> O limite de tamanho dos eventos em quarentena é definido pela equipe de infraestrutura no momento do deploy. Se precisar aumentá-lo, fale com o administrador da plataforma. Se os destinos também recusam eventos por tamanho, peça que o limite do destino seja revisto junto.

## Investigando a causa raiz

### O fornecedor mudou a API

Quando todos (ou quase todos) os eventos de um fornecedor falham de uma vez, suspeite de mudança de schema na API.

1. Em **Normalização -> Quarentena**, abra alguns eventos recentes do fornecedor e compare o conteúdo bruto com o que o mapeamento espera: faltam campos? Surgiram campos novos? Os tipos mudaram?
2. Se confirmado, atualize o mapeamento em **Normalização -> Mappings** conforme o novo formato e reprocesse a fila.
3. Registre um chamado com o fornecedor informando a data em que o formato mudou.

### Um mapeamento foi quebrado por engano

Se a quarentena disparou logo após uma edição de mapeamento, a edição é a suspeita principal.

1. Vá ao menu **Normalização -> Mappings** e abra o histórico de versões do mapeamento.
2. Compare a versão atual com a anterior para ver o que mudou.
3. Se a versão anterior estava correta, reverta para ela pelo próprio histórico.
4. A fila de quarentena costuma limpar em menos de 1 minuto após o reprocesso.

### Cliente novo ou grande volume

Quando só uma parte dos eventos falha (e não 100%), o problema tende a ser qualidade de dados, não schema.

1. Vá ao menu **Operação -> Alertas**.
2. Filtre pelas últimas 24 horas e pelo fornecedor em questão.
3. Compare o volume de antes com o de agora e estime o percentual em quarentena.

Se o percentual de erro subiu (por exemplo, de 1% para 3%), as causas prováveis são:

- Cliente novo enviando dados "sujos" (muitos campos inválidos).
- Atividade anormal, como um ataque em andamento.
- Teste de carga do lado do fornecedor.

Identifique qual cliente ou qual tipo de evento está falhando e trate a origem.

### Confusão com mascaramento de dados (redação de PII)

Se uma regra de roteamento aplica **mascaramento de dados** (remoção de informações sensíveis) antes de enviar a um destino, alguns campos podem sumir e dar a impressão de que o evento "se perdeu". Isso **não é quarentena** — os eventos foram normalizados com sucesso.

Para confirmar, vá ao menu **Operação -> Alertas** (não à Quarentena), filtre pelo fornecedor nas últimas horas e verifique se os eventos aparecem normalmente. Se eles estão lá, a redução de conteúdo é o mascaramento agindo, e não um erro.

> As regras de roteamento e de mascaramento ficam no menu **Operação -> Roteamento**, disponível apenas para administradores. Se você não é administrador e suspeita que o mascaramento está agressivo demais, fale com o administrador da plataforma.

## Limpando muitos eventos de uma vez

Quando há centenas ou milhares de eventos na fila, escolha a abordagem conforme a causa.

| Abordagem | Quando usar | Como fazer |
|-----------|-------------|------------|
| Corrigir e reprocessar | O erro é mapeável (nome ou tipo de campo). É a opção preferida. | Corrija o mapeamento em **Normalização -> Mappings**, volte a **Normalização -> Quarentena** e reprocesse a fila. O reprocesso roda em segundo plano; aguarde alguns minutos. |
| Descartar tudo | Os eventos são ruído ou de teste e não valem reprocesso. | Em **Normalização -> Quarentena**, filtre pelo fornecedor e descarte os eventos selecionados. Eles não são recuperáveis pela quarentena. |
| Recoleta histórica | O erro afeta muitos eventos antigos, não só os recentes. | Após corrigir o mapeamento, solicite uma recoleta histórica da integração (por exemplo, dos últimos dias). Os eventos antigos voltam a ser processados com o mapeamento corrigido. |

## Prevenção

- Acompanhe o card de quarentena em **Normalização -> Saúde do Pipeline** e em **Visão geral -> Dashboard** para detectar picos cedo.
- Use o histórico de versões dos mapeamentos: ele permite reverter uma alteração com poucos cliques.
- Após uma atualização grande de um fornecedor, teste o mapeamento (pré-visualização com amostra) antes de aplicar em produção.
- Registre as mudanças de formato dos fornecedores para reagir mais rápido na próxima vez.
- Se os eventos começarem a chegar truncados, isso indica que o limite de tamanho pode estar baixo demais. Fale com o administrador da plataforma para revisá-lo.

## Próximos passos

- **Editar ou reverter um mapeamento?** Vá a [Mappings](../normalization/overview.md).
- **Triar eventos em quarentena?** Vá a [Quarentena](../operations/quarantine.md).
- **Falha ao entregar a um destino?** Vá a [Operação de destinos](../outputs/destination-operations.md).
- **Roteamento e mascaramento?** Vá a [Roteamento](../outputs/routing.md).
