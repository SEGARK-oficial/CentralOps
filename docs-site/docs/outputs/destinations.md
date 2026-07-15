---
sidebar_position: 2
title: Destinos (catálogo e como configurar)
description: Para onde o CentralOps envia seus eventos — tipos de destino disponíveis, quando usar cada um, como criar, testar, monitorar e trocar credenciais pela interface.
---

# Destinos

Um **Destino** é um lugar para onde o CentralOps envia seus eventos já normalizados: um SIEM (Splunk, Sentinel, Elasticsearch), um barramento de mensagens (Kafka), um data lake (S3), um servidor Syslog ou um arquivo local. Cada destino é independente — você pode criar, testar a conexão, acompanhar a saúde, trocar a credencial e reprocessar eventos que falharam, tudo pela interface.

Destinos é uma tela de administrador. Você a encontra no menu **Operação → Destinos**.

---

## Quando usar

- **Espelhar seus alertas no SIEM que sua equipe já opera.** Se o SOC vive dentro do Splunk ou do Microsoft Sentinel, crie um destino para esse SIEM e os eventos normalizados passam a chegar lá automaticamente, no formato que a ferramenta espera.
- **Guardar tudo a longo prazo para auditoria e conformidade.** Crie um destino S3 (ou compatível) para arquivar o histórico completo de eventos de forma barata, atendendo a requisitos de retenção e investigação retroativa.
- **Distribuir o mesmo evento para vários times ao mesmo tempo.** Você pode ter vários destinos ativos — por exemplo, Splunk para o SOC, Kafka para o time de dados e S3 para o arquivo — e o CentralOps faz o envio simultâneo a todos eles.

---

## Como escolher o destino certo

Use esta tabela para decidir qual tipo de destino criar:

| Você quer... | Use o tipo |
|--------------|-----------|
| Enviar para o Splunk que já é seu SIEM principal | **Splunk HEC** |
| Enviar para Microsoft Sentinel / Defender / Azure | **Microsoft Sentinel** |
| Enviar para Elasticsearch ou OpenSearch | **Elasticsearch / OpenSearch** |
| Inserir num banco analítico colunar de alto volume | **ClickHouse** |
| Encaminhar para o CrowdStrike Falcon Next-Gen SIEM | **CrowdStrike Falcon Next-Gen SIEM** |
| Buscar logs em escala de longo prazo no CrowdStrike LogScale | **CrowdStrike Falcon LogScale** |
| Arquivar a longo prazo para conformidade e investigação histórica | **Amazon S3** (ou compatível: MinIO, R2, Ceph) |
| Publicar os eventos num barramento para outros times consumirem | **Kafka** |
| Mandar para uma stack de observabilidade (OpenTelemetry) | **OpenTelemetry (OTLP)** |
| Encaminhar para um SIEM legado ou para o Wazuh Manager via Syslog | **Syslog RFC 3164** ou **Syslog RFC 5424** |
| Gerar um arquivo local para teste, backup ou staging | **Arquivo JSONL** |
| Integrar com SOAR, automação ou webhooks customizados | **Webhook genérico** |
| Centralizar logs de segurança na observabilidade Datadog | **Datadog (Logs)** |
| Enviar para o SIEM do Google (Chronicle) | **Google SecOps (Chronicle)** |
| Arquivar em formato OCSF padronizado no data lake AWS | **Amazon Security Lake** |

Você não precisa escolher só um. É comum ter Splunk para a operação do dia a dia e S3 para retenção, rodando ao mesmo tempo.

---

## Catálogo de destinos disponíveis

Todos os tipos abaixo estão prontos para uso. Ao criar um destino, você escolhe o tipo numa lista e a interface mostra apenas os campos daquele tipo.

### Splunk HEC

Envio de eventos para o Splunk (Enterprise ou Cloud) pelo HTTP Event Collector.

- **Quando usar:** sua equipe já opera o Splunk como SIEM principal.
- **O que você informa:** endereço (URL) do HEC, índice, sourcetype e se a conexão usa TLS.
- **Credencial:** o token do HEC.

### Elasticsearch / OpenSearch

Ingestão de eventos em um cluster Elasticsearch ou OpenSearch.

- **Quando usar:** você pesquisa e monta dashboards no Elastic/OpenSearch (ou Kibana).
- **O que você informa:** endereço do cluster, índice e o tipo de autenticação.
- **Credencial:** a API key (quando a autenticação por chave estiver selecionada).
- **Diferenciais:** evita duplicar eventos em reenvios e permite apagar eventos por critério, atendendo a pedidos de exclusão (LGPD).

### ClickHouse

Inserção de eventos num banco analítico colunar (ClickHouse OSS, ClickHouse Cloud ou compatível) pela interface HTTP.

