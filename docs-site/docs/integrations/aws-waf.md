---
sidebar_position: 7
title: AWS WAF
description: Colete o log de cada requisição avaliada pelas web ACLs do AWS WAF, lido do S3 ou do CloudWatch Logs
---

# AWS WAF

Conecte o AWS WAF ao CentralOps para coletar o **log de cada requisição avaliada por uma web ACL**: o que o WAF decidiu (liberar, bloquear, CAPTCHA, challenge), qual regra decidiu, o IP e o país do cliente, a URI e o User-Agent. O CentralOps lê o log onde o próprio WAF o entrega: num bucket S3 ou num log group do CloudWatch Logs.

## Quando usar

- **Ver ataques bloqueados na borda.** SQLi, XSS, bots, regras gerenciadas da AWS.
- **Achar o que passou.** Uma regra em modo **COUNT** casou e a requisição chegou à aplicação. Esse é o evento que pede investigação.
- **Rastrear um cliente.** Todas as requisições de um IP ou de uma impressão TLS (JA4) que troca de IP.
- **Afinar regras.** Quais regras bloqueiam tráfego legítimo, por URI e país.

## Como funciona

```
Requisição ─► web ACL (WAF) ─► logging ─► bucket S3 aws-waf-logs-*      ─┐
                                      └─► log group aws-waf-logs-*      ─┴─► CentralOps (a cada 5 min)
```

O WAF não tem API que devolva o log completo; ele só existe depois que o logging da web ACL é ligado. Cada web ACL entrega para **um** destino, cujo nome precisa começar com `aws-waf-logs-`.

| Item | S3 | CloudWatch Logs |
|---|---|---|
| Stream | `events` (tipo `aws_waf.event`) | o mesmo |
| Onde lê | `AWSLogs/<conta>/WAFLogs/<região>/<web-acl>/AAAA/MM/dd/HH/mm/*.log.gz` | o log group informado |
| Formato | um registro JSON por linha, arquivos de 5 minutos | um registro JSON por log event |
| Web ACLs | todas as que entregam no bucket, descobertas sozinhas | todas as que entregam no log group |
| Regiões | todas, ou as que você listar (inclui `cloudfront`) | a do log group |
| Posição da coleta | `LastModified` do último arquivo, com 15 minutos de releitura | horário do último evento lido |
| Teto por ciclo | 500 arquivos | 20.000 eventos |
| Quando prefere | volume alto (mais barato) | volume baixo, ou se o log já está no CloudWatch |

Os dois caminhos entregam o mesmo registro. Um arquivo do S3 que **não dá para ler** (sem `s3:GetObject`, sem `kms:Decrypt`) para o ciclo sem avançar a posição, e o erro aparece em **Saúde do Pipeline**. Um arquivo **corrompido** é pulado e fica registrado em `skipped_objects` no estado da coleta. Uma linha inválida dentro de um arquivo bom é descartada sozinha.

O destino **Firehose** do WAF não é lido.

## Pré-requisitos

- **Logging ligado na web ACL**, para um bucket S3 ou um log group com nome `aws-waf-logs-…`. No console: **WAF & Shield → Web ACLs → sua ACL → Logging and metrics → Enable**.
- **Acesso de leitura** ao destino: o papel cross-account do CentralOps (recomendado) ou uma access key.

## Passo 1: Ligar o logging da web ACL

Se ainda não está ligado, escolha o destino e ligue. Pela CLI, para S3:

```bash
aws s3api create-bucket --bucket aws-waf-logs-minha-empresa --region us-east-1
aws wafv2 put-logging-configuration --logging-configuration \
  ResourceArn=arn:aws:wafv2:us-east-1:111122223333:regional/webacl/minha-acl/ID-DA-ACL,LogDestinationConfigs=arn:aws:s3:::aws-waf-logs-minha-empresa
```

Para ver para onde uma web ACL já entrega:

```bash
aws wafv2 get-logging-configuration \
  --resource-arn arn:aws:wafv2:us-east-1:111122223333:regional/webacl/minha-acl/ID-DA-ACL \
  --query "LoggingConfiguration.LogDestinationConfigs"
```

O valor devolvido é exatamente o que vai no campo **Destino dos logs do WAF** do Passo 3.

:::tip[Corte o volume na origem]

O WAF registra também as requisições liberadas, que são a maioria. Para mandar só o que interessa, use o **filtro de logging** do WAF (manter `BLOCK` e `COUNT`, descartar o resto) e o **redigir campos** para não gravar os headers `Authorization` e `Cookie`.

:::

## Passo 2: Dar acesso de leitura ao CentralOps

