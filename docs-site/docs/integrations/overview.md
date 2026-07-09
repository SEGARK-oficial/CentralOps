---
sidebar_position: 1
title: Visão geral de integrações
description: O que são integrações no CentralOps, quais plataformas são suportadas e o que acontece depois de conectar uma.
---

# Visão geral de integrações

Uma **integração** conecta o CentralOps a uma plataforma de segurança para coletar eventos automaticamente, normalizá-los em um formato único e encaminhá-los para um ou mais destinos. Assim você reúne dados de vendors diferentes em um só lugar, sem ficar preso a um único SIEM.

## Quando usar

- **Centralizar alertas de EDR/XDR.** Você usa Sophos Central, Microsoft Defender ou CrowdStrike para proteção de endpoints e quer que as detecções cheguem ao mesmo lugar onde investiga o resto do ambiente.
- **Trazer eventos de identidade e auditoria de nuvem.** Você coleta logs de sign-in (Entra ID, Okta) e eventos de API (AWS CloudTrail) para correlacionar com os alertas de segurança.
- **Trazer alertas do seu SIEM Wazuh para análise unificada.** Você quer puxar alertas do Wazuh por severidade ou origem e cruzá-los com os demais eventos coletados.
- **Enviar o mesmo evento para vários lugares.** Você precisa que um evento normalizado chegue completo ao seu data lake (para retenção) e reduzido ao SIEM (para detecção), sem duplicar o trabalho de coleta.

## Plataformas de coleta suportadas

Estas são as plataformas das quais o CentralOps coleta eventos hoje:

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
| Fortinet FortiGate | Rede / Firewall | Logs de tráfego/UTM via syslog (fonte **push**) |
| Windows Event Log | Endpoint / OS | Eventos de segurança via WEC/WEF (fonte **push**) |

As fontes marcadas como **push** não têm API de coleta — elas *empurram* os dados para o CentralOps por um edge-collector (Vector/Fluent Bit). A configuração é diferente das demais: veja [Ingestão push](./push-ingestion.md).

Para configurar qualquer uma delas, abra o menu **Visão geral -> Integrações**, clique em "Criar integração" e escolha o vendor desejado. Você encontrará os guias de cada um na seção [Vendors suportados](./adding-new-vendor.md). Várias têm páginas próprias de configuração:

- [Integração Sophos](./sophos.md)
- [Integração Wazuh](./wazuh.md)
- [Integração CrowdStrike](./crowdstrike.md)
- [Integração Microsoft Defender](./microsoft-defender.md)
- [Ingestão push (FortiGate, Windows Event Log)](./push-ingestion.md)

## Para onde os eventos vão (destinos)

Depois de coletados e normalizados, os eventos são encaminhados para um ou mais **destinos**. O CentralOps já vem com suporte para:

| Destino | Para que serve |
| --- | --- |
| Syslog (RFC 3164 e 5424) | Encaminhar eventos para SIEMs e coletores que recebem syslog. |
| JSONL (arquivo) | Gerar arquivos de eventos linha a linha, para arquivamento ou ingestão posterior. |
| Splunk HEC | Enviar eventos para o Splunk. |
| Elasticsearch / OpenSearch | Indexar eventos no Elasticsearch ou OpenSearch (API `_bulk`). |
| ClickHouse | Inserir eventos num banco analítico colunar de alto volume. |
| CrowdStrike Falcon Next-Gen SIEM | Encaminhar eventos ao Falcon NG-SIEM (conector HEC). |
| CrowdStrike Falcon LogScale | Encaminhar eventos ao Falcon LogScale (ex-Humio). |
| Amazon S3 | Armazenar eventos em um bucket de objeto (data lake). |
| Microsoft Sentinel | Encaminhar eventos para o SIEM da Microsoft. |
| Kafka | Publicar eventos em um tópico Kafka. |
| OTLP | Exportar eventos no padrão OpenTelemetry. |
| Webhook genérico | Enviar eventos via HTTP POST para qualquer endpoint (SOAR, automação, integrações ad-hoc). |
| Datadog (Logs) | Encaminhar eventos para o Datadog Logs (observabilidade). |
| Google SecOps (Chronicle) | Enviar eventos para o SIEM do Google (Chronicle/SecOps). |
| Amazon Security Lake | Gravar eventos em Parquet OCSF no data lake de segurança da AWS. |

A criação e a manutenção dos destinos ficam disponíveis para o administrador no menu **Operação -> Destinos**. Para detalhes de cada destino, veja [Destinos](../outputs/destinations.md).

## Capacidades de consulta (Query)

Algumas plataformas de coleta e dados permitem que você execute buscas em tempo real ou histórico sem precisar coletar tudo de antemão. Cada vendor tem um dialeto próprio:

| Vendor | Dialeto | O que permite |
| --- | --- | --- |
| CrowdStrike Falcon | FQL | Buscar alertas ao vivo usando a sintaxe de query do CrowdStrike (Falcon Query Language) |
| Microsoft Defender | KQL | Advanced hunting — consultar eventos de segurança com Kusto Query Language (KQL) |
| Sophos (XDR) | XDR Data Lake | Consultar o data lake histórico (últimos 30 dias) com filtros estruturados |
| Wazuh | Opensearch DSL | Buscar alertas no índice do Wazuh usando Opensearch Query DSL |
| Lake (S3) | Lake Filter | Consultar eventos já armazenados no data lake de segurança (S3 ou AWS Security Lake) com filtros estruturados |