- **Quando usar:** você indexa um volume alto de eventos para análise/consulta colunar (data lake de SIEM/observabilidade).
- **O que você informa:** URL, banco, tabela, usuário e se quer ignorar campos sem coluna correspondente.
- **Credencial:** a senha do usuário ClickHouse.
- **Detalhes:** veja [Destino: ClickHouse](./destination-clickhouse.md).

### CrowdStrike Falcon Next-Gen SIEM

Encaminhamento de eventos ao Falcon Next-Gen SIEM por um conector HEC.

- **Quando usar:** você opera o Falcon NG-SIEM e quer os eventos normalizados lá para detecção e correlação.
- **O que você informa:** a URL do conector HEC (gerada no console do Falcon) e, opcionalmente, sourcetype/source.
- **Credencial:** o token de ingestão (HEC) do Falcon.
- **Detalhes:** veja [Destino: CrowdStrike Falcon Next-Gen SIEM](./destination-crowdstrike-ngsiem.md).

### CrowdStrike Falcon LogScale

Encaminhamento de eventos ao Falcon LogScale (ex-Humio) por endpoint HEC-compatível.

- **Quando usar:** você usa o LogScale para busca de logs em escala e longo prazo.
- **O que você informa:** a URL de ingestão do LogScale (depende da sua região/cloud) e, opcionalmente, sourcetype/source.
- **Credencial:** o token de ingestão do LogScale.
- **Detalhes:** veja [Destino: CrowdStrike Falcon LogScale](./destination-crowdstrike-logscale.md).

### Amazon S3

Armazenamento de objetos para data lake ou arquivo de longo prazo. Funciona com S3 da AWS e com compatíveis (MinIO, Cloudflare R2, Ceph).

- **Quando usar:** retenção de longo prazo, conformidade e investigação histórica de baixo custo.
- **O que você informa:** nome do bucket, região, prefixo de pasta e se quer compressão.
- **Credencial:** a chave secreta de acesso (ou nenhuma, quando o ambiente usa um papel de acesso da própria nuvem).
- **Diferenciais:** compressão para reduzir custo e exclusão de eventos por critério (LGPD).

### Microsoft Sentinel

Ingestão no Microsoft Sentinel pela API de ingestão de logs.

- **Quando usar:** sua plataforma de segurança é Microsoft Defender / Azure / Sentinel.
- **O que você informa:** o endpoint de ingestão, o identificador da regra de coleta (DCR), o nome do fluxo e os dados do aplicativo do Entra ID (tenant e client).
- **Credencial:** o segredo do aplicativo (client secret).

### Kafka

Publicação dos eventos em um tópico Apache Kafka.

- **Quando usar:** outros times consomem os eventos a partir de um barramento de mensagens.
- **O que você informa:** os servidores de bootstrap, o tópico, o protocolo de segurança e o usuário.
- **Credencial:** a senha SASL (quando a autenticação SASL estiver ativada).

### OpenTelemetry (OTLP)

Envio dos eventos como logs no padrão OpenTelemetry (OTLP/HTTP) para stacks de observabilidade.

- **Quando usar:** você centraliza logs e métricas numa stack de observabilidade (OpenTelemetry Collector e similares).
- **O que você informa:** o endpoint do coletor e, opcionalmente, cabeçalhos e atributos de recurso.
- **Credencial:** normalmente nenhuma (a autenticação, quando existe, vai nos cabeçalhos).

### Syslog RFC 3164

Encaminhamento via Syslog no formato clássico (BSD), com o conteúdo em JSON.

- **Quando usar:** integração com Wazuh Manager ou outro receptor Syslog tradicional.
- **O que você informa:** host, porta e se a conexão usa TLS.
- **Credencial:** nenhuma.

### Syslog RFC 5424

Encaminhamento via Syslog no formato moderno, com dados estruturados.

- **Quando usar:** integração com SIEMs como QRadar, ArcSight ou syslog-ng modernos.
- **O que você informa:** host, porta e se a conexão usa TLS.
- **Credencial:** nenhuma.

### Arquivo JSONL

Gravação dos eventos em um arquivo local, uma linha por evento.

- **Quando usar:** teste, backup ou área de preparação (staging).
- **O que você informa:** a pasta onde os arquivos são gravados.
- **Credencial:** nenhuma.

### Webhook genérico

Envio de eventos via POST HTTP para qualquer endpoint (SOAR, automação, integrações ad-hoc).