Use o mesmo **papel cross-account** do CloudTrail. No formulário da integração, o painel **Acesso por papel AWS** mostra o External ID da organização, o principal do CentralOps e o template CloudFormation. O passo a passo está em [AWS CloudTrail: Opção A](./aws-cloudtrail.md#opção-a-recomendada-papel-cross-account).

Ao criar a stack, informe o bucket do WAF:

```bash
aws cloudformation deploy \
  --template-file centralops-readonly-role.json \
  --stack-name centralops-readonly \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides WafLogsBucketName=aws-waf-logs-minha-empresa
```

O template dá `s3:ListBucket`/`s3:GetObject` no bucket do WAF e leitura de CloudWatch Logs (`logs:FilterLogEvents`). Se o bucket do CloudTrail também for lido, passe os dois parâmetros na mesma stack. Com SSE-KMS, informe também `KmsKeyArn`.

Com **access key** (legado), a policy do usuário precisa de `s3:ListBucket` e `s3:GetObject` no bucket, ou de `logs:FilterLogEvents` no log group.

## Passo 3: Criar a integração no CentralOps

1. Vá em **Coleta -> Integrações** e adicione uma integração **AWS WAF**.
2. Preencha:

   | Campo | O que informar |
   |---|---|
   | ARN do papel ou Access Key ID | O `RoleArn` da stack (ex.: `arn:aws:iam::111122223333:role/CentralOpsReadOnly`) ou o `AccessKeyId`. |
   | Secret Access Key | Só com Access Key ID. |
   | Destino dos logs do WAF | O ARN que o WAF mostra: `arn:aws:s3:::aws-waf-logs-minha-empresa` (ou `s3://aws-waf-logs-minha-empresa/prefixo`) para S3, ou `arn:aws:logs:us-east-1:111122223333:log-group:aws-waf-logs-api` para CloudWatch. Um nome solto é recusado: bucket e log group usam o mesmo prefixo `aws-waf-logs-`. |
   | Regiões AWS | Só para S3. Vazio ou `*` = todas as regiões do bucket, incluindo `cloudfront`. Ou uma lista: `us-east-1, sa-east-1`. |
   | Account ID | A conta dona dos logs. Vazio = a conta do ARN do papel (S3) ou do log group (CloudWatch). |

3. Clique em **testar a conexão**. Com papel, o teste assume o papel. Depois lista o bucket (S3) ou lê 1 evento do log group (CloudWatch).
4. Salve. A coleta começa no próximo ciclo (até 5 minutos).

## O que é coletado

Cada registro vira um evento OCSF 1.8 da classe **HTTP Activity (4002)**, com o registro original no `raw`.

| Ação do WAF | `disposition` (`disposition_id`) | `action` | Severidade | Alerta |
|---|---|---|---|---|
| `ALLOW`, sem regra casada | Allowed (1) | Allowed | Informational | não |
| `ALLOW` com regra em modo COUNT | Count (20) | Allowed | **Medium** | sim |
| `BLOCK` | Blocked (2) | Denied | Low | sim |
| `CAPTCHA` | Captcha (22) | Other | Low | sim |
| `CHALLENGE` | Challenge (23) | Other | Low | sim |

A requisição liberada que casou uma regra em COUNT recebe a severidade mais alta de propósito: ela chegou à aplicação.

| Campo OCSF | Origem no log do WAF |
|---|---|
| `time` | `timestamp` (epoch em ms) |
| `activity_id` / `type_uid` | `httpRequest.httpMethod` (GET = 3, POST = 6, …) |
| `src_endpoint.ip` / `.location.country` | `httpRequest.clientIp` / `httpRequest.country` (`-` = vazio) |
| `http_request.url.hostname`, `dst_endpoint.hostname` | header `Host` (ou `:authority` no HTTP/2) |
| `http_request.url.path` / `.query_string` | `httpRequest.uri` / `httpRequest.args` |
| `http_request.user_agent` | header `User-Agent` |
| `http_request.http_method` / `.version` / `.uid` | `httpMethod` / `httpVersion` / `requestId` |
| `http_response.code` | `responseCodeSent` (resposta customizada do WAF) |
| `firewall_rule.uid` / `.type` | `terminatingRuleId` / `terminatingRuleType` |
| `firewall_rule.match_location` / `.match_details` | `terminatingRuleMatchDetails[0]` (SQLi/XSS) |
| `policy.uid` | `webaclId` |
| `metadata.labels` | `labels[].name` (ex.: `awswaf:managed:aws:bot-control:…`) |
| `ja4_fingerprint_list` | `ja4Fingerprint` (CloudFront e ALB) |
| `cloud.region` / `cloud.account.uid` | caminho no S3 ou ARN do log group |
| `unmapped.aws_waf_count_rules` | `nonTerminatingMatchingRules[].ruleId` |
| `unmapped.aws_waf_group_rule` | a regra de dentro do grupo gerenciado que decidiu |
| `unmapped.aws_waf_attack_type` | `terminatingRuleMatchDetails[0].conditionType` |

Os demais campos (`ruleGroupList`, `rateBasedRuleList`, headers completos) ficam no `raw`. Duplicados são descartados pela combinação web ACL + `requestId` + `timestamp`.

## Solução de problemas

**`Informe o destino de logging da web ACL como ARN…`.** O campo recebeu um nome solto. Use o ARN do Passo 1 ou `s3://bucket`.

**O teste passa, mas nada chega (S3).** Confira o caminho que o WAF usa:

```bash
aws s3 ls s3://aws-waf-logs-minha-empresa/AWSLogs/111122223333/WAFLogs/ --recursive | tail -3
```

Vazio? O logging não está ligado, ou o Account ID não é a conta das web ACLs. Se as pastas são de outra região, confira o campo **Regiões AWS**.

**`waf: falha ao ler s3://… — AccessDenied`.** O papel lista o bucket mas não lê o arquivo: falta `s3:GetObject` ou, com a dica `kms:Decrypt`, permissão na chave KMS. A coleta fica parada nesse ponto, sem perder nada, até a permissão ser corrigida.

**Eventos na quarentena com `unparsedMessage`.** O log group informado tem eventos que não são do WAF. Confira o ARN do log group.

## Limitações conhecidas

- **Firehose não é lido.** Entrega via Kinesis Data Firehose tem outro formato de caminho.
- **Uma conta por integração no S3.** Um bucket central com logs de várias contas precisa de uma integração por conta.
- **Sem descoberta pela API do WAF.** As web ACLs são descobertas pelas pastas do bucket, não por `wafv2:ListWebACLs`.
- **Volume.** Web ACL com muito tráfego liberado passa do teto de 500 arquivos por ciclo. Filtre na origem (veja o Passo 1).
