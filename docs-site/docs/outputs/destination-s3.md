---
sidebar_position: 6
title: "Destino: S3 / Object Store (data lake)"
description: Guarde eventos normalizados em um bucket S3 para auditoria, retenção longa e consulta posterior.
---

# Destino: S3 / Object Store (data lake)

O destino **S3 / Object Store** grava seus eventos normalizados em um bucket de armazenamento (AWS S3 ou compatível, como MinIO, Wasabi, Ceph e Cloudflare R2), formando um "lago frio" de dados: armazenamento barato, de retenção longa, ideal para auditoria e consulta sob demanda. O CentralOps organiza os arquivos por organização e por data, e garante que o reenvio de um mesmo lote não gere duplicatas no bucket.

Como envolve a criação e a gestão de destinos, esta tela está disponível apenas para **administradores** da plataforma. Operadores e analistas conseguem consultar o status de entrega, mas a configuração é feita pelo admin.

## Quando usar

Use o destino S3 quando precisar de armazenamento durável e de baixo custo para grandes volumes de eventos, sem a urgência de consulta em tempo real:

- **Retenção de longo prazo para conformidade.** Manter todos os alertas do Wazuh (ou de qualquer integração) por 1 ano, 5 anos ou 7 anos para atender a auditorias de LGPD, ISO ou requisitos do cliente, sem inflar o custo do seu SIEM principal.
- **Data lake para o time de analytics/BI.** Centralizar todos os eventos normalizados em um único bucket para que o time de dados rode consultas analíticas (volume de incidentes por mês, tendências por severidade) com as ferramentas de lago que já utiliza.
- **Cópia de segurança independente do SIEM.** Manter um arquivo histórico fora da plataforma de busca quente, como rede de segurança caso um destino principal (Sentinel, Splunk, Elastic) precise ser reconstruído ou reindexado.

## Como o destino funciona

| Aspecto | Comportamento |
|---------|---------------|
| **Formato dos arquivos** | Uma linha por evento, em texto JSON; por padrão os arquivos são compactados para ocupar menos espaço. |
| **Organização no bucket** | Os arquivos são separados por organização e por data (ano/mês/dia), o que facilita localizar e consultar um período específico. |
| **Sem duplicatas no reenvio** | Quando o **mesmo lote** de eventos é reenviado, o CentralOps sobrescreve o arquivo existente em vez de criar uma cópia — o reenvio é seguro e não infla o bucket. |
| **Retenção** | Este destino é classificado como armazenamento de retenção longa, com aplicação automática do prazo de retenção configurado. |

:::info[Sobre duplicatas na leitura]
Se um lote for reprocessado de forma **parcial** (por exemplo, quando alguns eventos voltam pela fila de reenvio), o CentralOps pode gravar um arquivo novo em vez de sobrescrever — então, em casos raros, o mesmo evento pode aparecer em dois arquivos. Cada evento carrega um identificador único, e as ferramentas de consulta do lago conseguem remover duplicados por esse identificador na hora da leitura. Isso é esperado e não indica erro.
:::

## Antes de começar

Para que o CentralOps consiga gravar no bucket, três coisas precisam estar prontas. Os dois primeiros itens são tarefas de nuvem que normalmente ficam com a equipe de infraestrutura ou de cloud:

1. **Um bucket já criado e acessível.** Recomenda-se manter o versionamento e as regras de ciclo de vida (lifecycle) do bucket desligados — o CentralOps já cuida da deduplicação e da retenção, e essas opções do provedor podem conflitar com esse controle.
2. **Permissão de acesso ao bucket.** O CentralOps precisa de permissão para ler, gravar, listar e apagar objetos no bucket. Quem cuida da conta de nuvem deve conceder essa permissão à plataforma — seja por uma identidade gerenciada da própria nuvem, seja por uma chave de acesso. Se você não administra a conta de nuvem, peça ao seu administrador de nuvem que prepare a permissão antes de continuar.
3. **A credencial de acesso disponível no CentralOps**, caso o acesso seja por chave (veja o passo a passo abaixo).

