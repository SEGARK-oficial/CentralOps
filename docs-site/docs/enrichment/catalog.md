---
sidebar_position: 3
title: Catálogo de fontes
description: O que cada fonte de enriquecimento faz, que campos devolve, e o que ela exige para funcionar
---

# Catálogo de fontes

Esta página descreve as cinco fontes de enriquecimento disponíveis hoje. A lista **não é fixa no código do console**, a tela em **Enriquece → Enrichment → Catalog** sempre reflete exatamente o que a sua instalação tem registrado, então use `GET /collectors/enrichment/enrichers` (ou a própria tela) como fonte da verdade se este texto ficar desatualizado.

## Tabela do cliente (chave exata), `table_exact`

| | |
|---|---|
| Modo | por evento |
| Egresso | nenhum |
| Tipos de chave | `ip`, `domain`, `url`, `file_hash`, `cve`, `mac`, `user`, `container_id` |
| Requer configuração externa? | Não |

Casa a chave do evento contra a **sua própria tabela**, por igualdade exata. É o enricher genérico: como você define os campos da tabela livremente, ele serve para qualquer contexto que você já tenha em planilha ou export de outro sistema, usuário → departamento, hostname → dono, hash → veredito interno, CVE → prioridade de patch da sua empresa.

Os campos que ele devolve são exatamente os que você colocou em cada linha da tabela, não há uma lista fixa.

## Tabela do cliente (CIDR, prefixo mais específico), `table_cidr`

| | |
|---|---|
| Modo | por evento |
| Egresso | nenhum |
| Tipos de chave | `ip` |
| Requer configuração externa? | Não |

Casa um IP contra a sua tabela de faixas de rede (CIDR), sempre pelo prefixo **mais específico**. Se a tabela tem `10.0.0.0/16` e `10.0.5.0/24`, um evento com IP `10.0.5.7` recebe o resultado do `/24`, não o do `/16`. É a fonte certa para plano de endereçamento corporativo, inventário de rede exportado de uma ferramenta de gestão, ou listas de bloqueio distribuídas em CIDR.

Veja o passo a passo completo em [Como enriquecer um evento](./how-to-enrich.md).

## TAXII 2.1, `taxii`

:::tip[Um conector, qualquer plataforma de threat intel]
TAXII 2.1 é padrão OASIS, e MISP, OpenCTI, Anomali, ThreatConnect e EclecticIQ falam todos ele. Se a sua plataforma expõe uma coleção TAXII, este enricher a consome sem precisar de conector específico. Suporte a STIX/TAXII 2.1 é critério de avaliação padrão de TIP no mercado.
:::

| | |
|---|---|
| Modo | por evento |
| Egresso | interno (buscamos a lista; nada do seu ambiente sai) |
| Tipos de chave | `ip`, `domain`, `url`, `file_hash`, `mac` |
| Requer configuração externa? | Sim, URL do servidor, id da coleção e credencial |

Baixa periodicamente uma coleção TAXII e a materializa como tabela local. Por rodar **por evento**, alimenta as regras de detecção em pipeline.

Quando usar este em vez do conector do OpenCTI: sempre que a plataforma não for OpenCTI, ou quando você quiser evitar acoplamento à API interna dele. O conector do OpenCTI fala GraphQL, que é a API deles e mudou de schema entre 5.x e 6.x. Em compensação, ele traz campos próprios (score do OpenCTI, marcações TLP resolvidas) que o TAXII entrega de forma menos completa, porque as dependências dentro do bundle nem sempre vêm resolvidas.

**Campos que devolve:**

| Campo | Descrição |
|---|---|
| `kind` | Tipo de chave normalizado |
| `stix_id` | Id do Indicator STIX |
| `indicator_name` | Nome do indicador |
| `confidence` | Confiança 0 a 100 |
| `valid_from` / `valid_until` | Janela de validade |
| `kill_chain_phases` | Fases, ex.: `command-and-control` |
| `labels` | Rótulos STIX, ex.: `malicious-activity` |
| `has_markings` | Há marcação (TLP) a respeitar |
| `created` / `modified` | Datas do objeto STIX |
| `source` | Sempre `"taxii"` |

**Configuração:**

