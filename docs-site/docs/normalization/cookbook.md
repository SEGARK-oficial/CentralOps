---
sidebar_position: 4
title: Cookbook de mapeamento
description: Receitas prontas para os cenários de mapeamento mais comuns — o que cada caso resolve, quando usá-lo e como montar a regra no editor de mapeamento.
---

# Cookbook de mapeamento

Um mapeamento transforma o evento cru de um fornecedor (Sophos, firewall, EDR, e-mail) no formato OCSF normalizado que o CentralOps usa internamente. Esta página reúne receitas prontas para os cenários que mais aparecem no dia a dia: ajustar severidade, montar listas de indicadores, extrair campos escondidos dentro de um texto JSON, e assim por diante.

Cada receita mostra **o evento que chega**, **a regra que você cria** e **o resultado normalizado**, sempre indicando quando usar aquele caso.

## Quando usar

- **Onboarding de uma nova origem.** Você acabou de conectar um fornecedor e percebe, na tela de Saúde do Pipeline, que vários campos não estão sendo preenchidos. Use as receitas abaixo para escrever as regras que populam severidade, título, indicadores e horário do evento.
- **Eventos caindo em Quarentena.** Eventos de uma origem específica aparecem em **Normalização -> Quarentena** com falha de mapeamento. As receitas de horário obrigatório (Receita 10) e de táticas MITRE (Receita 8) cobrem as causas mais comuns.
- **Campos novos detectados.** O Drift Explorer apontou campos novos que o fornecedor passou a enviar (por exemplo, um novo bloco de dados de e-mail). As receitas de extração de JSON aninhado (Receita 4) e de construção de indicadores (Receita 5) ajudam a incorporá-los ao mapeamento.

## Onde isso acontece na interface

Todas as receitas são montadas no mesmo lugar. Antes de seguir qualquer uma, abra o editor de mapeamento:

1. No menu lateral, vá em **Normalização -> Mappings**.
2. Escolha o mapeamento da origem que você quer ajustar (ou abra o mapeamento que está gerando eventos em Quarentena).
3. No editor de regras, cada linha tem um campo **target** (onde o valor vai parar, no padrão OCSF) e um campo **source** (de onde o valor vem, no evento do fornecedor). Os demais ajustes de cada receita são opções dessa mesma regra.
4. Use o painel de amostra ao lado para colar um evento de exemplo (modo **Manual**) ou puxar um evento real já coletado (modo **Reservoir**). É com base nessa amostra que o **dry-run** mostra, em tempo real, o resultado normalizado e os avisos.
5. Quando o resultado estiver correto, salve com **Salvar nova versão** e descreva a mudança no campo de mensagem. A nova versão passa a valer para os próximos eventos.

:::tip
O dry-run não altera nada em produção: ele só pré-visualiza como o evento de amostra seria normalizado. Use-o à vontade para testar uma receita antes de salvar.
:::

---

## Receita 1: Severidade numérica do fornecedor

**Use esta receita quando** uma detecção chega com a severidade já em número (por exemplo, o Sophos XDR envia uma escala de 0 a 10) e você quer levar esse número para o campo de severidade normalizado.

### Evento que chega
```jsonc
{
  "id": "det_001",
  "time": "2026-04-27T12:30:00Z",
  "severity": 7,
  "detectionRule": "Malware.Generic"
}
```

### Regra que você cria
- **target:** `normalized.severity_id`
- **source:** `severity`
- Marque a conversão para inteiro e marque a regra como **obrigatória**.

### Resultado normalizado
```jsonc
{ "normalized": { "severity_id": 7 } }
```

### Por que funciona
A regra pega o número direto do evento. A escala do fornecedor (0–10) é preservada como está, porque você não pediu nenhuma reescala. Se quiser converter para a escala OCSF de 1 a 6, adicione um **mapa de valores** com as correspondências (por exemplo, `7 → 4`).

### Variações
- O fornecedor às vezes omite a severidade? Defina um **valor padrão** (por exemplo, `0`).
- A severidade chega como texto (`"7"` em vez de `7`)? Adicione uma conversão para inteiro antes da regra.

