---
sidebar_position: 13
title: Akamai App & API Protector (WAF)
description: Colete os eventos de segurança do WAF da Akamai (App & API Protector, Kona Site Defender) pela SIEM Integration API — regras disparadas, ação aplicada, IP e geo do atacante e a requisição HTTP
---

# Akamai App & API Protector (WAF)

Conecte o WAF da Akamai ao CentralOps para coletar os **eventos de segurança** das suas configurações de segurança: cada requisição que disparou uma regra na borda da Akamai, com o que foi feito com ela (bloqueada, só alertada, monitorada), quais regras dispararam, de onde veio e o que pedia.

A fonte é a **SIEM Integration API** da Akamai, a mesma usada pelos conectores oficiais de Splunk e CEF. Não exige infraestrutura: só ligar a coleta para SIEM nas configurações de segurança e criar um API client.

## Quando usar

- **Ver os ataques contra os seus sites e APIs.** SQL injection, XSS, *command injection*, *path traversal*, *scanners*, com a regra exata que disparou e o trecho da requisição que casou.
- **Distinguir o que foi barrado do que passou.** Uma regra em modo `alert` deixa a requisição chegar à origem: esses eventos pedem atenção maior que os `deny`.
- **Correlacionar a borda com o resto.** O IP de origem do ataque no WAF com o mesmo IP em logins (Entra ID, Okta, Workspace), no firewall ou no EDR.
- **Acompanhar bots e abuso de conta.** Com Bot Manager ou Account Protector licenciados, o evento traz a pontuação de bot e o risco do usuário.

## Como funciona

```
Configurações de segurança ──► SIEM Integration API ──(EdgeGrid, polling 1 min)──► stream siem
(Data collection for SIEM)     /siem/v1/configs/{id;id}
```

- O CentralOps pede os eventos de **todas** as configurações informadas numa chamada só (`/siem/v1/configs/111;222`).
- A primeira coleta lê a **última hora** por tempo. Daí em diante a leitura é sempre por **offset**: a Akamai devolve, no fim de cada resposta, a posição exata de onde continuar. É o modo que não perde evento — o modo por tempo corta em silêncio quando o volume passa do limite e pode pular os 5 segundos de latência da Akamai.
- Enquanto a resposta vier cheia (a Akamai sinaliza que bateu no limite), o CentralOps continua lendo no mesmo ciclo, até 10 páginas de 5.000 eventos.

:::info[Por que a credencial é assinada no próprio CentralOps]
A Akamai não usa token *bearer*: cada pedido é assinado com HMAC-SHA256 (o protocolo **EdgeGrid**). O CentralOps implementa a assinatura seguindo o SDK oficial da Akamai, sem biblioteca extra, e só assina pedidos para hosts sob `akamaiapis.net`.
:::

## O que coletar: análise de segurança

| Campo do evento | Para que serve |
|---|---|
| `attackData.appliedAction` | O que a Akamai fez: `deny` (e `deny_custom_*`), `alert`, `monitor`, `tarpit`, ações de bot. |
| `attackData.rules` / `ruleMessages` / `ruleTags` | As regras que dispararam (ID, mensagem, grupo de ataque como `WEB_ATTACK/XSS`). |
| `attackData.ruleData` / `ruleSelectors` | O trecho da requisição que casou e onde estava (ex.: `ARGS:q`). |
| `attackData.clientIP` | IP do cliente. Desde 30/abr/2026 é ele que define a geo (o `X-Forwarded-For` deixou de ser usado). |
| `geo` | Continente, país, cidade, região e ASN do cliente. |
| `httpMessage` | Método, host, path, query, status, bytes, TLS e os headers de requisição e resposta. |
| `botData` | Pontuação de bot e segmento de resposta (Bot Manager). |
| `userRiskData` | Risco e confiança do usuário no login (Account Protector, só em endpoint protegido). |
| `clientData` | Dados do SDK móvel (Bot Manager Premier). |