| Campo | Obrigatório | O que é |
|---|---|---|
| `url` | Sim | Só o endereço base, ex.: `https://tip.exemplo` |
| `api_root` | Não (padrão `/taxii2/`) | Caminho da api-root. Varia por plataforma (`/taxii2/`, `/api/v21/`) |
| `collection` | Sim | Id da coleção a consumir |
| `auth_mode` | Não (padrão `bearer`) | `bearer`, `basic` ou `none`. O padrão OASIS sugere `basic`; plataformas comerciais costumam usar `bearer` |
| `username` | Só para `basic` | O usuário. A senha é a credencial da fonte |
| `min_confidence` | Não (padrão 0) | Piso de confiança do indicador |
| `page_size` / `max_pages` | Não | Paginação e teto por carga |

:::note[Por que `url` e `api_root` são campos separados]
O guard de egresso do projeto recusa URL com caminho, de propósito, e é ele que aplica a allowlist de host e CIDR. Como a api-root do TAXII sempre tem caminho, juntar os dois exigiria afrouxar o guard justamente no campo que viaja com a credencial no header `Authorization`. Separados, a base passa pela allowlist e o caminho é validado à parte.
:::

**O que é descartado na carga**, e por quê:

- Indicador **revogado** ou **fora da validade**. Intel vencida é a maior fonte de falso positivo num feed, e filtrar aqui evita depender de alguém lembrar de escrever a condição em toda regra nova.
- Indicador **abaixo do `min_confidence`**.
- Padrão STIX **composto** (`AND`/`OR`). Casar um evento contra ele exigiria avaliar a expressão inteira; avaliar só o primeiro termo daria hit errado em silêncio.
- Objetos que não são `indicator`. O filtro `match[type]=indicator` roda **no servidor**, então malware, campanhas e relacionamentos nem trafegam.

## OpenCTI, `opencti`

:::tip[Configure em Enriquecimento → Fontes antes de usar numa regra]
Este enricher precisa saber o endereço da sua instância e ter uma credencial. Isso é uma **fonte configurada**: crie uma em **Enriquecimento → Fontes**, e depois a regra da política só cita o nome dela. A credencial vai uma única vez, o servidor a cifra, e nem a API nem a tela a devolvem, o que você vê depois é só "credencial cadastrada".
:::

| | |
|---|---|
| Modo | por evento |
| Egresso | interno (a instância é sua) |
| Tipos de chave | `ip`, `domain`, `url`, `file_hash`, `mac` |
| Requer configuração externa? | Sim. URL da sua instância + token de API |

Sincroniza periodicamente os indicadores (observáveis) da sua instância própria do OpenCTI para uma tabela local, e casa a chave do evento contra ela. Por rodar **por evento**, o resultado alimenta as regras de detecção em pipeline, diferente de uma consulta remota, que chegaria tarde demais para isso. Como a instância é sua, nenhum dado do seu ambiente sai para fora.

**Campos que devolve:**

| Campo | Descrição |
|---|---|
| `score` | Score do OpenCTI (0 a 100) |
| `entity_type` | Tipo do observável (`IPv4-Addr`, `Domain-Name`, `StixFile`, ...) |
| `kind` | Tipo de chave normalizado (`ip`, `domain`, `url`, `file_hash`, `mac`) |
| `opencti_id` | Id interno do observável no OpenCTI |
| `created_at` / `updated_at` | Datas de criação e última atualização |
| `markings` | Marcações TLP/PAP |
| `labels` | Rótulos atribuídos no OpenCTI |
| `created_by` | Quem reportou o indicador (o feed ou o analista) |
| `source` | Sempre `"opencti"`, indica de onde veio o dado |

**Configuração:**

| Campo | Obrigatório | O que é |
|---|---|---|
| `url` | Sim | Endereço base da sua instância, ex.: `https://opencti.interno` |
| `preset` | Não (padrão `ip`) | O que buscar. Veja a tabela abaixo |
| `page_size` | Não (padrão 500) | Itens por página ao sincronizar |
| `max_pages` | Não (padrão 40) | Teto de páginas por atualização. Protege contra uma instância muito grande drenar o ciclo inteiro |
| `min_score` | Não (padrão 0) | Só traz indicadores com score igual ou acima deste valor |

A credencial não aparece aqui: ela é um campo próprio da fonte configurada, não da configuração.

### Escolha o preset pelo tipo de indicador

O preset filtra **no servidor do OpenCTI**. Uma tabela de IP não baixa hashes e URLs para descartar depois, o que muda bastante o tempo de sincronização numa base grande.

