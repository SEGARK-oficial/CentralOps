---
sidebar_position: 10
title: "A plataforma não está respondendo? O que fazer"
description: "Guia para o usuário identificar sinais de que o CentralOps está fora do ar ou degradado e saber quando acionar o administrador da plataforma — mais o procedimento de recuperação, para o administrador, quando uma atualização fica pendurada na migração de schema."
---

# A plataforma não está respondendo? O que fazer

Às vezes o CentralOps pode demorar a carregar, mostrar erros ao abrir uma tela, ou não exibir dados que você sabe que existem. Esta página ajuda você a reconhecer esses sinais, fazer as verificações simples que estão ao seu alcance pela interface e saber **quando** e **como** acionar o administrador da plataforma.

> Nada **nesta primeira parte** exige terminal, comandos ou conhecimento técnico. Tudo é feito pela interface web ou consiste em avisar a pessoa certa.

:::note[Você é o administrador e a plataforma parou logo depois de uma atualização?]

Duas seções no fim desta página são para você — ambas exigem acesso ao terminal e ao banco de dados. Escolha pelo **log da API**:

- **O log parou na linha da migração e não saiu de lá**, sem erro nenhum: **[A atualização ficou pendurada na migração](#upgrade-pendurado)**. A API não crashou, ela ficou bloqueada antes de subir.
- **O log mostra um erro de `lock timeout`** e o container reinicia em laço: **[A migração abortou com "lock timeout"](#lock-timeout)**. Aqui o processo morreu — e o erro diz o motivo.

:::

## Quando usar

- **Logo após uma janela de manutenção ou atualização**: você tenta entrar e a tela de login não carrega, ou entra mas as telas aparecem vazias. Antes de abrir um chamado urgente, vale confirmar se o problema é só com você ou com toda a plataforma.
- **No meio de um plantão do SOC**: você abre **Visão geral → Dashboard** ou **Detecta → Queries salvas** e os dados não atualizam há um tempo. Você precisa decidir rápido se isso é uma falha da plataforma ou apenas ausência de eventos novos.
- **Ao receber relatos de colegas**: vários analistas dizem que "o CentralOps caiu". Você quer fazer uma checagem rápida e padronizada antes de escalar para a infraestrutura.

## Sinais de que algo está errado

| O que você observa | O que geralmente significa |
| --- | --- |
| A tela de login não abre ou fica girando sem terminar | A plataforma pode estar reiniciando ou indisponível |
| Você consegue logar, mas as telas aparecem em branco ou com erro ao carregar | A interface subiu, mas o serviço por trás dela ainda não está pronto |
| Uma tela específica falha (ex.: **Integrações** não lista nada), mas o resto funciona | Pode ser um problema pontual daquela área, não da plataforma toda |
| Os números do **Dashboard** ou os resultados das consultas em **Queries salvas** estão "congelados" há bastante tempo | Pode ser falta de eventos novos **ou** o processamento em segundo plano parado |

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
- **Detecta → Queries salvas**
- **Coleta → Integrações**

- Se **todas** falham em carregar, o problema é geral.
- Se **apenas uma** falha e as outras funcionam, o problema é localizado naquela área.

Anote quais telas funcionam e quais não — isso ajuda muito quem for investigar.

### 4. Verifique se os dados estão apenas parados (não ausentes)

Se as telas abrem, mas os números parecem "congelados":

- Em **Visão geral → Saúde do pipeline**, confira se o processamento dos eventos está acontecendo normalmente.
- Em **Detecta → Queries salvas** e **Visão geral → Dashboard**, observe os horários dos eventos mais recentes.

Se os eventos mais recentes pararam num mesmo horário e não voltam a chegar, registre esse horário. Isso indica que o processamento em segundo plano (que recebe e trata os eventos) pode ter parado — e é uma informação importante para o administrador.

## Quando e como acionar o administrador

Acione o administrador da plataforma quando:

- A plataforma continua indisponível após você esperar alguns minutos e recarregar.
- **Todos** os usuários estão afetados, não só você.
- Os dados estão claramente parados (eventos novos deixaram de aparecer) e não voltam.
- Uma tela essencial para a sua operação segue falhando depois das verificações acima.

Para que o atendimento seja rápido, descreva o que você observou:

- **O que você tentou fazer** (ex.: "abrir a tela de Queries salvas").
- **O que aconteceu** (ex.: "fica girando e nunca carrega" ou "mostra erro ao carregar").
- **Quem está afetado** (só você ou todos os colegas).
- **Desde quando** você notou o problema, com horário aproximado.
- **Quais telas funcionam e quais não**, se você fez essa verificação.
- Se aplicável, **a partir de que horário os eventos pararam** de chegar.

> A recuperação da plataforma (subir os serviços, restabelecer o banco de dados e o processamento) é feita pela equipe de infraestrutura. Esse trabalho não é feito pela interface do CentralOps e não cabe ao usuário. O seu papel é reconhecer o sintoma, fazer as verificações simples acima e repassar essas informações ao administrador.

Se o que você descreveu foi "parou logo depois da atualização", o administrador tem um procedimento pronto para esse caso: **[A atualização ficou pendurada na migração](#upgrade-pendurado)**, logo abaixo.

## O que **não** fazer

- Não fique recarregando a página dezenas de vezes em poucos segundos — isso não acelera a recuperação.
- Não conclua que "perdeu dados" só porque uma tela está vazia: muitas vezes é apenas a plataforma ainda subindo. Confirme com o administrador antes de assumir perda.
- Não tente "reiniciar" nada por conta própria; não há nada na interface para isso, e a recuperação é responsabilidade da infraestrutura.

## A atualização ficou pendurada na migração {#upgrade-pendurado}

:::info[Daqui para baixo é o procedimento do administrador]

Esta seção exige acesso ao terminal do host (ou ao cluster) e ao banco de dados da plataforma. Ela trata de **um** sintoma: você trocou a tag da imagem, subiu os serviços, e a API **nunca terminou de subir** — sem erro nenhum no log.

:::

O defeito está na **2.3.0** e aparece só quando ela sobe sobre um banco **que já tinha dados**. A **2.3.1 corrige** — e é para lá que este runbook termina levando você.

### Sintoma

- O container da API aparece como `unhealthy` e nunca fica saudável. No Kubernetes, o pod nunca chega a `Ready` e acaba sendo reiniciado pelo `startupProbe`.
- O log da API **para** na linha da migração e não sai dela:

  ```
  start-api: migração de schema (python -m app.db.migrate)...
  ```

  Depois disso, nada. Nenhuma exceção, nenhum aviso, nenhuma linha nova — nem quando você espera dez minutos. É esse silêncio que engana: parece "ainda trabalhando".

- O container **não caiu**:

  ```bash
  docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}} health={{.State.Health.Status}}' \
    $(docker compose -f compose/docker-compose.yml ps -q centralops)
  # running exit=0 health=unhealthy
  ```

  `running` com `exit=0` é a assinatura do caso. Um processo que morresse por falta de memória, credencial errada ou schema inválido deixaria código de saída **diferente de zero** e uma mensagem no log. Aqui o processo está vivo e **bloqueado**, esperando algo que não vem.

- Os serviços de coleta ficam no mesmo estado, pelo mesmo motivo: cada um roda a mesma migração antes de subir (`start-collector: migração de schema...`).

:::note[Ambiente de teste recém-criado não reproduz]
O travamento depende de existir uma coluna nova a acrescentar numa tabela **que já existe com dados**. Numa instalação limpa o schema nasce inteiro de uma vez e o passo problemático nem chega a rodar — por isso uma stack criada do zero sobe na 2.3.0 sem sintoma algum. Reproduzir exige uma cópia do banco de produção.
:::

### Como confirmar

Abra um `psql` no banco da plataforma. No Compose:

```bash
docker compose -f compose/docker-compose.yml exec postgres \
  psql -U centralops -d centralops
```

No Kubernetes o Postgres normalmente é gerenciado e fica fora do cluster — use o cliente `psql` com que você já administra essa instância.

Liste as sessões abertas:

```sql
SELECT pid,
       state,
       wait_event_type,
       wait_event,
       now() - xact_start AS tempo_na_transacao,
       left(query, 60) AS query
FROM pg_stat_activity
WHERE datname = current_database()
  AND pid <> pg_backend_pid()
ORDER BY xact_start;
```

No impasse, a saída tem esta cara — este é o caso real que originou o runbook:

```
 pid | state               | wait_event_type | wait_event | tempo_na_transacao | query
-----+---------------------+-----------------+------------+--------------------+-----------------------------------
  42 | idle in transaction | Client          | ClientRead | 00:41:12           | UPDATE integrations SET ...
  50 | active              | Lock            | relation   | 00:41:12           | SELECT pg_catalog.pg_attribute...
  51 | active              | Lock            | advisory   | 00:38:02           | SELECT pg_advisory_lock(202235908)
```

Como ler cada linha:

| O que você vê | O que significa |
|---|---|
| `idle in transaction` + `ClientRead` | Esta sessão **abriu uma transação, alterou coisas e parou**. O banco não está travado nem lento: ele está **ocioso, esperando o cliente** mandar o próximo comando. Quem não manda é o processo do lado de fora. |
| `Lock` / `relation` | Esta sessão quer mexer numa **tabela** e está na fila de um bloqueio que outra sessão segura. |
| `Lock` / `advisory` | Esta sessão está esperando a **vez de migrar**. A plataforma serializa a migração com um cadeado (`pg_advisory_lock(202235908)`) para que réplicas e workers não apliquem o mesmo DDL ao mesmo tempo. Quem aparece aqui é **vítima**, não causa — normalmente é outro container tentando subir. |

A assinatura do impasse são as **duas primeiras** linhas juntas: `idle in transaction`/`ClientRead` de um lado, `Lock`/`relation` do outro, com **tempo na transação praticamente igual e só crescendo**. No caso acima, as pids `42` e `50` são **duas conexões do mesmo processo**: a primeira segura a tabela `integrations` e espera o processo falar; o processo está parado na segunda, que espera a tabela liberar. O ciclo existe, mas **passa por fora do banco** — e é exatamente por isso que o banco não o enxerga.

:::warning[O Postgres não desfaz isso sozinho]
O detector de deadlock do Postgres enxerga **ciclos de espera por bloqueio**. Do ponto de vista dele não há ciclo aqui: a pid 42 não espera bloqueio nenhum — ela espera o **cliente**. Então nada expira, nada aparece em log de erro e nada é abortado. O impasse permanece indefinidamente, até alguém intervir.
:::

Enquanto durar, **qualquer** consulta a `integrations` entra na fila — inclusive as de containers que estavam saudáveis.

### Como sair

Três passos, **nesta ordem**. Leia o passo 2 inteiro antes de executá-lo.

#### 1. Pare quem está tentando subir

Enquanto os containers reiniciam em laço, cada tentativa abre sessões novas — você derrubaria as antigas para nada.

```bash
docker compose -f compose/docker-compose.yml stop centralops \
  collector-worker-priority collector-worker-bulk collector-worker-maintenance \
  collector-worker-query collector-dispatcher collector-kafka-dispatcher collector-beat
```

No Kubernetes, zere as réplicas:

```bash
kubectl -n centralops scale deploy/centralops-api --replicas=0
kubectl -n centralops get deploy -o name | grep worker | xargs -n1 kubectl -n centralops scale --replicas=0
```

O `stop` manda `SIGTERM` — muitas vezes isso já encerra o processo travado, e o Postgres desfaz a transação sozinho. **Rode a consulta da seção anterior de novo.** Se não sobrou nenhuma sessão da plataforma, pule direto para o passo 3.

#### 2. Derrube as sessões que sobraram

Uma sessão pode continuar `idle in transaction` mesmo com o container já morto: o Postgres só percebe que o cliente sumiu quando tenta falar com ele — e ninguém está falando. Enquanto ela existir, ela segura a tabela.

Derrube **pelo `pid`**, um a um, começando pela que está `idle in transaction`:

```sql
SELECT pg_terminate_backend(42);
```

:::danger[O que este comando faz de verdade — não o copie às cegas]

`pg_terminate_backend` **aborta a transação em curso** da sessão que você indicar. É por isso que ele resolve, e é por isso que ele pede atenção:

- **Contra a migração travada, é seguro — mas não porque "tudo é desfeito".** A migração **não** é uma transação única: são cerca de **vinte passos independentes**, cada um com o seu próprio commit (apagar tabelas legadas, criar o que falta, e depois uma sequência de alterações irmãs). Abortar no meio desfaz **apenas o passo em curso**; os que já commitaram permanecem aplicados. Isso é seguro porque **cada passo é idempotente**: ele confere o que já existe antes de agir. O estado que sobra é **parcial, porém válido** — nunca uma coluna pela metade ou um schema inconsistente — e **o boot seguinte completa o que faltou sozinho**. Não existe passo manual de banco para "acertar" a metade que ficou.
- **Neste travamento da 2.3.0, nem parcial fica.** Aqui foi verificado que o banco fica exatamente como estava: o impasse acontece **dentro do primeiro bloco grande de alterações, antes do commit dele**, e os passos anteriores não tinham o que fazer — a 2.3.0 não cria tabela nova, então a etapa de criação é um no-op numa base 2.2.0. É por isso que voltar para a 2.2.0 (passo 3) funciona sem nenhum ajuste de banco.
- **Contra o resto, não é.** Um `pid` que não seja da migração pode ser uma escrita legítima de coleta, e derrubá-lo joga aquele lote fora. **Não** dispare o comando contra tudo que aparece na `pg_stat_activity`: olhe a coluna `query` de cada `pid` na consulta anterior e derrube só os que fazem parte do impasse.
- **Isto não conserta o defeito.** Se você subir a **2.3.0** de novo, o boot pendura no mesmo ponto. A saída é o passo 3.

:::

Confirme que as sessões sumiram repetindo a consulta. As que estavam em `Lock`/`advisory` saem sozinhas assim que a primeira cair.

#### 3. Suba na 2.3.1

Troque a tag da imagem para a **2.3.1** e recrie os serviços — a mecânica genérica (Compose e Helm) está em **[Atualizar de versão](../deployment/upgrading)**. Não há passo manual de banco: a migração que pendurava passa direto, e o log segue para o start do servidor.

:::tip[Se você ainda não tem a 2.3.1 à mão]
Voltar à **tag imutável da 2.2.0** também restabelece o serviço — justamente porque a 2.3.0 não chegou a aplicar nada no banco. É uma parada temporária, não o destino: a 2.2.0 não tem o que a 2.3.0 trouxe.
:::

Confirme que o boot passou do ponto onde travava:

```bash
docker compose -f compose/docker-compose.yml ps          # centralops: healthy
docker compose -f compose/docker-compose.yml logs centralops | grep edition=
```

E confirme que o banco voltou ao normal: repetindo a consulta da `pg_stat_activity`, nenhuma sessão deve ficar `idle in transaction` acumulando tempo.

### A causa

A migração de schema abria uma **segunda conexão com o banco** no meio da própria transação, só para conferir quais colunas a tabela já tinha — e essa segunda conexão ficava esperando a liberação de uma tabela que a **primeira** conexão, da mesma migração, mantinha bloqueada.

O defeito existia havia várias versões, adormecido. O que o acordou foi a **2.3.0** ser a primeira em muito tempo a acrescentar uma coluna em `integrations` — e só em bancos que já existiam, porque só neles há coluna a acrescentar.

**Corrigido na 2.3.1**: a verificação passou a usar a conexão da própria transação. Não há segunda conexão, não há espera, e o passo continua idempotente.

## A migração abortou com "lock timeout" {#lock-timeout}

:::info[Daqui para baixo também é procedimento do administrador]

Este é o **outro** desfecho possível — e é o desejável. A partir da **2.3.1** a migração tem um **teto de espera por bloqueio**: em vez de pendurar em silêncio, ela desiste e aborta o boot com um erro explícito. O padrão é **15 segundos**.

Você trocou "travado para sempre, sem pista nenhuma" por "falhou rápido, com o motivo no log". Se você chegou aqui, o mecanismo funcionou.

:::

### Sintoma

Ao contrário do caso anterior, aqui **o processo morre** — e por isso o container entra em laço de reinício **de verdade**:

```bash
docker compose -f compose/docker-compose.yml ps centralops
# NAME         STATUS
# centralops   Restarting (1) 4 seconds ago
```

`Restarting (1)` é a assinatura: o **`1`** é o código de saída, e é justamente ele que separa este caso do anterior — lá o processo continuava vivo com `exit=0`. Se você pegar o container no meio de uma tentativa, o `STATUS` pisca para `Up`; espere alguns segundos e olhe de novo, ou confira o contador de reinícios.

E o log, desta vez, **diz o que houve**:

```
start-api: migração de schema (python -m app.db.migrate)...
ERROR [app.db.migrate] migrate: falha ao inicializar o schema
  ...
  sqlalchemy.exc.OperationalError: (psycopg2.errors.LockNotAvailable)
  canceling statement due to lock timeout
start-api: migração FALHOU — abortando.
```

| Sinal | Este caso (lock timeout) | O caso anterior (pendurado) |
|---|---|---|
| Estado do container | `restarting`, `exit=1` | `running`, `exit=0` |
| Log | erro explícito em segundos | silêncio, indefinidamente |
| Quem resolve sozinho | ninguém — mas você **vê** o problema | ninguém, e você **não vê** |

**O que isto significa:** a migração precisou alterar uma tabela, mas **outra sessão do banco já a estava segurando** — e não liberou dentro do teto. A migração não aplicou aquele passo. Como cada passo é idempotente, o próximo boot retoma de onde parou; mas ele vai falhar de novo enquanto o detentor do bloqueio continuar lá.

### Quem está segurando o bloqueio

Abra um `psql` no banco da plataforma (mesmo comando da seção anterior) e pergunte **quem bloqueia quem**:

```sql
SELECT bloqueada.pid          AS pid_bloqueada,
       bloqueante.pid         AS pid_bloqueante,
       bloqueante.state       AS estado_bloqueante,
       bloqueante.application_name,
       bloqueante.client_addr,
       now() - bloqueante.xact_start AS tempo_na_transacao,
       left(bloqueante.query, 80)    AS query_bloqueante
FROM pg_stat_activity AS bloqueada
JOIN LATERAL unnest(pg_blocking_pids(bloqueada.pid)) AS blocker(pid) ON true
JOIN pg_stat_activity AS bloqueante ON bloqueante.pid = blocker.pid
WHERE bloqueada.datname = current_database();
```

Se a migração já abortou, ela não aparece mais como bloqueada — mas o **detentor** continua lá. Liste os candidatos diretamente:

```sql
SELECT pid,
       state,
       application_name,
       client_addr,
       now() - xact_start AS tempo_na_transacao,
       left(query, 80)    AS query
FROM pg_stat_activity
WHERE datname = current_database()
  AND state <> 'idle'
  AND xact_start IS NOT NULL
ORDER BY xact_start;
```

Interprete pelo **tempo na transação** e pela coluna `query`:

| O que você encontra | O que fazer |
|---|---|
| Uma sessão `idle in transaction` parada há minutos ou horas | É o suspeito clássico: alguém abriu uma transação e não fechou (um `psql` esquecido aberto, um script interrompido, uma ferramenta de BI). Confirme com a pessoa e derrube pelo `pid` — vale o mesmo alerta do `pg_terminate_backend` da seção anterior. |
| Um `VACUUM FULL`, `CREATE INDEX` ou restore/backup em andamento | **Não derrube.** Espere terminar e suba de novo. Derrubar um restore no meio custa muito mais caro que aguardar. |
| Uma escrita legítima da própria plataforma, longa mas ativa | Prefira aumentar o teto (abaixo) a matar a sessão. |
| Nada com tempo relevante | O bloqueio já passou. Só suba os serviços de novo. |

### Quando aumentar o teto em vez de derrubar a sessão

O teto existe para você **não ficar cego**, não para forçar uma janela de 15 segundos. Aumente-o quando a espera for **legítima e previsível**:

- o banco tem manutenção pesada em curso (`VACUUM FULL`, reindexação, restore) que você prefere deixar terminar;
- a sua instalação é grande e o `ALTER TABLE` legitimamente demora para conseguir o bloqueio num horário movimentado;
- você está atualizando **sem** janela de manutenção, com coleta rodando, e as escritas normais disputam a tabela.

Ajuste pela variável `APP_DB_MIGRATION_LOCK_TIMEOUT_MS` (em **milissegundos**), nos serviços que rodam a migração — a API e os de coleta:

```yaml
# compose/docker-compose.yml — no serviço centralops e nos collector-*
environment:
  APP_DB_MIGRATION_LOCK_TIMEOUT_MS: "60000"   # 60 s
```

No Helm, pelo mesmo caminho de variáveis de ambiente do chart.

:::warning[Aumentar o teto não é desligá-lo]

Valor **inválido ou menor/igual a zero cai no padrão** de 15 s — de propósito. Não existe "sem teto": desligar reintroduz exatamente o pendura-para-sempre que esta versão veio consertar, e um erro de digitação não deve conseguir isso.

Suba o valor com parcimônia. Um teto de dez minutos devolve boa parte da cegueira original: você fica dez minutos sem saber se está trabalhando ou esperando.

:::

Se você aumentou o teto e a migração **ainda** aborta, o problema não é o teto — é o detentor do bloqueio. Volte para a consulta acima.

### Como sair

1. **Identifique o detentor** com as consultas acima e decida: esperar terminar, ou derrubar pelo `pid`.
2. **Se for derrubar**, use `pg_terminate_backend(<pid>)` — e leia antes o alerta da seção anterior sobre o que esse comando faz.
3. **Suba os serviços de novo.** A migração retoma do ponto em que parou; os passos que já tinham commitado não são refeitos.

## Próximos passos

- **A atualização travou e você quer as notas da versão?** Veja [Atualizar de versão](../deployment/upgrading).
- **Os eventos de uma integração específica pararam de chegar?** Veja [Coletores](../pipelines/collectors.md).
- **A plataforma está no ar, mas você quer entender a saúde do processamento?** Veja [Saúde do Pipeline](../operations/pipeline-health.md).