---

## Receita 2: Severidade em texto para número

**Use esta receita quando** o fornecedor manda a severidade por extenso (`"critical"`, `"high"`) e o OCSF espera o número equivalente. É o caso típico de alertas de firewall e EDR do Sophos.

### Evento que chega
```jsonc
{
  "id": "alert_001",
  "severity": "high",
  "description": "Intrusion attempt blocked"
}
```

### Regra que você cria
- **target:** `normalized.severity_id`
- **source:** `severity`
- Adicione um **mapa de valores** com as correspondências de texto para número:

| Texto do fornecedor | Valor normalizado |
|---|---|
| `critical` | 5 |
| `high` | 4 |
| `medium` | 3 |
| `low` | 2 |
| `info` / `informational` | 1 |

- Defina um **valor padrão** (por exemplo, `0`) e marque a regra como **obrigatória**.

### Resultado normalizado
```jsonc
{ "normalized": { "severity_id": 4 } }
```

### Por que funciona
O mapa procura o texto que chegou (`"high"`) e devolve o número correspondente (`4`). Se a severidade vier em branco, o valor padrão é usado.

### Variações
- Se o fornecedor mistura maiúsculas e minúsculas, adicione uma conversão para minúsculas antes do mapa de valores.
- Apareceu uma severidade que não está no mapa? O valor padrão entra no lugar — sem erro, a menos que você o tenha deixado vazio em uma regra obrigatória.
- Precisa de mais níveis? É só acrescentar linhas ao mapa de valores.

---

## Receita 3: Título com várias fontes possíveis

**Use esta receita quando** o mesmo tipo de informação aparece em campos diferentes dependendo da variante do evento. Por exemplo, detecções de e-mail do Sophos trazem a descrição em um campo, enquanto detecções de endpoint trazem em outro — e você quer um título único de qualquer forma.

### Evento que chega (variante de e-mail)
```jsonc
{
  "id": "det_002",
  "detectionRule": "Email.Phishing.Advanced",
  "attackType": "Phishing",
  "ruleDescription": "Advanced phishing with graphics spoofing"
}
```

### Regra que você cria
- **target:** `normalized.finding_info.title`
- **source principal:** `detectionRule`
- **fontes alternativas:** `attackType` (e quantas mais precisar, na ordem de preferência)
- **valor padrão:** `Unknown Detection`

### Resultado normalizado
```jsonc
{ "normalized": { "finding_info": { "title": "Email.Phishing.Advanced" } } }
```

### Por que funciona
O editor tenta a fonte principal primeiro. Se ela vier vazia, tenta a próxima fonte alternativa, e assim por diante. Se todas vierem vazias, usa o valor padrão. Assim você sempre obtém *algum* título, mesmo quando o fornecedor é inconsistente.

### Variações
- As fontes alternativas são tentadas da esquerda para a direita; a primeira que tiver valor vence.
- Para uma lógica mais elaborada, combine esta receita com uma condição (Receita 6).

:::note
A opção de fontes alternativas exige a versão mais recente do formato de mapeamento. Se ela não aparecer no editor para o seu mapeamento, fale com o administrador da plataforma para confirmar qual versão está em uso.
:::

---

## Receita 4: Abrir um JSON escondido dentro de um texto

**Use esta receita quando** o fornecedor empacota dados importantes como um texto JSON dentro de um único campo, e você precisa alcançar os campos de dentro (IP de origem, remetente do e-mail, etc.). É comum em detecções de e-mail do Sophos.

### Evento que chega
```jsonc
{
  "id": "det_003",
  "time": "2026-04-27T12:30:00Z",
  "severity": 7,
  "processedData": "{\"parsedAlert\":{\"fields\":{\"clientIp\":\"192.0.2.1\",\"mailFrom\":\"attacker@evil.com\"}}}"
}
```

### Regra que você cria
Esta receita tem duas etapas no editor:

