---
sidebar_position: 12
title: "Destino: nano SIEM"
description: Envie eventos normalizados ao nano SIEM via ingestão OCSF direta para análise imediata.
---

# Destino: nano SIEM

O destino **nano SIEM** entrega os eventos já normalizados pelo CentralOps diretamente para o nano, um SIEM leve cuja camada de armazenamento é ClickHouse e cuja ingestão fala OCSF. Você cria e gerencia tudo pela interface, na tela de Destinos.

Esta tela só aparece para administradores da plataforma.

## Por que um destino próprio para o nano

O nano é um SIEM que, por desenho, **não repassa o evento por seu pipeline de parsing**. Ele recebe OCSF já normalizado (da mesma forma que Tenzir, Cribl e Axoflow o recebem) e oferece a camada de correlação, detecção e investigação.

O CentralOps é, por construção, uma camada de normalização: coleta do vendor, mapeia para OCSF 1.8, valida contra o manifesto, enriquece e reduz. Essa postura bate com a do nano, e este kind é o atalho que evita errar qualquer uma de cinco decisões acopladas.

Se você tivesse que usar o ClickHouse genérico, precisaria acertar juntos:
- porta HTTP 8123 (não nativa 9000);
- banco `nanosiem`, tabela `ocsf_logs_raw`;
- conteúdo OCSF puro (não envelope);
- forma wrapped (evento aninhado + rótulo);
- usuário `nanosiem_ingest` (INSERT-only);

Aqui são dois campos: onde falar com o nano, e com que rótulo.

