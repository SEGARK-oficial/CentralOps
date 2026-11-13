---
sidebar_position: 14
title: Cloudflare (WAF e segurança)
description: Colete os eventos de segurança das suas zonas na Cloudflare (WAF, regras de firewall, rate limiting, bots) pela GraphQL Analytics API — ação aplicada, regra, IP real do cliente, geo e a requisição HTTP
---

# Cloudflare (WAF e segurança)

Conecte as zonas da Cloudflare ao CentralOps para coletar os **eventos de segurança**: cada requisição que um produto de segurança da Cloudflare bloqueou, desafiou, registrou ou deixou passar por uma regra, com a ação aplicada, a regra que atuou, o **IP real do cliente**, o país e o ASN, e o recorte da requisição.

Quando o seu site está atrás da Cloudflare, o WAF, o proxy ou o firewall que fica depois dela só enxerga IPs da Cloudflare. É na Cloudflare que o IP verdadeiro do atacante aparece, e é isso que esta integração traz para o pipeline.

A fonte é a **GraphQL Analytics API** (dataset `firewallEventsAdaptive`, o mesmo que alimenta a tela *Security Events* do painel). Não exige infraestrutura: só um API token de leitura.

## Quando usar

- **Ver quem ataca de verdade.** O IP do cliente por trás da Cloudflare, com país, ASN e *user agent*, para bloquear ou investigar.
- **Correlacionar a borda com o resto.** O mesmo IP em logins (Entra ID, Okta, Workspace), no firewall interno ou no EDR.
- **Medir o que as regras fazem.** Quais regras bloqueiam, quais só registram (`log`) e quais desafios os clientes resolvem.
- **Auditar exceções.** Eventos `skip`, `allow` e `bypass` mostram quando uma regra liberou tráfego.

## Como funciona

```
Zonas Cloudflare ──► GraphQL Analytics API ──(Bearer token, polling 1 min)──► stream security_events
                     POST /client/v4/graphql      uma consulta por zona e página
```

- A cada minuto, para **cada zona**, o CentralOps lê primeiro os limites que a Cloudflare declara para aquela zona (nó `settings`: campos disponíveis, tamanho de página, largura máxima da janela e retenção) e só então consulta os eventos dentro desses limites. Nenhum limite é fixo no código.
- A primeira coleta lê a **última hora**. Daí em diante a leitura continua de onde parou, **zona por zona**: o cursor guarda o instante de cada uma.
- A janela termina 60 segundos antes de agora (latência de ingestão da Cloudflare).
- Página cheia: o CentralOps continua do instante do último evento no mesmo ciclo, até 10 páginas por ciclo. Se sobrar backlog, o cursor fica salvo e a **Saúde do Pipeline** marca o stream como em *backlog*.

## O que coletar: análise de segurança

| Campo do evento | Para que serve |
|---|---|
| `action` | O que a Cloudflare fez: `block`, `managed_challenge`, `challenge`, `jschallenge`, `log`, `skip`, `allow`, `bypass`, resultados de desafio (`*_solved`, `*_failed`, `*_bypassed`, `*_skipped`), `connection_close`. |
| `source` / `ruleId` / `description` | Produto que atuou (`firewallcustom`, `firewallmanaged`, `ratelimit`, `bic`, `l7ddos`…), ID e nome da regra. |
| `rayName` | ID da requisição (Ray ID). Uma requisição pode gerar **vários** eventos, um por produto ou regra. |
| `clientIP` / `clientIPClass` | IP real do cliente e a classificação de reputação. |
| `clientAsn` / `clientASNDescription` / `clientCountryName` | ASN, nome da operadora e país do cliente. |
| `clientRequestHTTPHost`, `…MethodName`, `…Protocol`, `clientRequestScheme`, `clientRequestPath`, `clientRequestQuery` | A requisição. |
| `userAgent` / `clientRefererHost` | *User agent* e host do *referer*. |
| `edgeResponseStatus` / `originResponseStatus` | Status devolvido ao cliente e status da origem. |
| `kind`, `matchIndex`, `rulesetId`, `edgeColoName`, `sampleInterval`, `ref` | Contexto adicional (vão para `unmapped`). |

O CentralOps só pede os campos que a zona expõe (`availableFields`) e que cabem no limite de campos por consulta (`maxNumberOfFields`, tipicamente 30). Cinco campos são obrigatórios: `datetime`, `rayName`, `action`, `source` e `clientIP`. Se a zona não expõe algum deles, o teste de conexão reprova e a coleta pula a zona.

## Segurança da integração

