import type React from "react"
import { useId, useMemo } from "react"
import { useTranslation } from "react-i18next"
import { GitBranchIcon, PlusIcon, Trash2Icon } from "lucide-react"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Select } from "@/components/ui/Select/Select"
import { Badge } from "@/components/ui/Badge/Badge"
import { JMESPathInput } from "@/components/mappings/JMESPathInput"
import { EnrichWhenBuilder } from "./EnrichWhenBuilder"
import { TagChipsInput } from "./TagChipsInput"
import type {
  EnricherCatalogItem,
  EnrichmentRule,
  EnrichmentSource,
  EnrichmentTable,
} from "@/services/api"

/** Prefixo obrigatório de todo output (ADR-LOCAL-0002 §3.1). */
const TARGET_PREFIX = "_centralops.enrichment."

/**
 * Tags que o runtime escreve sozinho. Entram nas sugestões porque uma regra
 * pode legitimamente reagir a elas com `has_tag`, e ninguém as adivinha.
 */
const RUNTIME_TAGS = ["enrich_degraded", "enrich_ambiguous"]

/**
 * Converte o texto do campo "Padrão" para o tipo que o enricher devolve no hit.
 *
 * `applier._write_outputs` escreve `out.default` CRU. Guardar sempre string faz
 * o MESMO target sair number no acerto e string no miss, e quem consome depois
 * (regra de rota com `range`, mapeamento de destino que espera número) quebra
 * só no evento de miss, longe daqui.
 *
 * Usa o tipo declarado em `output_fields` quando existe; sem declaração, cai
 * numa inferência conservadora (só number e boolean, nunca objeto).
 */
function coerceDefault(raw: string, declared?: string): unknown {
  const text = raw.trim()
  if (text === "") return undefined

  const kind = (declared ?? "").toLowerCase()
  if (kind.includes("number") || kind.includes("int") || kind.includes("float")) {
    const n = Number(text)
    return Number.isFinite(n) ? n : text
  }
  if (kind.includes("bool")) {
    if (text === "true") return true
    if (text === "false") return false
    return text
  }
  if (kind.includes("str")) return raw

  // Sem declaração: infere o mínimo que não surpreende.
  if (text === "true") return true
  if (text === "false") return false
  if (/^-?\d+(\.\d+)?$/.test(text)) return Number(text)
  return raw
}

/** `on_miss: "default"` só faz efeito se algum output tiver valor padrão. */
function hasDefault(rule: EnrichmentRule): boolean {
  return rule.outputs.some((o) => o.default !== undefined && o.default !== null && o.default !== "")
}

interface PolicyRuleEditorProps {
  rules: EnrichmentRule[]
  enrichers: EnricherCatalogItem[]
  tables: EnrichmentTable[]
  /** Fontes configuradas da org — quem exige credencial escolhe uma daqui. */
  sources?: EnrichmentSource[]
  /**
   * Caminhos que a organização de fato produz, para sugerir em `key.source`.
   * Vem de `GET /collectors/enrichment/key-sources`.
   */
  keySources?: string[]
  /** `true` quando `keySources` veio dos mappings ativos, não do catálogo comum. */
  keySourcesFromMappings?: boolean
  onChange: (rules: EnrichmentRule[]) => void
}

/**
 * Primeiro `regra-N` que ainda não está em uso.
 *
 * Um contador de módulo não serve: ele nasce em zero a cada carregamento de
 * página, e desde que o editor passou a abrir COM as regras da versão vigente,
 * a primeira "Adicionar regra" da sessão gerava `regra-1` em cima de uma
 * `regra-1` que já existia. O compilador recusa id duplicado (`dsl.py:317`),
 * então o operador só descobria no 422, sem saber qual clique causou.
 */
function nextRuleId(existing: EnrichmentRule[]): string {
  const taken = new Set(existing.map((r) => r.id))
  let n = existing.length + 1
  while (taken.has(`regra-${n}`)) n += 1
  return `regra-${n}`
}