1. **Etapa de preparo:** adicione um passo que lê o campo de texto (`processedData`) e o abre como JSON, guardando o resultado em uma área de trabalho interna (aqui chamada de `_processed`). Marque esse passo como **tolerante**.
2. **Etapas de regra:** agora aponte as regras normais para os campos de dentro:
   - **target:** `normalized.src_endpoint.ip` — **source:** `_processed.parsedAlert.fields.clientIp`
   - **target:** `normalized.actor.user.email_addr` — **source:** `_processed.parsedAlert.fields.mailFrom`

### Resultado normalizado
```jsonc
{
  "normalized": {
    "src_endpoint": { "ip": "192.0.2.1" },
    "actor": { "user": { "email_addr": "attacker@evil.com" } }
  }
}
```

### Por que funciona
O passo de preparo transforma o texto JSON em um objeto navegável. Depois disso, as regras alcançam os campos internos usando o caminho com pontos, como se sempre tivessem estado ali.

### Variações
- Deixar o passo **tolerante** é recomendado para fornecedores com problemas ocasionais de codificação: se o texto vier quebrado, o campo fica vazio em vez de mandar o evento para a Quarentena.
- Se você esquecer o passo de preparo, o dry-run avisa que a regra aponta para uma área que ainda não foi preenchida. Adicione o passo e o aviso some.
- Textos JSON muito grandes são recusados por segurança. O limite é definido pela equipe de infraestrutura no momento do deploy. Se precisar ajustá-lo, fale com o administrador da plataforma.

---

## Receita 5: Montar uma lista de indicadores de várias fontes

**Use esta receita quando** você precisa produzir a lista de indicadores (observables) de um evento combinando pedaços que estão espalhados: IP de origem, endereços de e-mail, hashes e nomes de arquivos anexados. É o coração de uma detecção de e-mail bem normalizada, porque é essa lista que alimenta as buscas em **Operação -> Investigações**.

### Evento que chega (já com o JSON aberto, como na Receita 4)
```jsonc
{
  "_processed": {
    "parsedAlert": {
      "fields": {
        "clientIp": "192.0.2.1",
        "mailFrom": "attacker@evil.com",
        "envelopeRecipients": ["victim1@company.com", "victim2@company.com"],
        "attachments": [
          { "name": "invoice.exe", "checksum": "abc123def456" },
          { "name": "document.pdf", "checksum": "ghi789jkl012" }
        ]
      }
    }
  }
}
```

### Regra que você cria
Use uma regra do tipo **construtor de lista** apontando para `normalized.observables`. Dentro dela, adicione um item para cada tipo de indicador:

| Item | Tipo | Source |
|---|---|---|
| IP de origem | IP Address | `_processed.parsedAlert.fields.clientIp` |
| Remetente | Email Address | `_processed.parsedAlert.fields.mailFrom` |
| Destinatários | Email Address | `_processed.parsedAlert.fields.envelopeRecipients` |
| Hash do anexo | Hash | `_processed.parsedAlert.fields.attachments[*].checksum` |
| Nome do anexo | File Name | `_processed.parsedAlert.fields.attachments[*].name` |

Para os itens que vêm de listas (destinatários, hashes, nomes), marque a opção **expandir**, para gerar um indicador por elemento. Marque também a opção de **remover duplicados** pelo valor.

### Resultado normalizado
```jsonc
{
  "normalized": {
    "observables": [
      { "name": "src_ip",    "type": "IP Address",    "value": "192.0.2.1" },
      { "name": "email_from", "type": "Email Address", "value": "attacker@evil.com" },
      { "name": "email_to",   "type": "Email Address", "value": "victim1@company.com" },
      { "name": "email_to",   "type": "Email Address", "value": "victim2@company.com" },
      { "name": "file_hash",  "type": "Hash",          "value": "abc123def456" },
      { "name": "file_hash",  "type": "Hash",          "value": "ghi789jkl012" },
      { "name": "file_name",  "type": "File Name",     "value": "invoice.exe" },
      { "name": "file_name",  "type": "File Name",     "value": "document.pdf" }
    ]
  }
}
```