- **Host travado.** Só `api.cloudflare.com`, em HTTPS. Redirecionamento nunca é seguido.
- **Token só no header.** O token vai em `Authorization: Bearer`, nunca na URL, e nunca aparece em mensagem de erro, log ou resultado do teste de conexão. Está guardado no campo secreto, criptografado como os demais segredos do CentralOps.
- **Só leitura.** O CentralOps usa a permissão de leitura de analytics. Nenhuma mutação é enviada.
- **Zone IDs validados.** Só os 32 caracteres hexadecimais são aceitos e eles entram na consulta como variável, nunca no texto dela.

## Pré-requisitos

- Zonas na Cloudflare em qualquer plano (Free, Pro, Business ou Enterprise): o dataset de eventos de segurança está disponível em todos, com retenção de pelo menos 31 dias. No plano Free o painel mostra só amostras; confirme com o teste de conexão.
- Permissão para criar API tokens (ou um administrador que crie para você).
- Saída HTTPS (443) do CentralOps para `api.cloudflare.com`.

## Passo 1: Criar o API token

1. No painel da Cloudflare, abra **My Profile → API Tokens** (ou **Manage account → Account API tokens**, para um token de conta) e selecione **Create Token → Create Custom Token**.
2. Dê um nome, por exemplo `centralops-security-events`.
3. Em **Permissions**, adicione:
   - **Zone → Analytics → Read**: obrigatória, é o que permite consultar o dataset.
   - **Zone → Zone → Read**: só se você **não** for informar os Zone IDs e quiser que o CentralOps descubra as zonas sozinho.
4. Em **Zone Resources**, escolha **Include → Specific zone** (uma linha por zona) ou **All zones**.
5. Opcional: restrinja por IP de origem (o IP de saída do CentralOps) e defina uma data de expiração. Lembre de trocar o token antes que expire.
6. Selecione **Continue to summary → Create Token** e copie o token. Ele só é exibido uma vez.

:::note[Permissão de analytics]
O guia oficial de token do GraphQL Analytics usa **Account → Account Analytics → Read** como exemplo. Para consultar o dataset de uma **zona**, a permissão de zona acima é a que o CentralOps espera; se o teste de conexão acusar `sem permissão`, confira também se o token não ficou restrito a zonas diferentes das informadas.
:::

## Passo 2: Anotar os Zone IDs (opcional)

O **Zone ID** está na página *Overview* da zona, na coluna da direita (**API → Zone ID**): são 32 caracteres hexadecimais. Você pode informar até **50** zonas, separadas por vírgula.

Se deixar o campo em branco, o CentralOps coleta **todas as zonas ativas que o token enxerga**, descobertas a cada ciclo (exige **Zone → Zone → Read**). Zonas novas passam a ser coletadas sozinhas; sem os Zone IDs fixos, porém, é mais fácil exceder a cota de consultas (veja os limites abaixo).

## Passo 3: Criar a integração no CentralOps

1. No menu lateral, vá em **Coleta -> Integrações** e adicione uma integração **Cloudflare (WAF e segurança)**.
2. Preencha:

   | Campo | O que informar |
   |---|---|
   | API token da Cloudflare | O token do Passo 1. |
   | Zone IDs (opcional) | Um ou mais Zone IDs do Passo 2, separados por vírgula. Vazio = todas as zonas do token. |