/** Id livre para a alternativa. Dois cliques no mesmo botão colidiriam. */
function nextAlternativeId(existing: EnrichmentRule[], baseId: string): string {
  const taken = new Set(existing.map((r) => r.id))
  const first = `${baseId}-alternativa`
  if (!taken.has(first)) return first
  let n = 2
  while (taken.has(`${first}-${n}`)) n += 1
  return `${first}-${n}`
}

function newRule(defaultEnricher: string, id: string): EnrichmentRule {
  return {
    id,
    enricher: defaultEnricher,
    key: { source: "normalized.src_endpoint.ip", kind: "ip" },
    outputs: [{ from: "", target: "_centralops.enrichment." }],
    tags: [],
    on_miss: "skip",
  }
}

/**
 * Editor estruturado de uma lista de regras de política.
 *
 * Deliberadamente MAIS SIMPLES que o `RulesEditor` de mappings: a DSL de
 * enriquecimento tem 6 campos por regra (contra ~12 da DSL de mapping, com
 * value_map/type_cast/array_builder), então um construtor por campo — sem
 * reorder, sem import/export, sem agrupamento — já cobre o formato inteiro
 * sem reproduzir a complexidade de uma feature diferente.
 *
 * O que o editor NÃO valida (fica para o servidor, no commit): se o `target`
 * de cada output começa com `_centralops.enrichment.` — isso é regra do
 * compilador (ADR-LOCAL-0002 §3.1) e a mensagem de erro do backend já explica
 * o porquê; duplicá-la aqui divergiria da fonte da verdade.
 */
