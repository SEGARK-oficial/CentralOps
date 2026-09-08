import type React from "react"
import { useTranslation } from "react-i18next"
import type { TFunction } from "i18next"
import type { EnricherCatalogItem, EnrichmentRule } from "@/services/api"

/**
 * Resumo legível de uma regra de enriquecimento.
 *
 * A regra é escrita no vocabulário do motor — `enricher`, `key.source`,
 * `on_miss: skip | tag | default` — e nenhum campo, isolado, diz o que ela faz.
 * Revisar quatro regras exigia ler doze campos e montar a frase de cabeça.
 *
 * O resumo não substitui o formulário: ele é o que aparece na LISTA, onde a
 * pergunta é "qual destas eu quero abrir?". O formulário continua sendo onde se
 * edita, agora na ordem da frase.
 *
 * O caminho da chave é encurtado para o último segmento significativo
 * (`normalized.src_endpoint.ip` → `IP de origem`) por um mapa pequeno dos
 * caminhos que de fato aparecem. Um caminho fora do mapa cai no próprio texto,
 * em vez de virar uma tradução inventada — errar o nome de um campo aqui seria
 * pior que mostrar o caminho cru.
 */

/**
 * Caminhos OCSF comuns → chave de tradução do nome que o operador usa.
 *
 * O texto NÃO fica aqui: a interface tem três idiomas, e frase fixa em
 * português já apareceu uma vez nesta feature (na prontidão, vinda do
 * backend). O mapa guarda a chave; o catálogo guarda a palavra.
 */
const PATH_KEYS: Record<string, string> = {
  "normalized.src_endpoint.ip": "srcIp",
  "normalized.dst_endpoint.ip": "dstIp",
  "normalized.src_endpoint.hostname": "srcHost",
  "normalized.dst_endpoint.hostname": "dstHost",
  "normalized.device.ip": "deviceIp",
  "normalized.device.hostname": "deviceHost",
  "normalized.actor.user.name": "user",
  "normalized.user.name": "user",
  "normalized.file.hashes[0].value": "fileHash",
  "normalized.url.hostname": "urlHost",
  "normalized.url.text": "url",
}

export function labelForPath(path: string | undefined, t: TFunction): string {
  if (!path) return "—"
  const key = PATH_KEYS[path]
  // Caminho fora do mapa cai no texto CRU. Inventar uma tradução para um campo
  // desconhecido seria pior: o operador confiaria num nome errado.
  return key ? t(`policies.summary.path.${key}`) : path
}

/** Último segmento do destino, sem o prefixo obrigatório. */
function outputLabel(target: string): string {
  return target.replace(/^_centralops\.enrichment\./, "") || target
}

export interface RuleSummaryParts {
  key: string
  via: string
  writes: string[]
  /** ``null`` quando a regra roda sempre. */
  when: string | null
  onMiss: string | null
}

export function summarizeRule(
  rule: EnrichmentRule,
  enrichers: EnricherCatalogItem[] = [],
  t: TFunction = ((k: string) => k) as unknown as TFunction,
): RuleSummaryParts {
  const cat = enrichers.find((e) => e.name === rule.enricher)
  // A tabela é mais informativa que o enricher: duas regras com `table_cidr`
  // consultando tabelas diferentes fazem coisas diferentes, e é o nome da
  // tabela que o operador reconhece.
  const via = rule.table || rule.source || cat?.label || rule.enricher

  const writes = (rule.outputs ?? [])
    .map((o) => outputLabel(o.target ?? ""))
    .filter(Boolean)

  let when: string | null = null
  const w = rule.when as Record<string, unknown> | null | undefined
  if (w) {
    const gate = (
      ["has_tag", "lacks_tag", "exists", "equals", "in", "not"] as const
    ).find((k) => k in w)
    if (gate) when = t(`policies.summary.when.${gate}`)
  }

  const onMiss =
    rule.on_miss === "tag" || rule.on_miss === "default"
      ? t(`policies.summary.onMiss.${rule.on_miss}`)
      : null

  return { key: labelForPath(rule.key?.source, t), via, writes, when, onMiss }
}

/**
 * O resumo como elemento. Os nomes ficam em destaque e os conectivos não —
 * quem varre a lista procura os substantivos, não as preposições.
 */
export const RuleSummary: React.FC<{
  rule: EnrichmentRule
  enrichers?: EnricherCatalogItem[]
}> = ({ rule, enrichers = [] }) => {
  const { t } = useTranslation("enrichment")
  const s = summarizeRule(rule, enrichers, t)
  return (
    <span className="text-xs text-muted">
      <b className="font-medium text-text">{s.key}</b>
      {" → "}
      <b className="font-medium text-text">{s.via}</b>
      {s.writes.length > 0 ? (
        <>
          {" → "}
          <b className="font-medium text-text">{s.writes.join(", ")}</b>
        </>
      ) : null}
      {s.when ? <span className="text-muted">{` · ${s.when}`}</span> : null}
      {s.onMiss ? <span className="text-muted">{` · ${s.onMiss}`}</span> : null}
    </span>
  )
}

export default RuleSummary