Os campos de regra chegam da Akamai **codificados** (URL-encoded e base64, um item por regra separado por `;`). O CentralOps os decodifica na coleta e acrescenta `attackData.decoded_rules` — uma lista com `id`, `version`, `message`, `tag`, `data`, `selector` e `action` de cada regra. Os campos originais continuam no `raw`.

## Segurança da integração

- **Host travado.** O host da credencial só é aceito sob `akamaiapis.net`, em HTTPS, sem porta nem caminho. Redirecionamento nunca é seguido: a assinatura do pedido vale para a credencial do cliente.
- **Credencial num campo secreto só.** As quatro partes do EdgeGrid ficam juntas no campo secreto, criptografado como os demais segredos do CentralOps, e podem ser trocadas editando a integração.
- **Erro sem eco.** Quando a Akamai recusa um pedido, o CentralOps registra só o `title` do erro (`application/problem+json`), nunca o corpo inteiro nem o header de autorização.
- **Só leitura de fato.** A Akamai exige o serviço **SIEM API** em nível **READ-WRITE** para ler eventos, mas a API não tem operação de escrita: o CentralOps só faz `GET`.

## Pré-requisitos

- Um produto de segurança da Akamai com integração SIEM: **App & API Protector**, **Kona Site Defender** (ou os demais produtos listados na documentação da SIEM Integration).
- Acesso ao **Akamai Control Center** com permissão para editar a configuração de segurança e criar API clients.
- Saída HTTPS (443) do CentralOps para `*.akamaiapis.net`. Se houver proxy, ele não pode alterar os headers `Host` e `Authorization`.

## Passo 1: Ligar a coleta para SIEM na configuração de segurança

Para **cada** configuração de segurança que você quer coletar:

1. No Control Center, abra **Security Configurations** e a configuração.
2. Em **Advanced Settings**, ative **Data collection for SIEM Integrations**. Escolha as políticas (ou todas) e, se quiser, inclua os eventos de Bot Manager e Account Protector.
3. **Ative a versão em produção.** Enquanto a versão com a coleta ligada não estiver ativa em produção, a API não devolve evento.
4. Anote o **ID da configuração** (número que aparece no Control Center).

## Passo 2: Criar o API client

1. Em **Identity & Access Management → API clients**, crie um API client. A Akamai recomenda um **service account** cujo único papel seja **Manage SIEM**.
2. Dê ao client o serviço **SIEM API** com acesso **READ-WRITE** (é o nível que a Akamai exige para ler os eventos).
3. Gere a credencial e baixe o arquivo **.edgerc**. Ele tem quatro linhas: `host`, `client_token`, `client_secret` e `access_token`.

## Passo 3: Criar a integração no CentralOps

1. No menu lateral, vá em **Coleta -> Integrações** e adicione uma integração **Akamai App & API Protector (WAF)**.
2. Preencha:

   | Campo | O que informar |
   |---|---|
   | IDs das configurações de segurança | Um ou mais IDs do Passo 1, separados por vírgula (ex.: `14227, 14228`). |
   | Credencial EdgeGrid (.edgerc) | O conteúdo da seção do `.edgerc` do Passo 2 (o cabeçalho `[default]` é opcional). Também aceita um JSON com as chaves `host`, `client_token`, `client_secret` e `access_token`. |

3. Clique em **testar a conexão**. O teste faz uma leitura mínima (último minuto, 1 evento) em **cada** configuração e mostra o estado de cada uma:
   - `config 14227: ok`: credencial válida e coleta para SIEM acessível.
   - `sem permissão (...)`: falta o serviço SIEM API em READ-WRITE no API client, ou a configuração não tem *Data collection for SIEM Integrations* ligado e ativado.
   - `configuração não encontrada`: ID errado, ou a configuração não é visível para esse API client.
   - `credencial recusada`: `client_token`, `access_token` ou `client_secret` errados, ou o relógio da máquina está fora do horário (a assinatura aceita 30 s de diferença).
   - `host da credencial inválido`: o `host` do `.edgerc` não é um host da Akamai.

   O teste só passa se **todas** as configurações responderem: a coleta pede todas numa chamada só, e uma configuração sem acesso derruba a chamada inteira.