3. Clique em **testar a conexão**. O teste verifica o token, resolve as zonas (informadas ou descobertas) e lê os limites do dataset em **cada** zona. Passa só se **todas** forem utilizáveis. Veja as mensagens na seção [Solução de problemas](#solução-de-problemas).
4. Salve. O stream `security_events` roda a cada minuto.

## O que é coletado

Cada evento chega normalizado em OCSF 1.8 como **HTTP Activity** (classe 4002), com a linha original da Cloudflare (mais o `zoneTag` da zona) preservada no `raw`.

| OCSF | Origem |
|---|---|
| `activity_id` | Método HTTP: GET → Get, POST → Post, PUT → Put, DELETE → Delete, HEAD, OPTIONS, TRACE, PATCH, CONNECT; outros → Other. |
| `disposition_id` / `action_id` / `severity_id` / `is_alert` | Pela ação, veja a tabela abaixo. |
| `src_endpoint` | `ip` = `clientIP` (o IP real); `location.country` = país; `autonomous_system` = número (`clientAsn`) e nome. |
| `dst_endpoint.hostname` | Host pedido. |
| `http_request` | `http_method`, `version` (protocolo), `user_agent`, `referrer` (host do *referer*), `uid` (= Ray ID) e `url` (`hostname`, `path`, `query_string`, `scheme`). |
| `http_response.code` | `edgeResponseStatus`: o status devolvido ao cliente. |
| `firewall_rule` | `uid` = `ruleId`, `name` = `description`, `type` = `source`. |
| `time` | `datetime` (ISO-8601 UTC, em segundos). |
| `metadata.uid` | Ray ID. `event_code` = ação original. |
| `unmapped.cloudflare_*` | `zone_id`, `kind`, `match_index`, `ruleset_id`, `client_ip_class`, `edge_colo`, `origin_response_status`, `sample_interval`, `ref`, só quando existem. |

Como cada ação vira OCSF:

| Ação da Cloudflare | `disposition` | `action` | Severidade | Alerta |
|---|---|---|---|---|
| `block`, `connection_close`, `force_connection_close`, `*_failed` | Blocked | Denied | Low | sim |
| `challenge`, `jschallenge`, `managed_challenge` (desafio emitido) | Challenge | Other | Low | sim |
| `*_solved`, `*_bypassed`, `*_skipped` (cliente passou ou foi dispensado do desafio) | Allowed | Allowed | Informational | não |
| `log` | Logged | Allowed | Medium | sim |
| `allow`, `skip`, `bypass` | Allowed | Allowed | Informational | não |
| qualquer outra | Other | Other | Informational | não |

- **Desafio emitido** não é nem permitido nem negado ainda: por isso `action` fica em *Other*, a mesma convenção usada para o AWS WAF.
- **`log`** deixa a requisição chegar à origem: severidade Medium, como o `alert`/`monitor` da Akamai.

O dedupe usa zona + Ray ID + `datetime` + `source` + `ruleId` + ação + `matchIndex`: a mesma requisição que dispara duas regras gera dois eventos distintos.

## Operação e saúde

- **Retenção.** O dataset guarda pelo menos 31 dias, mas o valor real vem do `notOlderThan` de cada zona. Se o CentralOps ficar parado além disso, o log do worker diz **quanto tempo de eventos se perdeu**; a leitura retoma do limite permitido (com 5 minutos de folga).
- **Janela.** Cada consulta cobre no máximo `maxDuration` da zona. Backlog maior que isso é lido em janelas consecutivas, no limite de 10 páginas por ciclo.
- **Amostragem.** `firewallEventsAdaptive` usa amostragem adaptativa: em picos de ataque a Cloudflare devolve uma amostra e informa a taxa em `sampleInterval` (1 = sem amostragem). Quem precisa do registro completo e é Enterprise deve usar o Logpush.
- **Mais de uma página no mesmo segundo.** A API só ordena por `datetime` (em segundos) e limita a página (`maxPageSize`, tipicamente 10.000). Se mais eventos que uma página caírem no **mesmo segundo**, o excedente não é alcançável: o log registra **perda parcial** e a leitura avança 1 segundo. Não vira laço infinito.
- **Zona com o dataset desligado.** Zona cujo plano não expõe o dataset é **pulada** com um WARNING, sem derrubar as demais. Se **nenhuma** zona for utilizável o ciclo falha, para a saúde da integração não ficar verde sem coletar nada.
- **Indicador de atraso.** O atraso exibido é o da zona mais atrasada.
- **Backfill.** Funciona dentro da retenção, em fatias de `maxDuration`; a fatia que enche a página é dividida ao meio, até 1 segundo. O backfill não mexe na posição da coleta normal.

## Limites da API da Cloudflare

| Limite | Valor | Fonte |
|---|---|---|
| Consultas GraphQL | 300 por usuário a cada 5 minutos (em regime, 1 por segundo) | [Limits](https://developers.cloudflare.com/analytics/graphql-api/limits/) |
| Zonas por consulta | Até 10 (o CentralOps consulta uma por vez) | [Limits](https://developers.cloudflare.com/analytics/graphql-api/limits/) |
| Retenção do dataset | Pelo menos 31 dias em todos os planos (`notOlderThan` informa o valor da zona) | [Limits](https://developers.cloudflare.com/analytics/graphql-api/limits/) |
| Janela por consulta | `maxDuration` da zona (até 30 dias nos planos Free, Pro e Business e 31 no Enterprise) | [Security Events](https://developers.cloudflare.com/waf/analytics/security-events/#limits) |
| Campos e linhas por consulta | `maxNumberOfFields` (tipicamente 30) e `maxPageSize` (tipicamente 10.000) | [Settings node](https://developers.cloudflare.com/analytics/graphql-api/features/discovery/settings/) |
| API geral (REST) | 1.200 chamadas a cada 5 minutos por token | [API limits](https://developers.cloudflare.com/fundamentals/api/reference/limits/) |

**Cota de 300 consultas.** Cada zona gasta uma consulta de `settings` e ao menos uma de eventos por ciclo (2 por minuto, ou seja 10 a cada 5 minutos). Com isso cabem cerca de **30 zonas** por token antes de a cota apertar. Acima disso a Cloudflare responde `rate limiter budget depleted`; o CentralOps respeita o `Retry-After` e tenta de novo, mas a coleta atrasa. Use mais de um token (um por grupo de zonas) ou a [cota por conta](https://developers.cloudflare.com/analytics/graphql-api/account-based-rate-limiting/).

## Custo e licenciamento

- A Cloudflare não cobra à parte pelo uso da GraphQL Analytics API; o dataset de eventos de segurança está disponível em todos os planos. Os **produtos** que geram os eventos (WAF gerenciado, rate limiting, Bot Management) seguem o seu plano: confirme com a Cloudflare o que o seu contrato inclui.
- Do lado do CentralOps, o volume é o de eventos de segurança, não o de tráfego: só requisições acionadas por uma regra viram evento.

## Solução de problemas

Mensagens do teste de conexão, por zona:

**`token ... — gere um token ativo`**: o token foi desativado, expirou ou foi revogado. Crie outro no Passo 1.

**`token não verificável em /user/tokens/verify (token de conta?)`**: aviso, não erro. Tokens de conta não passam por essa verificação; o teste de cada zona, a seguir, é a prova real.

**`token inválido, expirado ou revogado`**: a Cloudflare respondeu 401. Confira se o valor foi colado inteiro, sem espaços.

**`sem permissão — o token precisa da permissão Zone > Analytics > Read ...`**: o token não tem a permissão de leitura de analytics nessa zona, ou a zona não está nos *Zone Resources* do token. Edite o token na Cloudflare.

**`Falha ao descobrir as zonas: sem permissão — a descoberta automática de zonas exige Zone > Zone > Read`**: o campo de Zone IDs está vazio e o token não tem **Zone → Zone → Read**. Adicione a permissão ou informe os Zone IDs.

**`o token não enxerga nenhuma zona ativa`**: o token não tem nenhuma zona em *Zone Resources*, ou as zonas ainda estão pendentes.

**`o token não tem acesso a esta zona (ou o ID não existe)`**: Zone ID errado ou fora dos recursos do token.

**`dataset firewallEventsAdaptive desativado para esta zona (o plano não o expõe)`**: a Cloudflare não expõe o dataset para esta zona. A coleta pula a zona com um WARNING.

**`campos obrigatórios indisponíveis na zona: ...`**: a zona não expõe algum de `datetime`, `rayName`, `action`, `source`, `clientIP`. Sem eles não há evento utilizável.

**`ID de zona inválido (use os 32 caracteres hexadecimais do Zone ID)`**: o valor colado não é um Zone ID (não use o nome da zona nem o Account ID).

**`limite de consultas da Cloudflare atingido`**: a cota de 300 consultas por 5 minutos acabou. Aguarde o tempo informado.

Mensagens no log do worker:

**`zona ... pulada — ...`**: a razão está na mensagem (dataset desligado, campos, acesso). As demais zonas seguem.

**`o cursor (...) passou da retenção da API ... de eventos não podem mais ser lidos`**: o CentralOps ficou parado por mais tempo que a retenção da zona. O que passou dela não existe mais na API.

**`mais de N eventos em <segundo> ... perda parcial`**: mais eventos que uma página no mesmo segundo (ataque volumétrico). Os eventos acima do limite desse segundo não são alcançáveis pela API.

**`nenhuma zona utilizável`**: todas as zonas foram puladas; a integração fica com erro até uma delas voltar a funcionar.

## Limitações conhecidas

- Só os **eventos de segurança** (`firewallEventsAdaptive`). Logs de requisições completas (HTTP Requests via Logpush) e analytics de tráfego agregado não são coletados.
- Os dados podem ser **amostrados** em picos de volume (veja `sampleInterval`).
- A latência de ingestão da Cloudflare faz o CentralOps ler com ~1 minuto de atraso.
- `firewall_rule` traz uma regra por evento: uma requisição que dispara várias regras gera **vários eventos** (um por regra), ligados pelo mesmo `metadata.uid` (Ray ID).
- Até **50 zonas** por integração.
