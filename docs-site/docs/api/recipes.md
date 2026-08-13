---
sidebar_position: 7
title: Receitas
description: Tarefas prontas com curl, do monitoramento externo ao destravamento de um coletor parado.
---

# Receitas

Cada receita traz o papel e os scopes mínimos, os endpoints e o que observar na resposta. Os exemplos assumem duas variáveis:

```bash
export CENTRALOPS_URL="https://centralops.example.com"
export CENTRALOPS_TOKEN="copsk_..."
```

## Monitoramento externo, somente leitura

O caso do Zabbix, do Grafana ou do script de plantão: perguntar de tempos em tempos se o pipeline está de pé, sem poder mexer em nada.

**Token:** papel `viewer`, com scopes `integration.read`, `destination.read` e `route.read`.

Esse é o menor conjunto que enxerga coleta e entrega. Ele não escreve nada, não lê credencial e não administra conta.

```bash
curl -X POST "$CENTRALOPS_URL/api/v1/tokens" \
  -H "Authorization: Bearer $CENTRALOPS_TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "zabbix-producao",
    "expires_at": "2027-01-01T00:00:00Z",
    "scopes": ["integration.read", "destination.read", "route.read"]
  }'
```

### As três perguntas que valem a pena

**1. O dado está entrando?**

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$CENTRALOPS_URL/api/collectors/state"
```

Devolve uma linha por integração e fluxo. Os campos que importam:

| Campo | Para que serve |
|-------|----------------|
| `watermark_at` | Até que momento o coletor **já processou** de fato |
| `last_success_at` | Quando o coletor rodou sem erro pela última vez |
| `consecutive_failures` | Quantas tentativas seguidas falharam |
| `last_run_capped` | O ciclo bateu no teto e parou no meio do backlog |
| `last_error` | A mensagem do último erro |

:::danger[Monitore `watermark_at`, não `last_success_at`]
Esta é a diferença que já custou 15 horas de atraso passando por saudável em produção.

`last_success_at` responde "o coletor rodou?". Ele é atualizado a cada ciclo bem sucedido, **mesmo quando o ciclo processou eventos de ontem**. Um coletor com backlog enorme roda, tem sucesso, atualiza esse campo, e continua meio dia atrasado. Um alerta baseado nele fica verde o tempo todo.

`watermark_at` responde "até quando ele chegou?". É a idade do dado mais recente que efetivamente passou. Se ele está três horas atrás, você está três horas atrás, não importa quantos ciclos tiveram sucesso.

Alerte pela idade de `watermark_at`. Use `last_run_capped` como sinal de apoio: verdadeiro significa que o ciclo parou no teto e ainda há backlog, ou seja, a distância não vai fechar sozinha no ritmo atual.
:::

```bash
# Idade do watermark, em minutos, por integração e fluxo
curl -s -H "Authorization: Bearer $TOKEN" "$CENTRALOPS_URL/api/collectors/state" \
| jq -r --arg agora "$(date -u +%FT%TZ)" '
  .[] | [ .integration_name,
          .stream,
          ((($agora | fromdateiso8601) - ((.watermark_at // "1970-01-01T00:00:00") + "Z" | fromdateiso8601)) / 60 | floor),
          .consecutive_failures,
          .last_run_capped ] | @tsv'
```

A saída fica assim, com a idade em minutos na terceira coluna:

```
edr-corp        detections   180   0   false
fw-perimetro    events         2   3   true
```

A primeira linha é o caso que importa: três horas de atraso, e **nenhuma falha**. Um alerta que só olhasse `consecutive_failures` não veria nada de errado ali.

O `+ "Z"` no meio da expressão está lá porque `watermark_at` vem sem fuso, e `fromdateiso8601` exige o sufixo. Veja a nota sobre os dois formatos de data em [Convenções](conventions.md#datas).

**2. A entrega está de pé?**

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$CENTRALOPS_URL/api/collectors/destinations/health"
```

Esse é o endpoint **de lote**: uma chamada traz todos os destinos. Prefira sempre ele a varrer destino por destino, porque o custo por requisição não é desprezível (veja [Convenções](conventions.md#limite-de-requisições)).

Observe o estado do disjuntor e a contagem da fila de mortos. Fila crescendo significa que o CentralOps está normalizando bem e o destino é que está recusando.

**3. O pipeline está saudável no conjunto?**

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$CENTRALOPS_URL/api/integrations/pipeline-health"
```

Agrega coleta, quarentena e campos novos por integração. Lembre que a resposta tem cache de 60 segundos por usuário: o campo `cached_at` diz de quando ela é. Se você consultar a cada 30 segundos, metade das leituras vem repetida, e um número parado pode ser cache em vez de estagnação.

### Ritmo sugerido

Um intervalo de 60 segundos é folgado e fica muito longe do limite de requisições. As três chamadas acima somam três requisições por ciclo, contra um teto padrão de 100 por minuto.

## Destravar um coletor parado

**Token:** papel `operator`, scope `integration.reset`.

Quando `consecutive_failures` está alto e `last_error` aponta cursor inválido ou posição perdida, zerar o cursor faz o coletor recomeçar.

```bash
curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  "$CENTRALOPS_URL/api/collectors/state/42/detections/cursor"
```

:::caution[Isso gera duplicidade temporária]
O coletor volta a partir da janela padrão do fornecedor, tipicamente uma hora atrás. Os eventos daquela janela são coletados de novo.

A deduplicação absorve a maior parte, mas ela tem prazo. Reset é a ferramenta certa para um coletor travado, e não é ferramenta de rotina.
:::

Para forçar um ciclo sem mexer no cursor:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  "$CENTRALOPS_URL/api/collectors/state/42/detections/trigger"
```

Isso dispara uma chamada real à API do fornecedor e consome a quota dele. A resposta traz o identificador da tarefa, não o resultado: confira o efeito lendo o estado de coleta de novo alguns segundos depois.

## Triagem de quarentena

**Token:** papel `operator`, scopes `quarantine.read` e `quarantine.discard`.

Quarentena é onde ficam os eventos que a normalização recusou.

```bash
# O que está parado, e por quê
curl -s -H "Authorization: Bearer $TOKEN" \
  "$CENTRALOPS_URL/api/quarantine?limit=50&offset=0" \
| jq '.total, (.items[] | {id, error_kind, error_detail})'

# Depois de corrigir o mapping, reprocessar em lote
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "$CENTRALOPS_URL/api/quarantine/bulk/reprocess" \
  -d '{"ids": [101, 102, 103]}'
```

O campo do corpo chama `ids`, tanto no reprocessamento quanto no descarte. Não é `event_ids`.

:::danger[Descartar apaga de verdade]
`bulk/discard` remove os eventos definitivamente. Não existe lixeira nem desfazer.

E os dois verbos compartilham a mesma permissão: um token que pode reprocessar também pode apagar. Não existe como conceder só o reprocessamento.
:::

Duas coisas a observar na resposta do reprocessamento:

- Identificador de outra organização volta como "não encontrado" na lista de erros, não como `403`. Isso é proposital, para não permitir varredura de identificadores.
- Se o evento falhar de novo na normalização, ele continua na quarentena com o erro atualizado. Reprocessar não é garantia de saída.

O lote aceita até 500 identificadores por chamada.

## Recoletar um período depois de corrigir um mapping

**Token:** papel `admin` (a criação de backfill exige `integration.write`).

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "$CENTRALOPS_URL/api/integrations/42/backfill" \
  -d '{
    "stream": "detections",
    "from_ts": "2026-08-01T00:00:00Z",
    "to_ts": "2026-08-07T00:00:00Z"
  }'

# Acompanhar
curl -s -H "Authorization: Bearer $TOKEN" "$CENTRALOPS_URL/api/backfill-jobs/17"
```

Limites que o servidor aplica:

- Janela máxima de 90 dias por trabalho.
- A data inicial não pode estar mais de 90 dias no passado.
- Integração pai de MSSP é recusada com `422`, porque ela não tem fluxo próprio.

Cancelar não interrompe na hora. O trabalhador termina a página atual e sai limpo na iteração seguinte:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  "$CENTRALOPS_URL/api/backfill-jobs/17/cancel"
```

:::tip[Se o backfill parece nunca começar]
Existe um endpoint de diagnóstico que verifica se a API e os trabalhadores estão falando com o mesmo intermediário de mensagens:

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$CENTRALOPS_URL/api/backfill-jobs/diagnostics"
```

Ele exige permissão de administrador. É o primeiro lugar a olhar quando uma tarefa é aceita com `201` e nada acontece depois.
:::

## Inventário para auditoria

**Token:** papel `viewer`, scopes `integration.read`, `mapping.read` e `audit.read`.

```bash
# O que está configurado
curl -s -H "Authorization: Bearer $TOKEN" "$CENTRALOPS_URL/api/integrations/" \
| jq -r '.[] | [.id, .name, .platform, .is_active] | @tsv'

# Quem mudou o quê
curl -s -H "Authorization: Bearer $TOKEN" \
  "$CENTRALOPS_URL/api/history/?limit=100"
```

Se o seu token tem escopo global e a listagem voltou vazia, você provavelmente esqueceu de nomear a organização. Veja [a explicação em Permissões](permissions.md#a-resposta-vazia-que-parece-não-tem-dado).

## Um roteiro de verificação para automação nova

Antes de colocar qualquer automação em produção:

1. Chame um endpoint que ela **precisa** e confirme `200`.
2. Chame um endpoint que ela **não deveria** alcançar e confirme `403`. Se vier `200`, o token está mais largo do que você pensa.
3. Force um erro de autenticação (mude um caractere do token) e confirme que a sua automação trata `401` sem entrar em laço.
4. Confirme que ela respeita o `Retry-After` de um `429`.
5. Registre o `X-Correlation-Id` de cada chamada no seu log. Isso transforma "não funcionou ontem à noite" em uma busca de um minuto.

O passo 2 é o que mais gente pula, e é o que pega token criado com escopo largo por engano.