4. Salve. O stream `siem` roda a cada minuto (o intervalo recomendado pela Akamai).

## O que é coletado

Cada evento chega normalizado em OCSF 1.8 como **HTTP Activity** (classe 4002), com o evento original da Akamai preservado no `raw`.

| OCSF | Origem |
|---|---|
| `activity_id` | Método HTTP: GET → Get, POST → Post, PUT → Put, DELETE → Delete, HEAD, OPTIONS, TRACE, PATCH, CONNECT; outros → Other. |
| `disposition_id` / `action_id` | `deny`, `deny_custom_*`, `tarpit`, `abort` → **Blocked** / Denied; `alert` → **Alert** / Allowed; `monitor` → **Logged** / Allowed; demais ações (ex.: de bot) → Other. |
| `severity_id` | Bloqueado → Low (o ataque parou na borda); `alert`/`monitor` → **Medium** (a requisição chegou à origem). |
| `is_alert` | Sempre `true`: todo evento da SIEM API é uma regra disparada. |
| `firewall_rule` | Primeira regra disparada: `uid` (ID), `name` (mensagem), `version`, `category` (tag), `match_location` (seletor). `match_details` = IDs de **todas** as regras. |
| `src_endpoint` | `ip` = `clientIP`; `location` = país, cidade, continente e região; `autonomous_system.number` = ASN. |
| `http_request` | `http_method`, `version` (protocolo), `user_agent` (do header da requisição), `uid` (= `requestId`) e `url` (`hostname`, `path`, `query_string`, `port`). |
| `http_response.code` | Status devolvido ao cliente. |
| `policy.uid` | Política de segurança que avaliou a requisição. |
| `time` | `httpMessage.start`. |
| `metadata.uid` | `requestId`. `unmapped.akamai_config_id` = configuração de segurança. |
| `ja4_fingerprint_list` | `identity.ja4` (fingerprint TLS do cliente) — o pivô para achar o mesmo bot atrás de IPs diferentes. |
| `actor.user` / `risk_score` | Account Protector: `name` = `userRiskData.username`, `uid` = `originUserId`; `risk_score` = `userRiskData.score`. |
| `unmapped.akamai_rules` | **Todas** as regras disparadas, decodificadas (`id`, `message`, `tag`, `action`) — a de anomalia que decide o `deny` costuma ser a última. |
| `unmapped.akamai_bot_score` / `akamai_client_reputation` | `botData.botScore` (Bot Manager) e `attackData.clientReputation`. |

Campos de produto não licenciado (Account Protector, Bot Manager) não aparecem no evento normalizado — não viram objeto vazio.

O dedupe usa configuração + política + `requestId` + início + o conjunto de regras: a mesma requisição avaliada por duas configurações gera dois eventos distintos.

## Operação e saúde

- **Retenção de 12 horas na Akamai.** A Akamai guarda os eventos por 12 horas. Se o CentralOps ficar parado por mais tempo que isso, o que passou não pode mais ser lido.
- **Offset expirado.** Se a Akamai recusar o offset salvo (HTTP 416), o CentralOps volta a ler por tempo a partir do último evento já coletado, dentro das 12 horas, e o log do worker diz de onde retomou. Se o último evento for mais antigo que a retenção, o log registra **quanto tempo de eventos se perdeu**. A troca acontece uma vez por ciclo; não vira laço.
- **Resposta cortada.** A borda da Akamai encerra a resposta depois de 2 minutos. Sem a linha final de contexto, o CentralOps descarta a página inteira e repete a mesma leitura com metade do limite (até três vezes), como a Akamai orienta. Nada da página cortada é entregue pela metade.
- **Teto por ciclo.** Até 10 páginas de 5.000 eventos por ciclo. No teto o offset da próxima página fica salvo, o próximo ciclo continua dali e **Saúde do Pipeline** marca o stream como em *backlog*.
- **Indicador de atraso.** O atraso exibido é o do evento mais novo já lido (o offset da Akamai é opaco). Em um site sem ataques, ele cresce sem que haja atraso real.
- **Backfill.** Funciona dentro das 12 horas de retenção, por fatias de 15 minutos; a fatia que bate no limite é dividida ao meio até 1 minuto. O backfill não mexe na posição da coleta normal.

