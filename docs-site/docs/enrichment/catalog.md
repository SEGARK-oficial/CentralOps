---
sidebar_position: 3
title: Catálogo de fontes
description: O que cada fonte de enriquecimento faz, que campos devolve, e o que ela exige para funcionar
---

# Catálogo de fontes

Esta página descreve as quatro fontes de enriquecimento disponíveis hoje. A lista **não é fixa no código do console** — a tela em **Enriquece → Enrichment → Catalog** sempre reflete exatamente o que a sua instalação tem registrado, então use `GET /collectors/enrichment/enrichers` (ou a própria tela) como fonte da verdade se este texto ficar desatualizado.

## Tabela do cliente (chave exata) — `table_exact`

| | |
|---|---|
| Modo | por evento |
| Egresso | nenhum |
| Tipos de chave | `ip`, `domain`, `url`, `file_hash`, `cve`, `mac`, `user`, `container_id` |
| Requer configuração externa? | Não |

Casa a chave do evento contra a **sua própria tabela**, por igualdade exata. É o enricher genérico: como você define os campos da tabela livremente, ele serve para qualquer contexto que você já tenha em planilha ou export de outro sistema — usuário → departamento, hostname → dono, hash → veredito interno, CVE → prioridade de patch da sua empresa.

Os campos que ele devolve são exatamente os que você colocou em cada linha da tabela — não há uma lista fixa.

## Tabela do cliente (CIDR, prefixo mais específico) — `table_cidr`

| | |
|---|---|
| Modo | por evento |
| Egresso | nenhum |
| Tipos de chave | `ip` |
| Requer configuração externa? | Não |

Casa um IP contra a sua tabela de faixas de rede (CIDR), sempre pelo prefixo **mais específico**. Se a tabela tem `10.0.0.0/16` e `10.0.5.0/24`, um evento com IP `10.0.5.7` recebe o resultado do `/24` — não o do `/16`. É a fonte certa para plano de endereçamento corporativo, inventário de rede exportado de uma ferramenta de gestão, ou listas de bloqueio distribuídas em CIDR.

Veja o passo a passo completo em [Como enriquecer um evento](./how-to-enrich.md).

## OpenCTI — `opencti`

| | |
|---|---|
| Modo | por evento |
| Egresso | interno (a instância é sua) |
| Tipos de chave | `ip`, `domain`, `url`, `file_hash`, `mac` |
| Requer configuração externa? | Sim — URL da sua instância + token de API |

Sincroniza periodicamente os indicadores (observáveis) da sua instância própria do OpenCTI para uma tabela local, e casa a chave do evento contra ela. Por rodar **por evento**, o resultado alimenta as regras de detecção em pipeline — diferente de uma consulta remota, que chegaria tarde demais para isso. Como a instância é sua, nenhum dado do seu ambiente sai para fora.

**Campos que devolve:**

| Campo | Descrição |
|---|---|
| `score` | Score do OpenCTI (0–100) |
| `entity_type` | Tipo do observável (`IPv4-Addr`, `Domain-Name`, `StixFile`, ...) |
| `kind` | Tipo de chave normalizado (`ip`, `domain`, `url`, `file_hash`, `mac`) |
| `opencti_id` | Id interno do observável no OpenCTI |
| `created_at` / `updated_at` | Datas de criação e última atualização do observável |
| `markings` | Marcações TLP/PAP |
| `labels` | Rótulos atribuídos no OpenCTI |
| `source` | Sempre `"opencti"` — indica de onde veio o dado |

**Configuração:**

| Campo | Obrigatório | O que é |
|---|---|---|
| `url` | Sim | Endereço base da sua instância, ex.: `https://opencti.interno` |
| `token_secret_ref` | Não | Referência ao token de API no cofre de segredos |
| `page_size` | Não (padrão 500) | Itens por página ao sincronizar |
| `max_pages` | Não (padrão 40) | Teto de páginas por atualização — protege contra uma instância muito grande drenar o ciclo inteiro |
| `min_score` | Não (padrão 0) | Só traz observáveis com score igual ou acima deste valor |

:::caution[Confira a query contra a sua versão do OpenCTI antes de usar em produção]
O schema GraphQL do OpenCTI mudou entre as versões 5.x e 6.x. Se a sincronização não trouxer nada, é o primeiro lugar a checar.
:::

## VirusTotal — `virustotal`

| | |
|---|---|
| Modo | **por lote** |
| Egresso | **envia a terceiro** |
| Tipos de chave | `ip`, `domain`, `file_hash` |
| Requer configuração externa? | Sim — chave de API |

Consulta a reputação de um indicador na API v3 do VirusTotal. Por rodar por lote, o resultado chega ao evento antes de ele ser roteado — mas **não** alimenta as regras de detecção em pipeline, que já rodaram por evento antes desse enriquecimento acontecer.

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
| `api_key_secret_ref` | Sim | Referência da chave de API no cofre de segredos |
| `key_kind` | Não (padrão `ip`) | `ip`, `domain` ou `file_hash` — uma instância do enricher resolve um tipo por vez |
| `max_keys_per_batch` | Não (padrão 25) | Teto de indicadores consultados por lote |

:::danger[A chave pública libera só 4 consultas por minuto]
Sem um `when` restritivo na regra, um lote de 200 eventos com indicadores distintos consome a cota diária de uma chave gratuita em segundos. Restrinja a regra para consultar só o que realmente precisa — por exemplo, apenas indicadores que uma regra anterior já marcou como "desconhecido". Veja o exemplo em [Como enriquecer um evento](./how-to-enrich.md#usando-uma-fonte-pronta-opencti-virustotal).

Toda consulta ao VirusTotal envia um indicador do seu ambiente (um IP, um hash de arquivo) para fora da sua infraestrutura. Confirme que isso é aceitável para o tipo de dado que você está enriquecendo antes de habilitar.
:::

## Próximos passos

- **Ainda não configurou nada?** Comece por [Como enriquecer um evento](./how-to-enrich.md).
- **Quer entender os conceitos primeiro?** Veja [O que é o enriquecimento](./overview.md).