export const PolicyRuleEditor: React.FC<PolicyRuleEditorProps> = ({
  rules,
  enrichers,
  tables,
  sources = [],
  keySources = [],
  keySourcesFromMappings = false,
  onChange,
}) => {
  const { t } = useTranslation("enrichment")
  const headingId = useId()

  const enricherOptions = enrichers.map((e) => ({ value: e.name, label: `${e.label} (${e.name})` }))
  const tableOptions = tables.map((tb) => ({ value: tb.name, label: tb.name }))

  function enricherOf(name: string): EnricherCatalogItem | undefined {
    return enrichers.find((e) => e.name === name)
  }

  function updateRule(index: number, patch: Partial<EnrichmentRule>) {
    const next = [...rules]
    next[index] = { ...next[index], ...patch }
    onChange(next)
  }

  function removeRule(index: number) {
    onChange(rules.filter((_, i) => i !== index))
  }

  function addRule() {
    onChange([...rules, newRule(enrichers[0]?.name ?? "", nextRuleId(rules))])
  }

  function updateOutput(ruleIndex: number, outputIndex: number, patch: Partial<EnrichmentRule["outputs"][number]>) {
    const rule = rules[ruleIndex]
    const outputs = [...rule.outputs]
    outputs[outputIndex] = { ...outputs[outputIndex], ...patch }

    // Apagar o último valor padrão deixava `on_miss: "default"` no objeto com a
    // opção já desabilitada na tela: estado que a UI não deixa escolher mas
    // continua publicando, e que não escreve nada no evento.
    const stillHasDefault = outputs.some(
      (o) => o.default !== undefined && o.default !== null && o.default !== "",
    )
    const patchRule: Partial<EnrichmentRule> = { outputs }
    if (!stillHasDefault && rule.on_miss === "default") patchRule.on_miss = "skip"

    updateRule(ruleIndex, patchRule)
  }

  function addOutput(ruleIndex: number) {
    const rule = rules[ruleIndex]
    updateRule(ruleIndex, {
      outputs: [...rule.outputs, { from: "", target: "_centralops.enrichment." }],
    })
  }

  function removeOutput(ruleIndex: number, outputIndex: number) {
    const rule = rules[ruleIndex]
    if (rule.outputs.length <= 1) return // sempre pelo menos 1 output — a API exige
    updateRule(ruleIndex, { outputs: rule.outputs.filter((_, i) => i !== outputIndex) })
  }

  /**
   * Cria a regra "se a anterior não casou, tente esta".
   *
   * A DSL não tem `or`: um gate tem exatamente uma chave (`dsl.py:203-207`).
   * O jeito de escrever alternativa é encadear regras, uma marcando com tag e a
   * seguinte consumindo `lacks_tag`. Esse encadeamento é fácil de errar à mão
   * (a tag tem que bater exatamente), então o botão monta o par.
   */
  /**
   * A base só pode virar cascata se o gate dela for ausente ou `lacks_tag`.
   *
   * Um gate tem EXATAMENTE uma chave (`dsl.py:203-207`), então não existe como
   * somar `lacks_tag` a um `exists`/`equals`/`in`/`not`/`has_tag` que já esteja
   * lá. Substituir seria descartar em silêncio a condição que o operador
   * escreveu, e a alternativa passaria a rodar justamente nos eventos que ele
   * excluiu. Melhor desabilitar o botão e dizer por quê.
   */
  function canChain(rule: EnrichmentRule): boolean {
    const w = rule.when as Record<string, unknown> | null | undefined
    if (!w) return true
    return Object.keys(w).length === 1 && "lacks_tag" in w
  }

  function addAlternative(ruleIndex: number) {
    const base = rules[ruleIndex]
    if (!canChain(base)) return
    const marker = (base.tags ?? [])[0] ?? `${base.id}_ok`
    const withTag: EnrichmentRule = {
      ...base,
      tags: (base.tags ?? []).includes(marker) ? base.tags : [...(base.tags ?? []), marker],
    }

    // ACUMULA os marcadores em vez de substituir. `lacks_tag` é "pula se
    // QUALQUER uma destas estiver presente" (`applier.py:138`), então numa
    // cascata de três níveis o terceiro elo precisa recusar os marcadores dos
    // DOIS anteriores. Sobrescrever o gate faria o terceiro rodar mesmo quando
    // o primeiro já resolveu, e o compilador aceitaria: continua uma chave só.
    const inherited = Array.isArray(base.when?.lacks_tag)
      ? (base.when?.lacks_tag as unknown[]).map(String)
      : typeof base.when?.lacks_tag === "string"
        ? [base.when.lacks_tag as string]
        : []
    const markers = Array.from(new Set([...inherited, marker]))

    const alternative: EnrichmentRule = {
      ...base,
      id: nextAlternativeId(rules, base.id),
      when: { lacks_tag: markers },
      tags: [],
    }
    const next = [...rules]
    next[ruleIndex] = withTag
    next.splice(ruleIndex + 1, 0, alternative)
    onChange(next)
  }

  /**
   * Tags que o operador pode escolher sem inventar uma que não existe.
   *
   * Junta o que as regras deste editor já escrevem, o que elas já consomem em
   * `has_tag`/`lacks_tag`, e as que o runtime gera sozinho. Errar uma letra
   * numa tag é silencioso: a tag "errada" é válida e a regra que a consome
   * simplesmente para de casar, sem erro em lugar nenhum.
   */
  /**
   * Caminhos oferecidos no campo "Origem da chave", em ordem de confiança.
   *
   * 1. O que as OUTRAS regras desta política já usam. É grátis (já está em
   *    memória) e é o mais contextual: reusar a mesma chave entre regras com
   *    enrichers diferentes é o padrão mais comum.
   * 2. O que a organização de fato produz, vindo dos mappings ativos dela.
   *
   * Deduplicado preservando a ordem, então um caminho que aparece nos dois
   * lugares fica no topo uma vez só.
   */
  const keySourceOptions = useMemo(() => {
    const seen = new Set<string>()
    const out: string[] = []
    for (const path of [...rules.map((r) => r.key?.source), ...keySources]) {
      if (!path || seen.has(path)) continue
      seen.add(path)
      out.push(path)
    }
    return out
  }, [rules, keySources])

  /** `true` quando a lista reflete o inventário REAL da org, não o catálogo. */
  const keySourceHint = keySourcesFromMappings

  const tagSuggestions = useMemo(() => {
    const out = new Set<string>(RUNTIME_TAGS)
    for (const r of rules) {
      for (const tg of r.tags ?? []) out.add(tg)
      const w = r.when as Record<string, unknown> | null | undefined
      for (const k of ["has_tag", "lacks_tag"] as const) {
        const raw = w?.[k]
        if (typeof raw === "string") out.add(raw)
        else if (Array.isArray(raw)) for (const tg of raw) out.add(String(tg))
      }
    }
    return Array.from(out)
  }, [rules])

  return (
    <section aria-labelledby={headingId} className="space-y-3" data-testid="policy-rule-editor">
      <div className="flex items-center justify-between">
        <h3 id={headingId} className="text-sm font-semibold">
          {t("policies.versions.rules")}
        </h3>
        <Badge variant="default">{t("policies.versions.ruleCount", { count: rules.length })}</Badge>
      </div>

      {rules.length === 0 && (
        <p className="text-sm text-muted" data-testid="rules-empty">
          {t("policies.versions.rulesEmpty")}
        </p>
      )}

      <div className="space-y-4">
        {rules.map((rule, index) => {
          const enricher = enricherOf(rule.enricher)
          const usesTable = tables.length > 0 // enricher de tabela é o caso mais comum; campo sempre disponível
          // Enricher que declara required_secrets NÃO roda sem fonte — a DSL
          // recusa no commit, então mostrar o campo aqui evita o 422.
          const needsSource = (enricher?.required_secrets?.length ?? 0) > 0
          return (
            <div
              key={`rule-${index}`}
              className="space-y-3 rounded-lg border border-border p-4"
              data-testid={`rule-card-${index}`}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="grid flex-1 gap-3 sm:grid-cols-2">
                  <Input
                    label={t("policies.versions.ruleId")}
                    value={rule.id}
                    onChange={(e) => updateRule(index, { id: e.target.value })}
                  />
                  <Select
                    label={t("policies.versions.enricher")}
                    value={rule.enricher}
                    onValueChange={(v) => updateRule(index, { enricher: String(v), source: null })}
                    options={enricherOptions}
                    size="sm"
                  />
                </div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => removeRule(index)}
                  aria-label={t("policies.versions.removeRule")}
                  data-testid={`remove-rule-${index}`}
                >
                  <Trash2Icon size={14} aria-hidden />
                </Button>
              </div>

              {usesTable && (
                <Select
                  label={t("policies.versions.table")}
                  value={rule.table ?? ""}
                  onValueChange={(v) => updateRule(index, { table: String(v) || null })}
                  options={[{ value: "", label: t("policies.versions.tableNone") }, ...tableOptions]}
                  size="sm"
                  helperText={t("policies.versions.tableHint")}
                />
              )}

              {/* Fonte configurada: obrigatória para enricher com credencial. A
                  regra cita o NOME; a credencial vive na linha escopada à org e
                  nunca trafega no JSON da política. */}
              {needsSource && (
                <Select
                  label={t("policies.versions.source")}
                  value={rule.source ?? ""}
                  onValueChange={(v) => updateRule(index, { source: String(v) || null })}
                  options={[
                    { value: "", label: t("policies.versions.sourceNone") },
                    ...sources
                      .filter((s) => s.enricher === rule.enricher)
                      .map((s) => ({ value: s.name, label: s.name })),
                  ]}
                  size="sm"
                  error={!rule.source ? t("policies.versions.sourceRequired") : undefined}
                  helperText={t("policies.versions.sourceHint")}
                />
              )}

              <div className="grid gap-3 sm:grid-cols-2">
                {/* Combobox, não texto livre: um caminho errado aqui NÃO dá
                    422. A regra publica com 201 e simplesmente nunca casa, e
                    nos painéis aparece com zero em tudo — indistinguível de
                    "nenhum evento tinha o campo". Sugerir o que a organização
                    de fato produz é a única defesa possível. Degrada para
                    texto livre quando não há sugestão (org sem integração
                    ativa e sem fallback), que é o comportamento do próprio
                    JMESPathInput. */}
                <JMESPathInput
                  label={t("policies.versions.keySource")}
                  value={rule.key.source}
                  onChange={(next) => updateRule(index, { key: { ...rule.key, source: next } })}
                  suggestions={keySourceOptions}
                  inputClassName="font-mono text-xs"
                  helperText={
                    keySourceHint
                      ? t("policies.versions.keySourceHintMapped")
                      : t("policies.versions.keySourceHint")
                  }
                  placeholder="normalized.src_endpoint.ip"
                />
                <Select
                  label={t("policies.versions.keyKind")}
                  value={rule.key.kind}
                  onValueChange={(v) => updateRule(index, { key: { ...rule.key, kind: String(v) } })}
                  options={(enricher?.key_kinds ?? ["ip", "domain", "url", "file_hash", "cve", "mac", "user", "container_id"]).map(
                    (k) => ({ value: k, label: k }),
                  )}
                  size="sm"
                />
              </div>

              {/* Outputs */}
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium text-muted">{t("policies.versions.outputs")}</span>
                  <Button
                    type="button"
                    variant="outline"
                    size="xs"
                    onClick={() => addOutput(index)}
                    data-testid={`add-output-${index}`}
                    leftIcon={<PlusIcon size={12} />}
                  >
                    {t("policies.versions.addOutput")}
                  </Button>
                </div>
                {rule.outputs.map((out, oi) => {
                  const known = Object.keys(enricher?.output_fields ?? {})
                  return (
                    <div key={`out-${oi}`} className="flex items-end gap-2">
                      {/* O enricher declara os campos que devolve. Quando a
                          declaração existe, escolher da lista elimina o erro
                          mais comum: nomear um campo que o provedor não
                          retorna, que vira miss silencioso, não erro. */}
                      {known.length > 0 ? (
                        <div className="w-1/3">
                          <Select
                            label={oi === 0 ? t("policies.versions.outputFrom") : undefined}
                            value={out.from}
                            onValueChange={(v) => updateOutput(index, oi, { from: String(v) })}
                            options={[
                              { value: "", label: t("policies.versions.outputFromNone") },
                              ...known.map((f) => ({
                                value: f,
                                label: `${f} (${String(enricher?.output_fields?.[f] ?? "")})`,
                              })),
                            ]}
                            size="sm"
                          />
                        </div>
                      ) : (
                        <Input
                          label={oi === 0 ? t("policies.versions.outputFrom") : undefined}
                          value={out.from}
                          onChange={(e) => updateOutput(index, oi, { from: e.target.value })}
                          placeholder="site"
                          className="w-1/3 font-mono text-xs"
                        />
                      )}

                      {/* O prefixo é fixo e não editável: escrever fora de
                          `_centralops.enrichment.` é 422 no commit, e pior,
                          fora dessa raiz o dado não é protegido pela redação
                          de PII. Deixar o campo livre convidava ao erro. */}
                      <div className="flex-1">
                        {oi === 0 && (
                          <span className="mb-1.5 block text-sm font-medium text-text">
                            {t("policies.versions.outputTarget")}
                          </span>
                        )}
                        <div className="flex items-center gap-0">
                          <span className="h-9 shrink-0 rounded-l-md border border-r-0 border-border-field bg-surface px-2 py-2 font-mono text-xs text-text-tertiary">
                            {TARGET_PREFIX}
                          </span>
                          <Input
                            aria-label={t("policies.versions.outputTargetSuffix")}
                            value={out.target.startsWith(TARGET_PREFIX)
                              ? out.target.slice(TARGET_PREFIX.length)
                              : out.target}
                            onChange={(e) =>
                              updateOutput(index, oi, {
                                target: `${TARGET_PREFIX}${e.target.value.replace(/^\.+/, "")}`,
                              })
                            }
                            placeholder="src.site"
                            className="rounded-l-none font-mono text-xs"
                          />
                        </div>
                      </div>

                      {/* Valor a escrever quando a consulta não acha nada.
                          Só tem efeito com `on_miss: default`, e é o que
                          habilita aquela opção. */}
                      <Input
                        label={oi === 0 ? t("policies.versions.outputDefault") : undefined}
                        value={out.default == null ? "" : String(out.default)}
                        onChange={(e) =>
                          updateOutput(index, oi, {
                            default: coerceDefault(
                              e.target.value,
                              enricher?.output_fields?.[out.from],
                            ),
                          })
                        }
                        placeholder={t("policies.versions.outputDefaultPlaceholder")}
                        className="w-1/5 font-mono text-xs"
                      />

                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => removeOutput(index, oi)}
                        disabled={rule.outputs.length <= 1}
                        aria-label={t("policies.versions.removeOutput")}
                      >
                        <Trash2Icon size={12} aria-hidden />
                      </Button>
                    </div>
                  )
                })}
              </div>

              {/* Condição: decide se a regra roda. Fica logo abaixo dos
                  outputs porque é o que o operador ajusta depois de ver o
                  resultado do dry-run. */}
              <EnrichWhenBuilder
                value={rule.when}
                tagSuggestions={tagSuggestions}
                onChange={(next) => updateRule(index, { when: next })}
              />

              <div className="flex justify-end">
                <Button
                  type="button"
                  variant="ghost"
                  size="xs"
                  onClick={() => addAlternative(index)}
                  disabled={!canChain(rule)}
                  title={canChain(rule) ? undefined : t("policies.versions.cannotChain")}
                  data-testid={`add-alternative-${index}`}
                  leftIcon={<GitBranchIcon size={12} />}
                >
                  {t("policies.versions.addAlternative")}
                </Button>
              </div>

              <div className="grid gap-3 sm:grid-cols-3">
                <TagChipsInput
                  label={t("policies.versions.tags")}
                  value={rule.tags ?? []}
                  suggestions={tagSuggestions}
                  onChange={(tags) => updateRule(index, { tags })}
                  placeholder="asset_known"
                  helperText={t("policies.versions.tagsHint")}
                  data-testid={`tags-${index}`}
                />
                <Select
                  label={t("policies.versions.onMiss")}
                  value={rule.on_miss ?? "skip"}
                  onValueChange={(v) => updateRule(index, { on_miss: v as EnrichmentRule["on_miss"] })}
                  options={["skip", "tag", "default"].map((v) => ({
                    value: v,
                    label: v,
                    // `default` sem nenhum output com valor padrão não escreve
                    // nada: a opção parece configurada e não faz efeito.
                    disabled: v === "default" && !hasDefault(rule),
                  }))}
                  size="sm"
                  helperText={
                    rule.on_miss === "default" && !hasDefault(rule)
                      ? t("policies.versions.onMissNeedsDefault")
                      : undefined
                  }
                />
                <Select
                  label={t("policies.versions.onError")}
                  value={rule.on_error ?? "skip"}
                  onValueChange={(v) => updateRule(index, { on_error: v as EnrichmentRule["on_error"] })}
                  options={["skip", "tag"].map((v) => ({ value: v, label: v }))}
                  size="sm"
                />
              </div>
            </div>
          )
        })}
      </div>

      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={addRule}
        data-testid="add-rule"
        leftIcon={<PlusIcon size={14} />}
      >
        {t("policies.versions.addRule")}
      </Button>
    </section>
  )
}

export default PolicyRuleEditor