## Limites da API da Akamai

| Limite | Valor | Fonte |
|---|---|---|
| Retenção | 12 horas | [SIEM para Splunk e CEF](https://techdocs.akamai.com/siem-integration/docs/akamai-siem-integration-for-splunk-and-cef-syslog) |
| Latência | 5 segundos (`to` não pode passar de agora − 5 s) | [Combinações de parâmetros](https://techdocs.akamai.com/siem-integration/reference/query-parameter-combinations) |
| Taxa | Sem limite imposto hoje; a Akamai pede no máximo 1 chamada por segundo e recomenda 60 s entre coletas | [Rate limits](https://techdocs.akamai.com/siem-integration/reference/rate-limits) |
| Tamanho da página | `limit` padrão 10.000, máximo 600.000 (aproximado) | [Get security events](https://techdocs.akamai.com/siem-integration/reference/get-config) |
| Tempo de resposta | A borda corta a resposta depois de 2 minutos | [Get started](https://techdocs.akamai.com/siem-integration/reference/api-get-started) |
| Offset expirado | HTTP 416 | [Get security events](https://techdocs.akamai.com/siem-integration/reference/get-config) |

## Custo e licenciamento

- A Akamai **não publica preço** da SIEM Integration API e a documentação dela não cita cobrança à parte pelo uso da API. A integração SIEM é uma funcionalidade dos produtos de segurança (App & API Protector, Kona Site Defender e afins); confirme no seu contrato com o time de contas da Akamai.
- `botData`, `clientData` e `userRiskData` só aparecem com **Bot Manager** (Premier, para `clientData`) e **Account Protector** licenciados.
- Do lado do CentralOps, o volume é o de eventos de segurança, não o de tráfego: só requisições que dispararam regra viram evento.

## Solução de problemas

**O teste passa, mas nada chega.** Confira se a versão da configuração com *Data collection for SIEM Integrations* está **ativa em produção** (Passo 1). Sem ataque nem regra disparada no período, a API devolve zero eventos — é normal num site com pouco tráfego.

**`sem permissão` em uma configuração.** O API client não tem o serviço SIEM API em READ-WRITE, ou a configuração pertence a outro contrato/grupo que o client não enxerga.

**`credencial recusada` depois de funcionar.** A credencial do API client foi rotacionada ou expirou no Control Center. Gere uma nova e cole o `.edgerc` novo no campo **Credencial EdgeGrid**. Se o erro citar horário, ajuste o NTP da máquina do CentralOps.

**Log do worker: `offset expirado ... não podem mais ser lidos`.** O CentralOps ficou parado por mais de 12 horas. Os eventos anteriores à retenção da Akamai não existem mais na API.

**Log do worker: `resposta incompleta — repetindo com limit=...`.** A resposta foi cortada pela borda (volume alto ou rede lenta). Se acontecer sempre, há muito evento por minuto: confira o volume em **Saúde do Pipeline**.

## Limitações conhecidas

- Só a SIEM Integration API (eventos de segurança). Logs de tráfego completos (DataStream 2) não são coletados.
- O WAF só registra requisições que dispararam regra: tráfego legítimo não aparece.
- `firewall_rule` traz a **primeira** regra disparada (o OCSF tem um objeto só); a lista completa, com mensagem e ação de cada uma, vai em `unmapped.akamai_rules`.
- O teste de conexão e a coleta exigem que **todas** as configurações informadas estejam acessíveis.