| Preset | Traz |
|---|---|
| `ip` | `IPv4-Addr` e `IPv6-Addr` |
| `domain` | `Domain-Name` e `Hostname` |
| `url` | `Url` |
| `file_hash` | `StixFile` e `Artifact` (SHA-256, MD5, ...) |
| `mac` | `Mac-Addr` |
| `all_observables` | Todos os tipos acima numa tabela só |
| `indicators` | Indicadores STIX em vez de observáveis. Veja abaixo |

### O preset `indicators` é o que muda o jogo

Um observável responde "esse IP está na base". Um indicador responde "esse IP é C2 conhecido, ativo, com confiança 80". A diferença aparece em quatro campos extras:

| Campo | Por que importa |
|---|---|
| `valid_until` e `revoked` | Indicador expirado ou revogado é **descartado na carga**. Intel vencida é a maior fonte de falso positivo em feed de threat intel: sem esse corte, o alerta dispara por um IP que foi C2 há dois anos e hoje pertence a uma CDN |
| `confidence` | Separa o que um analista marcou como confiável do que entrou por importação automática |
| `detection` | O indicador foi marcado como acionável para detecção |
| `kill_chain_phases` | A fase (`command-and-control`, `delivery`, `exfiltration`). É o que transforma um hit em contexto acionável no SIEM |

Indicadores com padrão STIX composto (`AND`/`OR`) são pulados de propósito: casar um evento contra eles exigiria avaliar a expressão inteira, e uma avaliação parcial daria hit errado em silêncio.

:::caution[Confira a query contra a sua versão do OpenCTI antes de usar em produção]
O schema GraphQL do OpenCTI mudou entre as versões 5.x e 6.x. Se a sincronização não trouxer nada, esse é o primeiro lugar a checar. O campo `query` na configuração aceita uma query própria, que é a saída para instâncias divergentes.
:::

## VirusTotal, `virustotal`

:::tip[Configure em Enriquecimento → Fontes antes de usar numa regra]
Como o OpenCTI, este enricher exige credencial. Crie uma **fonte configurada** em **Enriquecimento → Fontes** com a sua chave de API; a regra depois cita só o nome dela. A chave sobe uma única vez e é cifrada pelo servidor, a API nunca a devolve.
:::

| | |
|---|---|
| Modo | **por lote** |
| Egresso | **envia a terceiro** |
| Tipos de chave | `ip`, `domain`, `file_hash` |
| Requer configuração externa? | Sim, chave de API |

Consulta a reputação de um indicador na API v3 do VirusTotal. Por rodar por lote, o resultado chega ao evento antes de ele ser roteado, mas **não** alimenta as regras de detecção em pipeline, que já rodaram por evento antes desse enriquecimento acontecer.

**Campos que devolve:**

| Campo | Descrição |
|---|---|
| `malicious` / `suspicious` / `harmless` / `undetected` | Quantas engines classificaram o indicador em cada categoria |
| `total_engines` | Total de engines que responderam |
| `malicious_ratio` | `malicious / total_engines`, de 0.0 a 1.0 |
| `reputation` | Score de reputação da comunidade VirusTotal |
| `last_analysis_date` | Data (epoch) da última análise |
| `tags` | Tags atribuídas pelo VirusTotal |
| `source` | Sempre `"virustotal"` |

**Configuração:**

| Campo | Obrigatório | O que é |
|---|---|---|
| `key_kind` | Não (padrão `ip`) | `ip`, `domain` ou `file_hash`. Uma instância do enricher resolve um tipo por vez |
| `max_keys_per_batch` | Não (padrão 25) | Teto de indicadores consultados por lote |

A chave de API não aparece na tabela porque não é configuração: é o campo **Credencial** da fonte, que sobe uma vez e o servidor cifra.

:::danger[A chave pública libera só 4 consultas por minuto]
Sem um `when` restritivo na regra, um lote de 200 eventos com indicadores distintos consome a cota diária de uma chave gratuita em segundos. Restrinja a regra para consultar só o que precisa, por exemplo apenas indicadores que uma regra anterior já marcou como desconhecido.

O gate `when` é avaliado **antes** da chamada, então o que ele barra não sai da sua infraestrutura nem gasta cota.

Toda consulta ao VirusTotal envia um indicador do seu ambiente (um IP, um hash) para fora. Confirme que isso é aceitável para o tipo de dado antes de habilitar.
:::

## Próximos passos

- **Ainda não configurou nada?** Comece por [Como enriquecer um evento](./how-to-enrich.md).
- **Quer entender os conceitos primeiro?** Veja [O que é o enriquecimento](./overview.md).
