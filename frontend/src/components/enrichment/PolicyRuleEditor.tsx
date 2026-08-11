import type React from "react"
import { useId } from "react"
import { useTranslation } from "react-i18next"
import { PlusIcon, Trash2Icon } from "lucide-react"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Select } from "@/components/ui/Select/Select"
import { Badge } from "@/components/ui/Badge/Badge"
import type {
  EnricherCatalogItem,
  EnrichmentRule,
  EnrichmentSource,
  EnrichmentTable,
} from "@/services/api"

interface PolicyRuleEditorProps {
  rules: EnrichmentRule[]
  enrichers: EnricherCatalogItem[]
  tables: EnrichmentTable[]
  /** Fontes configuradas da org — quem exige credencial escolhe uma daqui. */
  sources?: EnrichmentSource[]
  onChange: (rules: EnrichmentRule[]) => void
}

let newRuleCounter = 0

function newRule(defaultEnricher: string): EnrichmentRule {
  newRuleCounter += 1
  return {
    id: `regra-${newRuleCounter}`,
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
    onChange([...rules, newRule(enrichers[0]?.name ?? "")])
  }

  function updateOutput(ruleIndex: number, outputIndex: number, patch: Partial<EnrichmentRule["outputs"][number]>) {
    const rule = rules[ruleIndex]
    const outputs = [...rule.outputs]
    outputs[outputIndex] = { ...outputs[outputIndex], ...patch }
    updateRule(ruleIndex, { outputs })
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
                <Input
                  label={t("policies.versions.keySource")}
                  value={rule.key.source}
                  onChange={(e) => updateRule(index, { key: { ...rule.key, source: e.target.value } })}
                  className="font-mono text-xs"
                  helperText={t("policies.versions.keySourceHint")}
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
                {rule.outputs.map((out, oi) => (
                  <div key={`out-${oi}`} className="flex items-end gap-2">
                    <Input
                      label={oi === 0 ? t("policies.versions.outputFrom") : undefined}
                      value={out.from}
                      onChange={(e) => updateOutput(index, oi, { from: e.target.value })}
                      placeholder="site"
                      className="w-1/3 font-mono text-xs"
                    />
                    <Input
                      label={oi === 0 ? t("policies.versions.outputTarget") : undefined}
                      value={out.target}
                      onChange={(e) => updateOutput(index, oi, { target: e.target.value })}
                      placeholder="_centralops.enrichment.src.site"
                      className="flex-1 font-mono text-xs"
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
                ))}
              </div>

              <div className="grid gap-3 sm:grid-cols-3">
                <Input
                  label={t("policies.versions.tags")}
                  value={(rule.tags ?? []).join(", ")}
                  onChange={(e) =>
                    updateRule(index, {
                      tags: e.target.value
                        .split(",")
                        .map((s) => s.trim())
                        .filter(Boolean),
                    })
                  }
                  placeholder="asset_known"
                  helperText={t("policies.versions.tagsHint")}
                />
                <Select
                  label={t("policies.versions.onMiss")}
                  value={rule.on_miss ?? "skip"}
                  onValueChange={(v) => updateRule(index, { on_miss: v as EnrichmentRule["on_miss"] })}
                  options={["skip", "tag", "default"].map((v) => ({ value: v, label: v }))}
                  size="sm"
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