:::note[Forma de autenticação]
A escolha entre usar a identidade gerenciada da nuvem (sem chave explícita) ou uma chave de acesso, e a permissão correspondente, é definida pela equipe de infraestrutura no momento do deploy e da configuração da conta de nuvem. Se precisar alterá-la, fale com o administrador da plataforma ou com o administrador de nuvem.
:::

## Passo 1: Criar o destino

1. No menu lateral, abra **Operação → Destinos**.
2. Clique no botão de criar um novo destino.
3. Em tipo de destino, escolha a opção de **S3 / Object Store**.
4. Dê um nome claro ao destino (por exemplo, "Data Lake - Produção"), que ajude a identificá-lo depois nas telas de roteamento e de saúde.

## Passo 2: Preencher a configuração

Preencha os campos do formulário. Os nomes podem variar ligeiramente na tela; abaixo está o que cada um significa e onde obtê-lo:

| O que informar | Descrição |
|----------------|-----------|
| **Nome do bucket** | O bucket de destino, já criado pela equipe de nuvem. |
| **Região** | A região do bucket (por exemplo, a região AWS onde ele foi criado). |
| **Prefixo / pasta raiz** | Pasta base dentro do bucket onde os arquivos serão guardados. Útil para separar ambientes (produção, teste). |
| **Endpoint** | Preencha apenas se você usa um armazenamento compatível que não seja a AWS (MinIO, Wasabi, Ceph, R2). Para AWS S3, deixe em branco. O endereço correto vem da sua equipe de nuvem. |
| **Compactação** | Mantenha a compactação ligada (padrão) para economizar espaço, salvo indicação contrária. |

**Credencial de acesso:**

- **Se o acesso usa a identidade gerenciada da nuvem:** marque a opção correspondente e não preencha nenhuma chave — a plataforma usa a permissão já concedida à própria infraestrutura.
- **Se o acesso usa chave:** informe o identificador da chave de acesso e guarde a parte secreta da credencial no cofre da plataforma, usando o botão de guardar credencial. A parte secreta é criptografada e nunca fica visível em texto puro.

## Passo 3: Testar a conexão