### Por que funciona
- **Expandir:** quando a fonte é uma lista, gera um indicador por elemento. A lista de dois destinatários vira dois indicadores de e-mail.
- **Remover duplicados:** depois de juntar tudo, descarta indicadores repetidos pelo mesmo valor, mantendo a primeira ocorrência. Evita que o mesmo IP ou e-mail apareça duas vezes.
- A notação `attachments[*].checksum` significa "para cada anexo, pegue o checksum". Combinada com **expandir**, cria um indicador por anexo.

### Variações
- **Lista ausente:** se um campo de lista vier vazio, nenhum indicador é criado para aquele item — sem erro.
- **Anexos vazios:** se não há anexos, nenhum indicador de hash ou nome é gerado.
- **Critério de duplicidade:** você pode remover duplicados por valor (padrão) ou por tipo e valor juntos, para um controle mais fino.

---

## Receita 6: Preencher um campo só quando houver dado

**Use esta receita quando** você só quer escrever um campo se o fornecedor de fato enviou aquele dado — caso contrário, prefere deixá-lo ausente em vez de gravar um valor vazio. Útil para campos como o e-mail do usuário, que só aparece em algumas variantes do evento.

### Evento que chega
```jsonc
{
  "id": "alert_001",
  "description": "Intrusion blocked",
  "person": { "name": "John Doe", "email": "john@company.com" }
}
```

### Regra que você cria
- **target:** `normalized.actor.user.email_addr`
- **source:** `person.email`
- Adicione uma **condição**: execute a regra apenas se `person.email` existir.

### Resultado normalizado (com e-mail)
```jsonc
{ "normalized": { "actor": { "user": { "email_addr": "john@company.com" } } } }
```

### Resultado normalizado (sem e-mail)
```jsonc
{ "normalized": {} }
```

### Por que funciona
A condição "existe" só deixa a regra rodar se o campo de origem tiver algum valor. Se `person.email` estiver vazio, a regra é pulada inteira e o campo de destino não é escrito.

Repare na diferença em relação a um valor padrão: um padrão `null` ainda gravaria o campo com valor vazio. A condição "existe" omite o campo de vez.

### Variações
- **existe vs. igual a vs. está em:** "existe" verifica se há valor; "igual a" verifica um valor específico; "está em" verifica se o valor pertence a uma lista.
- **Inverter:** use a condição de negação (Receita 7) para rodar a regra justamente quando o campo estiver ausente.
- **Várias condições ao mesmo tempo** (E/OU) ainda não são suportadas; por enquanto, use regras separadas.

---

## Receita 7: Ignorar valores de preenchimento ("unknown")

**Use esta receita quando** o fornecedor manda um campo mas às vezes o preenche com um marcador genérico, como `"unknown"`, e você não quer levar esse marcador para o evento normalizado.

### Evento que chega
```jsonc
{ "id": "flow_001", "direction": "outbound" }
```

### Regra que você cria
- **target:** `normalized.traffic.direction`
- **source:** `direction`
- Adicione uma **condição de negação**: execute a regra apenas quando `direction` **não** for `"unknown"`.

### Resultado normalizado (direção válida)
```jsonc
{ "normalized": { "traffic": { "direction": "outbound" } } }
```

### Resultado normalizado (direção igual a "unknown")
```jsonc
{ "normalized": {} }
```

### Por que funciona
A condição de negação inverte a lógica: a regra só roda quando a direção **não** é o valor de preenchimento. Assim, o `"unknown"` nunca chega ao evento normalizado.

### Variações
- A negação pode envolver qualquer outra condição — por exemplo, "o valor **não** está nesta lista".
- Não há motivo para aninhar duas negações; use direto a condição base.

---

## Receita 8: Converter táticas MITRE para o formato OCSF

**Use esta receita quando** o fornecedor envia as táticas MITRE em um formato próprio e o OCSF espera o formato padrão. O Sophos XDR, por exemplo, manda cada tática em um bloco diferente do esperado. Se você não converter, esses eventos podem cair em Quarentena.