Referência oficial: [nano SIEM, OCSF direct integration](https://nano.rs/docs/ocsf/integrations/direct-ocsf/)

## Quando usar

- **Ingerir eventos normalizados sem processamento adicional.** O nano não mexe no OCSF, você entrega pronto, e a plataforma cuida de correlação, detecção e busca.
- **Usar ClickHouse para análise de dados brutos.** A tabela `ocsf_logs_raw` do nano aceita consultas SQL diretas. Você monta dashboards, alertas e relatórios no ClickHouse.
- **Escopo de detecção por fonte.** O rótulo `source_type` é a chave de escopo das regras de detecção do nano, cada feed tem seu contexto isolado.

## O que você precisa antes de começar

Para criar o destino, tenha em mãos:

- **Endereço HTTP do nano**: a URL da instância nano, porta 8123, com `http://` ou `https://` (por exemplo, `https://nano.interno:8123`). Quem administra o nano fornece esse endereço. Se apenas lhe derem a porta 9000, saiba que é o protocolo nativo TCP, procure a HTTP, que costuma estar no mesmo host na porta 8123.
- **Usuário de ingestão**: geralmente `nanosiem_ingest`, um usuário INSERT-only criado no nano. Nunca use o usuário da aplicação (que lê e escreve o banco inteiro).
- **Senha do usuário**: a `CLICKHOUSE_INGEST_PASSWORD` gerada pelo instalador do nano. O CentralOps guarda essa senha de forma criptografada; ela nunca aparece em tela depois de salvo.
- **Rótulo do feed**: um identificador minúsculo para esta origem no nano (ex.: `centralops_sophos`, `centralops_wazuh`). É a chave de escopo das detecções, sem ele as linhas caem como `unknown` e as regras com escopo de fonte ignoram tudo.

> O instalador do nano gera a credencial `CLICKHOUSE_INGEST_PASSWORD` para o usuário `nanosiem_ingest`. No CentralOps você apenas informa esse valor ao criar o destino.

## Criar o destino

1. No menu lateral, abra **Coleta -> Integrações** para confirmar que suas fontes já estão coletando, depois vá em **Roteia -> Destinos**.
2. Use a opção de criar um novo destino.
3. Escolha o tipo **nano SIEM**.
4. Preencha os campos abaixo.

| Campo | O que informar |
|-------|----------------|
| **Nome** | Um nome que ajude a identificar este destino (ex.: "nano SIEM Produção"). |
| **URL** | O endereço HTTP do nano com porta 8123 (ex.: `https://nano.interno:8123`). Não use a porta 9000 (protocolo nativo). |
| **Rótulo da origem** | Identificador minúsculo deste feed no nano (ex: `centralops_sophos`). É a chave de escopo das detecções e painéis, sem ele as linhas caem como `unknown`. |
| **Usuário** | Padrão: `nanosiem_ingest`. Mude apenas se o nano usou outro nome para o usuário de ingestão. |
| **Senha** | A senha do usuário de ingestão. Fica criptografada após salvar. |
| **Banco** | Padrão: `nanosiem`. Mude apenas se sua instância usa banco diferente. |
| **Tabela** | Padrão: `ocsf_logs_raw`. Use essa tabela para ingestão OCSF. A tabela `ocsf_logs_native_raw` é para o sink nativo do Tenzir (protocolo TCP na porta 9000), não para o caminho HTTP. |
| **Verificar TLS** | Mantenha ativado para garantir uma conexão segura. |
| **CA bundle** | Caminho para certificado customizado (opcional). Normalmente definido pela equipe de infraestrutura no momento do deploy. |

### Sobre o rótulo da origem

O campo **Rótulo da origem** (`source_type`) precisa ser:
- **Minúsculo.** O nano separa as facetas de detecção por caixa. Um valor `Sophos_Central` será interpretado diferente de `sophos_central`. Se você digitar maiúscula, o salvar falha com uma mensagem clara.
- **Sem espaço.** Um rótulo como `sophos central` não é válido.
- **Único por feed.** Cada feed (Sophos, Wazuh, CrowdStrike) deve ter seu próprio rótulo. Reuse o mesmo rótulo para N coletores da mesma plataforma.

Se você suspeitar que as linhas caem como `unknown`, confirme que o rótulo foi digitado idêntico dos dois lados: no CentralOps (este destino) e na configuração das regras e painéis do nano.

### Sobre a verificação TLS e certificados próprios

Mantenha **Verificar TLS** ativado sempre que possível, ele garante que o CentralOps está mesmo falando com seu nano.

Se o nano usa um certificado próprio (autoassinado ou de uma autoridade interna), a confiança nesse certificado é definida pela equipe de infraestrutura no momento do deploy. Se a conexão segura falhar por causa do certificado, fale com o administrador da plataforma para que ele registre o certificado correto. Você não precisa lidar com arquivos de certificado pela interface.

### Salvar o destino

Clique em **Salvar** para criar o destino. Ele já fica **ativo** (badge verde) e começa a receber os eventos roteados para ele.

### Testar a conexão

Após criar o destino, abra a página de detalhes e use o botão **Testar** (ícone de play) no cabeçalho. O CentralOps verifica:

- conectividade com a instância do nano;
- credencial (usuário e senha);
- existência do banco e tabela;
- compatibilidade entre as colunas emitidas e as da tabela.

Se o teste passar, a conexão está OK. Se falhar, o relatório detalhado ajuda a identificar o problema: porta errada, tabela não encontrada, colunas incompatíveis.

> O teste de conexão valida a configuração sem enviar dados reais, use antes de ativar o roteamento para esse destino em produção.

:::info[O resultado normal aqui é "passou, com uma ressalva"]
O `nanosiem_ingest` é INSERT-only por desenho: ele não lê o catálogo do banco. Então os dois últimos itens da lista acima não rodam, e o teste diz isso em vez de fingir que verificou. Passar com essa ressalva é o resultado esperado, não um defeito.

Se quiser a verificação completa de banco, tabela e colunas, dê a permissão de leitura de schema no ClickHouse do nano:

```sql
GRANT SHOW COLUMNS ON nanosiem.* TO nanosiem_ingest
```

Isso não dá acesso a conteúdo de log, só a nomes de coluna.
:::

### Simular a forma da linha

Use o botão **Simular** (ícone de olho) para visualizar exatamente qual será a linha JSON enviada ao nano. Com a configuração padrão, será:

```json
{"event": {"class_uid": 1006, "time": 1786000000000, ...}, "source_type": "seu_rotulo"}
```

A simulação mostra a forma sem enviar dados, é apenas uma prévia para verificação.

## Como os eventos são entregues

Você não precisa configurar nada do funcionamento interno, ele já vem ajustado para entrega eficiente ao nano. Vale apenas entender o comportamento:

- **Envio em lotes.** Os eventos são agrupados e enviados em blocos, o que é mais eficiente do que enviar um a um. Cada linha do INSERT é um evento em formato JSON.
- **Nova tentativa automática.** Se o nano recusar ou ficar indisponível por um instante, o CentralOps tenta reenviar sozinho, esperando um pouco mais entre cada tentativa.
- **Proteção contra destino instável.** Se o nano começar a falhar de forma persistente, o CentralOps pausa o envio por um curto período e volta a tentar automaticamente.
- **Entrega ao menos uma vez.** Em uma queda no momento errado, um evento pode chegar duplicado. Cada evento carrega um identificador único (`_centralops.event_id` no envelope), então você pode fazer dedup no ClickHouse.

O payload é **sempre OCSF** (`normalized` do pipeline), nunca envelope canônico. A forma é **sempre wrapped** (evento aninhado + rótulo). Esses valores são fixos por design do nano e não são editáveis.

## Acompanhar a saúde do destino

Abra **Roteia -> Destinos** e selecione o destino nano.

O badge de saúde mostra a situação atual:

| Cor | Significado |
|-----|-------------|
| Verde | Eventos sendo entregues normalmente, sem itens na fila de reenvio. |
| Amarelo | Eventos chegando, mas há itens parados na fila de reenvio. |
| Vermelho | Envio pausado pela proteção contra destino instável ou nano indisponível. |
| Cinza | Destino desativado. |

Na visão do destino você acompanha as métricas em tempo real:

- **Eventos por segundo**: ritmo de entrega na última hora.
- **Volume**: quanto dado está saindo na última hora.
- **Latência média**: quanto o nano leva para responder.
- **Itens na fila de reenvio (24h)**: quantos eventos foram recusados no último dia.

Para ver os eventos que não puderam ser entregues, abra a **fila de reenvio** na visão do destino. Cada item mostra o identificador do evento, o motivo da recusa informado pelo nano/ClickHouse, o horário e o conteúdo exato que foi rejeitado, útil para entender e corrigir a causa.

Para uma visão mais ampla de como os dados percorrem a plataforma até os destinos, use **Roteia -> Fluxo de dados** e **Visão geral -> Saúde do pipeline**.

## Resolver problemas comuns

| Sintoma | O que verificar |
|---------|-----------------|
| **Não conecta ao nano, "You must use port 8123 for HTTP"** | Você usou a porta 9000? Aquela é o protocolo nativo TCP do ClickHouse, só para clientes nativos. Este destino fala HTTP. Use 8123 (ou 8443 com TLS). |
| **Não conecta ao nano** | A URL está completa, com `https://` (ou `http://`) e a porta correta? O nano está no ar? Se houver firewall entre as redes, o time de infraestrutura precisa liberar o acesso à porta. |
| **Usuário/senha recusados (erro 401 ou 403)** | O usuário `nanosiem_ingest` existe no nano? A senha foi digitada corretamente? O usuário tem permissão de INSERT na tabela? Verifique no nano e atualize o destino. |
| **Teste falha com "banco/tabela não encontrados"** | O banco `nanosiem` e a tabela `ocsf_logs_raw` existem? Verifique no nano. Se não existem, o instalador do nano pode não ter criado o schema ainda. |
| **Linhas caem como "unknown" no nano** | O rótulo foi digitado idêntico dos dois lados? Na regra de detecção do nano você usar `source_type=centralops_sophos` mas aqui digitou `centralops_sophos` com maiúscula? Confirme a grafia minúscula. |
| **Envio pausado / "muitas requisições"** | O nano/ClickHouse pode estar sobrecarregado. A proteção contra destino instável volta a tentar sozinha após um curto intervalo; se persistir, acione a equipe do nano para revisar carga. |
| **Falha de certificado seguro (TLS)** | Acontece quando o nano usa um certificado próprio que a plataforma ainda não reconhece. Essa confiança é configurada no deploy, fale com o administrador da plataforma. |

## Próximos passos

- **Confirmar que os dados estão chegando:** abra **Roteia -> Destinos**, selecione o nano e veja as métricas de eventos por segundo. Verifique também a tabela `ocsf_logs_raw` no nano: `SELECT COUNT(*) FROM ocsf_logs_raw WHERE source_type = 'seu_rotulo'`.
- **Investigar eventos recusados:** abra a fila de reenvio na visão do destino.
- **Montar detecções no nano:** use a interface de detecção do nano com escopo `source_type=seu_rotulo`.
- **Adicionar outros destinos:** veja a [visão geral de destinos](./overview.md).
- **Decidir quais eventos vão para cada destino:** use a tela de [Roteamento](./routing.md).