1. Ainda no formulário do destino, use o botão de **testar a conexão**.
2. O CentralOps tenta alcançar o bucket e confirma o acesso. Se estiver tudo certo, você verá uma confirmação de que o bucket está acessível.
3. Se o teste falhar, consulte a seção [Resolução de problemas](#resolução-de-problemas) abaixo antes de salvar.

Ao concluir, salve o destino.

## Passo 4: Enviar eventos para o destino

Criar o destino apenas o registra; ele só recebe eventos depois que você cria uma regra de roteamento apontando para ele.

1. No menu lateral, abra **Operação → Roteamento**.
2. Crie uma nova regra de envio.
3. Defina, na regra:
   - **A condição** — quais eventos vão para este destino. Deixe sem condição para enviar tudo, ou filtre (por exemplo, apenas eventos de severidade alta).
   - **O destino** — selecione o destino S3 que você acabou de criar.
4. Ative e salve a regra.

## Passo 5: Confirmar que os dados estão chegando

1. No menu lateral, abra **Normalização → Saúde do Pipeline**.
2. Localize o seu destino S3 na lista.
3. O status deve aparecer como **Ativo** (verde). Se aparecer inativo ou com erro, a credencial pode não ter sido resolvida — veja [Resolução de problemas](#resolução-de-problemas).

Depois disso, os arquivos passam a aparecer no bucket, separados por organização e por data. Quem consulta o lago (o time de analytics ou de BI) aponta a ferramenta de consulta para a pasta configurada.

## Retenção e remoção de dados

O destino S3 oferece dois controles importantes para conformidade, disponíveis na própria tela do destino, em **Operação → Destinos**:

### Aplicar a retenção (limpeza de dados antigos)

O destino aplica automaticamente o prazo de retenção configurado: arquivos mais antigos que o prazo definido são removidos do bucket. A data usada para decidir o que apagar vem da **data do próprio evento** (refletida na organização por ano/mês/dia), não da data em que o arquivo foi gravado — isso evita que um arquivo reescrito "rejuvenesça" e escape da retenção, o que poderia abrir uma brecha de conformidade.

O **prazo de retenção padrão** é de 1 ano. O valor exato e o agendamento dessa limpeza são definidos pela equipe de infraestrutura no momento do deploy. Se precisar alterar o prazo (por exemplo, para 7 anos por exigência regulatória), fale com o administrador da plataforma.

### Apagar os dados de uma organização (direito ao esquecimento — LGPD)

Quando for necessário atender a um pedido de exclusão sob a LGPD, o destino permite **apagar todos os dados de uma organização** guardados no bucket. A ação remove todos os arquivos daquela organização (de todos os períodos) e registra o que foi apagado para fins de auditoria. Por ser uma operação irreversível e sensível, ela é restrita a administradores e deve ser feita de forma deliberada, na tela do destino.

## Resolução de problemas

| Sintoma | O que verificar |
|---------|-----------------|
| **"Sem credencial S3" / destino inativo** | A credencial não foi resolvida. Se o acesso for por identidade gerenciada da nuvem, confirme que a opção correspondente está marcada. Se for por chave, confirme que a parte secreta foi guardada no cofre. Em seguida, use o botão de testar a conexão. |
| **"Acesso negado" / chave inválida** | A credencial ou a permissão está incorreta. Confirme o identificador da chave e peça à equipe de nuvem que verifique se a permissão de leitura, gravação, listagem e exclusão no bucket está concedida. Se você usa um armazenamento não-AWS, confirme com a equipe se o endereço (endpoint) está correto. |
| **"Conexão recusada" / tempo esgotado** | A plataforma não conseguiu alcançar o armazenamento. Geralmente é rede ou firewall bloqueando o acesso — algo que a equipe de infraestrutura precisa verificar. O CentralOps tenta reenviar automaticamente quando o problema é temporário. |
| **Os arquivos não aparecem no bucket** | Confirme em **Normalização → Saúde do Pipeline** que o destino está **Ativo**. Verifique também em **Normalização → Quarentena** se os eventos não foram retidos por algum problema de normalização. Lembre-se de que os arquivos são organizados pela data do evento, então procure na pasta da data correspondente. |
| **Eventos duplicados no bucket** | É esperado em reenvios parciais (alguns eventos voltando pela fila de reenvio). A remoção de duplicados é feita na hora da consulta, pelo identificador único de cada evento. O reenvio de um mesmo lote completo nunca duplica. |

## Consultar dados já gravados (search-in-place)

Depois que os eventos estão no bucket S3, você pode fazer buscas estruturadas diretamente nos arquivos guardados, sem reingerir ou carregar tudo novamente. Use a tela **Operação → Busca federada** e selecione a integração de origem S3/Lake.

O dialeto é **Filtros JSON estruturados** — uma sintaxe simples de key/value que não requer SQL:

```json
{
  "filters": [
    { "field": "severity", "op": "gte", "value": 4 },
    { "field": "source", "op": "eq", "value": "sophos" }
  ],
  "limit": 1000
}
```

Os dados consultados ficam isolados por organização — cada org vê só seus próprios eventos guardados no bucket. Se o bucket está em uma região diferente ou tem um endpoint customizado, essa configuração vem do momento em que o destino foi criado.

Para detalhes e exemplos, veja [Busca federada — Filtros JSON](../operations/federated-search.md#filtros-json-lakes3).

## Próximos passos

- **Confirmar que os dados estão chegando?** Veja [Saúde do Pipeline](../operations/pipeline-health.md).
- **Algum evento ficou retido?** Veja [Quarentena](../operations/quarantine.md).
- **Configurar quais eventos vão para o lago?** Veja [Roteamento](./routing.md).
- **Adicionar outros destinos?** Veja a [visão geral de destinos](./overview.md).