### Evento que chega
```jsonc
{
  "id": "det_004",
  "mitreAttacks": [
    { "tactic": { "id": "TA0001", "name": "Initial Access" } },
    { "tactic": { "id": "TA0002", "name": "Execution" } }
  ]
}
```

### Regra que você cria
- **target:** `normalized.attacks`
- **source:** `mitreAttacks`
- Aplique a conversão de **táticas MITRE para OCSF**.

### Resultado normalizado
```jsonc
{
  "normalized": {
    "attacks": [
      { "tactics": [{ "uid": "TA0001", "name": "Initial Access" }] },
      { "tactics": [{ "uid": "TA0002", "name": "Execution" }] }
    ]
  }
}
```

### Por que funciona
A conversão reorganiza cada tática no formato OCSF, ajusta os nomes dos campos, agrupa as táticas como o padrão exige e carimba a versão OCSF em uso. Táticas vazias que o fornecedor às vezes envia em alertas de baixa confiança são descartadas em silêncio.

### Variações
- **Sem táticas:** se o campo vier vazio, o resultado também fica vazio.
- **Tática incompleta:** se um item não tiver identificador ou nome, o evento vai para **Normalização -> Quarentena** com falha de mapeamento — corrija a origem ou trate o campo antes da conversão.

---

## Receita 9: Normalizar um score de 0 a 1 para 0 a 100

**Use esta receita quando** o fornecedor envia confiança ou probabilidade como um número decimal entre 0 e 1, e o OCSF espera uma porcentagem inteira de 0 a 100.

### Evento que chega
```jsonc
{ "id": "alert_001", "alertScore": 0.85 }
```

### Regra que você cria
- **target:** `normalized.confidence`
- **source:** `alertScore`
- Aplique a conversão de **score para porcentagem**.

### Resultado normalizado
```jsonc
{ "normalized": { "confidence": 85 } }
```

### Por que funciona
A conversão multiplica o decimal por 100 e arredonda (`0,85 → 85`). Se o valor já vier entre 0 e 100, passa direto. Se vier vazio, o resultado fica vazio.

### Variações
- Se o fornecedor mandar um valor fora da faixa esperada (por exemplo, `1.1`), a conversão falha e o evento vai para a Quarentena. Trate o valor antes — com um mapa de valores ou um ajuste prévio.

---

## Receita 10: Horário obrigatório com alternativas

**Use esta receita quando** o horário do evento é obrigatório no OCSF, mas alguns eventos chegam sem ele. Você define um horário principal, algumas alternativas e o que fazer caso nenhuma exista. É a causa número um de eventos parados em **Normalização -> Quarentena**.

### Evento que chega
```jsonc
{ "id": "alert_001", "createdAt": "2026-04-27T12:30:00Z" }
```

### Regra que você cria
- **target:** `normalized.time`
- **source principal:** `createdAt`
- **fontes alternativas:** `raisedAt`, `processedAt`
- Aplique a conversão de **data ISO para horário Unix** e marque a regra como **obrigatória**.
- Decida o **valor padrão** com cuidado (veja as variações).

### Resultado normalizado (caminho feliz)
```jsonc
{ "normalized": { "time": 1745764200 } }
```

### Por que funciona
O editor tenta `createdAt` primeiro; se vier vazio, tenta `raisedAt`, depois `processedAt`. Em seguida, converte o que encontrou para horário Unix. Como a regra é obrigatória, se nada for encontrado e você **não** tiver definido um padrão, o evento vai para a Quarentena — o comportamento desejado para não normalizar um evento sem horário.

### Variações
- **Cuidado com o padrão `0`:** o horário Unix `0` corresponde a 1º de janeiro de 1970, o que confunde as buscas. Em geral, é melhor **não** definir padrão e deixar a regra obrigatória mandar para a Quarentena os eventos sem horário, para você tratá-los à parte.
- Se preferir um padrão, escolha um valor que faça sentido para a sua operação e **descreva o motivo** na mensagem ao salvar a versão, para que a equipe entenda a decisão depois.

