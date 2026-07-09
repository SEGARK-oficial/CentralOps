---
sidebar_position: 100
title: Vendors suportados e como solicitar um novo
description: Veja quais vendors e destinos a plataforma já suporta e como pedir a inclusão de um novo.
---

# Vendors suportados e como solicitar um novo

No CentralOps, um **vendor** é a origem dos seus dados (a plataforma de onde os eventos são coletados, como um EDR, firewall ou serviço de nuvem) e um **destino** é para onde os eventos normalizados são entregues (um SIEM, data lake ou ferramenta de análise). Esta página mostra o que já está disponível na plataforma e como pedir a inclusão de algo novo.

Os vendors e destinos são adicionados pela **equipe da plataforma**. Você não precisa programar nem mexer em servidores: tudo o que está disponível aparece automaticamente nas telas de criação, prontos para você conectar pela interface.

## Quando usar

- **Onboarding de um novo cliente ou ambiente**: você precisa saber se o EDR/firewall do cliente já é suportado antes de prometer a coleta — confira a lista de vendors disponíveis nesta página ou direto na tela de criação de integração.
- **Encaminhar eventos para uma nova ferramenta**: o SOC decidiu enviar os alertas normalizados para um data lake ou um SIEM adicional — verifique se o destino já existe e, se não existir, solicite a inclusão.
- **Avaliar a plataforma**: durante uma POC, você quer confirmar de antemão quais origens e destinos estão cobertos para dimensionar o trabalho de integração.

## Como verificar o que já está disponível

A lista viva e sempre atualizada está na própria interface — ela reflete exatamente o que a sua instância suporta:

| O que você quer ver | Onde olhar na interface |
| --- | --- |
| Vendors de **entrada** (origens de coleta) | Menu **Visão geral -> Integrações**, ao criar uma nova integração: a lista de plataformas disponíveis aparece para escolha. |
| **Destinos** de saída (para onde os eventos vão) | Menu **Operação -> Destinos** (apenas administradores), ao criar um novo destino: os tipos disponíveis aparecem para escolha. |

Se um vendor ou destino aparece nessas telas, ele está pronto para uso — basta configurá-lo seguindo o guia da integração correspondente.

## Vendors de entrada já suportados

Plataformas de origem que a equipe já integrou. A lista pode crescer; confira sempre a tela **Visão geral -> Integrações** para o que está disponível na sua instância.

| Vendor | Categoria | O que coleta |
| --- | --- | --- |
| Sophos (Central / MDR) | EDR / XDR | Alertas, casos e detecções (inclui modo Partner/MSSP) |
| Microsoft Defender | EDR / XDR | Incidentes e alertas (Graph Security API) |
| CrowdStrike Falcon | EDR / XDR | Detecções/alertas (Alerts API v2) |
| NinjaOne | RMM | Atividades e alertas de endpoints |
| Microsoft Entra ID | Identity | Sign-in logs e directory audit (Microsoft Graph) |
| Okta | Identity | System Log (logins, MFA, lifecycle de identidade) |
| AWS CloudTrail | Cloud-audit | Eventos de API/management (polling do bucket S3) |
| Wazuh | SIEM | Detecções do Indexer (`wazuh-alerts-*`) |

Para conectar qualquer um deles, siga o guia [Visão geral de integrações](./overview.md), que explica o passo a passo na interface.

## Destinos de saída já suportados

Para onde o CentralOps entrega os eventos normalizados. Um mesmo evento pode ir para vários destinos ao mesmo tempo (envio simultâneo a vários destinos), conforme as regras definidas em **Operação -> Roteamento**.

| Destino | Para que serve |
| --- | --- |
| Syslog (RFC 3164 e 5424) | Encaminhar eventos para SIEMs e coletores que recebem syslog. |
| JSONL (arquivo) | Gerar arquivos de eventos linha a linha, para arquivamento ou ingestão posterior. |
| Splunk HEC | Enviar eventos para o Splunk. |
| Elastic (Bulk) | Indexar eventos no Elasticsearch. |
| Amazon S3 | Armazenar eventos em um bucket de objeto (data lake). |
| Microsoft Sentinel | Encaminhar eventos para o SIEM da Microsoft. |
| Kafka | Publicar eventos em um tópico Kafka. |
| OTLP | Exportar eventos no padrão OpenTelemetry. |
| Webhook genérico | Enviar eventos via HTTP POST para qualquer endpoint (SOAR, automação, integrações ad-hoc). |
| Datadog (Logs) | Encaminhar eventos para o Datadog Logs (observabilidade). |
| Google SecOps (Chronicle) | Enviar eventos para o SIEM do Google (Chronicle/SecOps). |
| Amazon Security Lake | Gravar eventos em Parquet OCSF no data lake de segurança da AWS. |

A configuração de cada destino é feita pela interface, em **Operação -> Destinos** (apenas administradores). Depois de criar o destino, você o utiliza nas regras de **Operação -> Roteamento**. Veja o guia de [Roteamento](../outputs/routing.md) para definir quem recebe quais eventos.

## Como solicitar um novo vendor ou destino

Se a origem ou o destino que você precisa **não aparece** nas telas acima, a inclusão é feita pela equipe da plataforma. Você não consegue (e não precisa) adicioná-la por conta própria.

Para acelerar o atendimento, ao abrir o pedido informe:

1. **Nome do produto/serviço** e fabricante (por exemplo, "EDR da fabricante X", "SIEM Y").
2. **Tipo**: é uma **origem** (de onde queremos coletar eventos) ou um **destino** (para onde queremos enviar eventos)?
3. **Tipos de evento** que importam (alertas, detecções, logs de autenticação, etc.).
4. **Como a plataforma disponibiliza os dados**: API, syslog, exportação para um bucket, etc., se você souber.
5. **Como você obtém credenciais/acesso** nessa plataforma (chave de API, token, conta de serviço).
6. **Volume e urgência** estimados, para ajudar a priorizar.

Envie essas informações pelo canal de suporte combinado com a sua organização (chamado de suporte ou contato direto com o administrador da plataforma). A equipe avalia a viabilidade, implementa a integração e, quando estiver pronta, ela passa a aparecer automaticamente nas telas de criação — sem nenhuma ação adicional sua.

## Perguntas frequentes

**Preciso instalar algo ou rodar comandos para um novo vendor aparecer?**
Não. Toda a habilitação é feita pela equipe da plataforma no momento da implantação. Quando um vendor ou destino fica disponível, ele aparece sozinho nas telas **Visão geral -> Integrações** e **Operação -> Destinos**.

**Posso definir como o destino se conecta (porta, TLS, tamanho de lote)?**
Os parâmetros que você pode ajustar aparecem no formulário de criação do destino, em **Operação -> Destinos**. Os ajustes de infraestrutura mais profundos são definidos pela equipe de infraestrutura no momento do deploy. Se precisar alterá-los, fale com o administrador da plataforma.

**Um destino instável vai derrubar a entrega para os outros?**
Não. A plataforma tem proteção contra destino instável: se um destino fica indisponível, os eventos ficam guardados em uma fila de reenvio e os demais destinos continuam recebendo normalmente. Você acompanha o estado da entrega em **Normalização -> Saúde do Pipeline**.

## Onde continuar

- [Visão geral de integrações](./overview.md) — como conectar um vendor já suportado.
- [Roteamento](../outputs/routing.md) — definir quais destinos recebem quais eventos.
- Telas de operação: **Visão geral -> Integrações**, **Operação -> Destinos** e **Operação -> Roteamento** (as duas últimas apenas para administradores).