- **Quando usar:** integração com ferramenta de orquestração, automação ou SOAR; webhooks em plataformas customizadas.
- **O que você informa:** URL do endpoint, método HTTP (POST ou PUT), modo de autenticação (nenhuma, Bearer, ou Basic), formato de empacotamento (array de eventos ou NDJSON), tipo de corpo (envelope padrão ou eventos normalizados), cabeçalhos customizados e se valida certificado TLS.
- **Credencial:** token Bearer ou par de usuário e senha (Basic auth), dependendo do modo escolhido.
- **Diferenciais:** autenticação flexível, múltiplos formatos de empacotamento, suporte a cabeçalhos customizados.

### Datadog (Logs)

Ingestão de eventos no Datadog para observabilidade e análise de segurança.

- **Quando usar:** você já opera observabilidade no Datadog e quer centralizar logs de segurança lá.
- **O que você informa:** site Datadog (datadoghq.com, datadoghq.eu ou endpoint customizado), nome do serviço, identificador de origem (ddsource) e tags para contexto.
- **Credencial:** a chave de API do Datadog (DD-API-KEY).
- **Diferenciais:** integração nativa com dashboards e alertas do Datadog.

### Google SecOps (Chronicle)

Envio de eventos ao SIEM do Google via API de ingestão de logs.

- **Quando usar:** você usa Google SecOps (Chronicle) como SIEM ou plataforma de análise de ameaças.
- **O que você informa:** ID do projeto, identificador de instância, região, localização dos logs, tipo de log e identificador do encaminhador (forwarder).
- **Credencial:** arquivo de conta de serviço JSON (autentica via OAuth2).
- **Diferenciais:** análise de ameaças integrada ao Chronicle, enriquecimento automático.

### Amazon Security Lake

Arquivamento de eventos em formato Parquet OCSF no data lake de segurança da AWS.

- **Quando usar:** retenção centralizada em nível empresarial, investigação histórica e conformidade com padrão OCSF.
- **O que você informa:** nome do bucket S3, ID da conta AWS, região, identificador de origem, compressão (Zstandard ou Snappy).
- **Credencial:** chave de acesso secreta da AWS (ou nenhuma, quando o ambiente usa um papel de acesso da própria instância EC2/ECS).
- **Pré-requisito:** a origem customizada (custom source) precisa estar registrada no Security Lake.
- **Diferenciais:** formato padronizado (OCSF) compatível com análise em ferramentas diversas, sem limite prático de retenção.

---

## Como criar um destino

1. Vá em **Operação → Destinos**.
2. Clique no botão para criar um novo destino.
3. Dê um **nome** que identifique o destino (por exemplo, "Splunk Produção").
4. Escolha o **tipo** na lista — a interface ajusta os campos conforme o tipo selecionado.
5. Preencha os **campos de conexão** que aparecerem (endereço, índice, tópico, bucket etc.).
6. Se o tipo exigir **credencial**, digite-a no campo indicado. A credencial é criptografada ao salvar e **nunca é exibida de volta** — a tela apenas indica que existe uma credencial guardada.
7. Salve.

Ao salvar, o destino aparece na lista. A partir daí você pode testar a conexão e acompanhar a saúde.

### Ajustes de entrega (opcional)

Cada destino tem uma **política de entrega** com valores padrão que funcionam bem na maioria dos casos. Você só precisa mexer nisso se quiser otimizar para um cenário específico:

| Ajuste | Para que serve |
|--------|----------------|
| **Tamanho do lote** | Quantos eventos o CentralOps agrupa antes de enviar. Lotes maiores reduzem o número de requisições; menores reduzem a latência. |
| **Reenvio automático (retry)** | Quantas vezes tentar de novo, com espera crescente, quando o destino falha temporariamente. |
| **Proteção contra destino instável** | Pausa os envios automaticamente quando o destino acumula falhas seguidas e volta sozinho após um tempo, evitando martelar um destino fora do ar. |
| **Modo de simulação (shadow)** | O CentralOps formata e valida os eventos, mas **não envia**. Útil para pré-visualizar antes de ativar de verdade. |

Deixe os campos em branco para usar os padrões do tipo.

:::note
Alguns ajustes mais avançados de processamento (concorrência, limites de fila, tempo limite de envio) são definidos pela equipe de infraestrutura no momento do deploy. Se precisar alterá-los, fale com o administrador da plataforma.
:::

:::caution[Campos ainda sem efeito]
Os campos de **camada de armazenamento** (hot/cold) e **dias de retenção** são informativos nesta versão: você pode preenchê-los para registrar a intenção, mas eles ainda **não executam** mudança de armazenamento nem expiração automática. Trate-os como anotação até que essa função seja liberada.
:::

### Roteamento automático

Ao criar um destino, o CentralOps já o inclui no fluxo de eventos automaticamente — você não precisa configurar nada para começar a receber eventos nele. Se quiser controle fino sobre quais eventos vão para quais destinos, use a tela **Operação → Roteamento** (também só para administrador).