**Lake não é uma plataforma de coleta** — é um destino consultável. Se você configurou o CentralOps para armazenar eventos em S3 ou AWS Security Lake (via destino), pode depois buscá-los no lugar onde estão, sem reingerir.

Para rodar uma busca federada, abra **Operação -> Busca federada**. Cada integração com capacidade de query mostra os dialetos suportados. Para ver o catálogo completo de dialetos e janelas de tempo, use **Integração -> Capacidades de consulta** ou navegue até a página da integração específica:

- [Integração CrowdStrike](./crowdstrike.md)
- [Integração Microsoft Defender](./microsoft-defender.md)

## Como os eventos são distribuídos

O administrador define **rotas** que decidem para quais destinos cada evento vai:

- **Condições e prioridade**: cada rota tem uma condição e uma prioridade. O evento segue a primeira rota cuja condição corresponde.
- **Envio simultâneo a vários destinos**: uma mesma rota pode entregar o evento a mais de um destino ao mesmo tempo.
- **Remoção de dados pessoais (PII) por rota**: o mesmo evento pode chegar completo a um destino e com os dados pessoais removidos em outro.
- **Migração gradual entre destinos**: dá para liberar uma rota aos poucos para fazer um corte controlado de um SIEM para outro.

A configuração de rotas fica no menu **Operação -> Roteamento** (somente administrador). Para detalhes, veja [Roteamento](../outputs/routing.md) e [Remoção de PII](../outputs/pii-redaction.md).

## O que acontece depois de criar uma integração

Você não precisa acompanhar nada manualmente — o CentralOps cuida da coleta em segundo plano. O fluxo, do ponto de vista de quem opera, é este:

1. **Você cria a integração e informa as credenciais.** O CentralOps testa a conexão na hora. Se as credenciais estiverem corretas, a integração fica **ativa**; se houver erro, ela fica em estado de erro e mostra a mensagem do que falhou.
2. **A coleta começa automaticamente.** A partir daí, o CentralOps puxa novos eventos em intervalos regulares (tipicamente a cada poucos minutos), sem você precisar acionar nada. Eventos repetidos são descartados, então o mesmo alerta não aparece duas vezes.
3. **Os eventos são normalizados.** Cada evento coletado é convertido para o formato padrão do CentralOps. Se algum evento não puder ser convertido (por exemplo, um formato inesperado), ele vai para a **Quarentena**, onde pode ser revisado e reprocessado.
4. **Os eventos são entregues aos destinos.** Conforme as rotas definidas, cada evento é enviado para um ou mais destinos, com ou sem remoção de dados pessoais.
5. **Você acompanha o resultado na interface.** Os eventos passam a aparecer em **Operação -> Alertas** e na tela de busca em **Operação -> Investigações**. A saúde da coleta fica visível na própria tela de Integrações.

Para acompanhar a etapa de normalização e a entrega, use também **Normalização -> Saúde do Pipeline** e **Normalização -> Quarentena**.

## Acompanhar a saúde de uma integração

Cada integração de coleta mostra um indicador de saúde:

| Indicador | Significado |
|-----------|-------------|
| Verde | Coleta ativa, última execução recente, sem erros. |
| Amarelo | Coleta mais lenta ou erros esporádicos (geralmente limite de requisições do vendor). |
| Vermelho | Coleta parada há vários minutos ou erro crítico (por exemplo, credencial inválida). |

Para ver o indicador e o último erro, abra o menu **Visão geral -> Integrações** e clique na integração.

## O que cada perfil pode fazer

| Ação | Quem pode |
|------|-----------|
| Ver a lista de integrações, a saúde e a última coleta | Todos os perfis |
| Pausar e retomar a coleta, testar a conexão | Operador ou superior |
| Editar mapeamentos e reprocessar eventos | Engenheiro ou superior |
| Criar e editar destinos e rotas | Administrador |
| Criar integração, editar credenciais, excluir integração e ajustar o intervalo de coleta | Administrador |

## Sobre as credenciais

As credenciais que você informa são guardadas de forma segura:

- São **criptografadas** quando armazenadas.
- **Nunca são exibidas de volta** na interface.
- Quando a plataforma usa tokens que expiram, o CentralOps os renova sozinho.

Para trocar uma credencial (por exemplo, um novo segredo do Sophos) sem interromper a coleta:

1. Abra o menu **Visão geral -> Integrações** e clique na integração.
2. Use a opção de editar credenciais.
3. Informe o novo segredo e salve.
4. O CentralOps valida e substitui a credencial se ela estiver correta.

## Quando uma integração fica vermelha

1. Clique na integração e veja a mensagem do último erro.
2. Use o botão de testar a conexão para confirmar se o problema persiste.
3. Se a causa for credencial expirada, atualize-a pela opção de editar credenciais.
4. Se for limite de requisições do vendor, aumente o intervalo de coleta.
5. Se o vendor estiver indisponível, aguarde o restabelecimento ou acione o suporte do vendor.

## Próximos passos

- **Conectar um vendor?** Abra **Visão geral -> Integrações** e clique em "Criar integração". Todos os vendors suportados aparecem ali.
- **Ver o guia de configuração do Sophos?** Veja [Integração Sophos](./sophos.md).
- **Ver o guia de configuração do Wazuh?** Veja [Integração Wazuh](./wazuh.md).
- **Entender os destinos?** Veja [Destinos](../outputs/destinations.md).
- **Configurar o roteamento de eventos?** Veja [Roteamento](../outputs/routing.md).
- **Solicitar um novo vendor ou destino?** Veja [Vendors suportados e como solicitar um novo](./adding-new-vendor.md).