---

## Receita 11: Remover destinatários duplicados de uma lista

**Use esta receita quando** uma lista chega com itens repetidos — por exemplo, destinatários de e-mail duplicados por causa de regras de encaminhamento — e você quer apenas os valores únicos.

### Evento que chega
```jsonc
{
  "id": "det_005",
  "envelopeRecipients": ["user@domain.com", "user@domain.com", "admin@domain.com"]
}
```

### Opção A — para a lista de indicadores
Use uma regra do tipo **construtor de lista** (como na Receita 5), com o item de destinatários marcado para **expandir** e a opção de **remover duplicados** pelo valor ativada.

### Opção B — para uma lista simples
Se você só quer uma lista plana de endereços, use uma regra direta:
- **target:** `normalized.email_recipients`
- **source:** `envelopeRecipients`
- Aplique a conversão de **remover duplicados**.

### Resultado normalizado (opção B)
```jsonc
{ "normalized": { "email_recipients": ["user@domain.com", "admin@domain.com"] } }
```

### Por que funciona
Em ambos os casos, os itens repetidos são descartados e a primeira ocorrência é mantida. Escolha a opção A se você precisa de indicadores (para alimentar Investigações) e a opção B se basta uma lista simples de valores.

### Variações
- Você pode remover duplicados por valor (padrão) ou por mais de um campo ao mesmo tempo, para um critério mais fino.

---

## Receita 12: Marcar um campo que sempre fica em branco de propósito

**Use esta receita quando** um fornecedor não envia um campo que o OCSF recomenda (por exemplo, IP de origem), mas você quer manter a regra no mapeamento — e evitar que o dry-run a aponte como possível erro toda vez.

### Evento que chega
```jsonc
{ "id": "alert_001", "description": "Policy violation", "severity": "medium" }
```

### Regra que você cria
- **target:** `normalized.src_endpoint.ip`
- **source:** `sourceIp`
- **valor padrão:** vazio (`null`)
- Marque a opção que indica **"este campo fica sempre no padrão de propósito"**.

### Resultado normalizado
```jsonc
{ "normalized": { "src_endpoint": { "ip": null } } }
```

### Por que funciona
Sempre que o dry-run percebe que uma regra cai no valor padrão em 100% dos eventos de amostra, ela mostra um aviso na barra de status, sugerindo um possível erro de mapeamento. Ao marcar essa regra como intencional, você silencia esse aviso específico. A marcação não muda o que a regra faz — é só um sinal para os diagnósticos.

### Variações
- **Por que não apagar a regra?** Mantenha-a se acredita que o fornecedor pode passar a enviar o campo no futuro. A marcação documenta a intenção sem poluir o dry-run.
- **Use com moderação.** Se muitas regras precisam dessa marcação, o mapeamento provavelmente está incompleto — reserve-a para lacunas conhecidas.
- Aproveite a mensagem ao salvar a versão para registrar por que aquele campo fica sempre em branco.

---

## Tabela de resumo

| Receita | Cenário | Recurso-chave |
|---|---|---|
| 1 | Severidade numérica do fornecedor | conversão para inteiro |
| 2 | Severidade em texto para número | mapa de valores |
| 3 | Título com várias fontes possíveis | fontes alternativas |
| 4 | Abrir JSON escondido em texto | passo de preparo (abrir JSON) |
| 5 | Montar lista de indicadores | construtor de lista + expandir |
| 6 | Preencher campo só quando houver dado | condição "existe" |
| 7 | Ignorar valores de preenchimento | condição de negação |
| 8 | Táticas MITRE para OCSF | conversão MITRE para OCSF |
| 9 | Score de 0–1 para 0–100 | conversão score para porcentagem |
| 10 | Horário obrigatório com alternativas | obrigatório + fontes alternativas |
| 11 | Remover duplicados de lista | remover duplicados |
| 12 | Campo sempre em branco de propósito | marcação de campo intencional |