---

## Testar a conexão

Antes de confiar em um destino novo, teste a conexão. Isso valida a credencial e a conectividade **sem** precisar esperar eventos reais.

1. Na lista de **Destinos**, localize o destino.
2. Acione a opção de **testar conexão** ao lado dele.
3. O resultado aparece na hora:
   - **Sucesso:** o destino respondeu e a interface mostra a latência (por exemplo, "conectado, 42 ms").
   - **Falha:** a interface mostra o motivo específico (por exemplo, "tempo esgotado ao conectar").

O teste usa a credencial apenas em memória e não envia eventos.

### Pré-visualizar o formato (simulação)

Se quiser ver **exatamente como o evento sai** para aquele destino antes de ligá-lo, use o **modo de simulação (shadow)** na política de entrega. O CentralOps formata um evento de amostra no formato do destino e mostra o resultado, sem enviar nada de verdade.

---

## Acompanhar a saúde

A tela de **Destinos** mostra a saúde de todos os destinos de uma vez, com eventos por segundo, volume e estado de cada um:

| Estado | O que significa | O que fazer |
|--------|-----------------|-------------|
| **Saudável** | Tudo certo, sem falhas recentes | Nenhuma ação |
| **Degradado** | Funcionando, mas há eventos que não foram aceitos nas últimas 24 h | Abra o destino e revise a fila de reenvio |
| **Instável** | Os envios foram pausados pela proteção contra destino instável (muitas falhas seguidas) | Aguarde a recuperação automática ou revise a credencial e a disponibilidade do destino |
| **Desativado** | O destino está desligado | Reative, se for o caso |
| **Indisponível** | Não foi possível ler a saúde no momento | Tente novamente; se persistir, fale com o administrador da plataforma |

### Fila de reenvio (eventos que não foram aceitos)

Quando um destino rejeita ou não consegue receber um evento, ele vai para a **fila de reenvio** daquele destino. Abrindo o destino, você vê os eventos parados, agrupados pelo motivo da falha, e pode **reprocessá-los** (tentar enviar de novo) direto pela interface.

### Eventos recentes e métricas

No detalhe de cada destino você também encontra:

- **Eventos recentes** que passaram por aquele destino, para conferência rápida.
- **Métricas ao longo do tempo:** eventos por segundo, volume, rejeições e latência média.

---

## Credenciais

A credencial de um destino (token, senha, chave) é sempre **criptografada** e nunca exibida de volta — a tela apenas indica que ela existe.

| Ação | Quando usar | Como fazer |
|------|-------------|------------|
| **Trocar a credencial** | Renovação periódica ou token vencido | No detalhe do destino, informe a nova credencial. O envio passa a usar a nova sem interrupção. |
| **Revogar a credencial** | A credencial foi comprometida | Revogue pela interface. O destino é desativado e para de receber eventos até você cadastrar uma nova credencial. |

Toda leitura, troca ou revogação de credencial fica registrada no histórico do destino, para auditoria.

---

## Histórico e rastreabilidade

- **Histórico de alterações:** cada destino guarda o registro de criação, edição e exclusão. As credenciais nunca aparecem em claro nesse histórico.
- **Rastreabilidade do evento:** você pode conferir por quais destinos um evento específico passou e o resultado da entrega em cada um. Esse rastro fica disponível por um período limitado (alguns dias).

---

## Problemas comuns

| Sintoma | Causa provável | O que fazer |
|---------|----------------|-------------|
| O teste passa, mas os eventos não chegam | Destino desativado ou sem rota | Confira o estado na tela de Destinos; ajuste o roteamento em **Operação → Roteamento** |
| A fila de reenvio cresce e o destino fica "instável" | Credencial vencida ou destino fora do ar | Teste a conexão; troque a credencial se necessário; verifique se o destino está disponível |
| Erro de evento muito grande | O evento passou do tamanho aceito pelo destino | Reduza o tamanho do lote ou ajuste a normalização |
| Erro de autenticação | Token inválido ou revogado | Troque a credencial e confira o histórico do destino |
| Latência alta | O destino está lento ou sobrecarregado | Verifique o desempenho do destino; ajustes finos de concorrência são feitos pela equipe de infraestrutura |
| Eventos duplicados | Reenvio após falha temporária em destinos que garantem "ao menos uma vez" | É esperado; a deduplicação fica a cargo do destino final |

---

## Próximos passos

- **Controlar quais eventos vão para cada destino:** [Roteamento](./routing.md)
- **Ajustar a normalização antes do envio:** [Mapeamento e Normalização](../normalization/overview.md)
- **Ver métricas e rastros de entrega:** [Observabilidade](./observability.md)
