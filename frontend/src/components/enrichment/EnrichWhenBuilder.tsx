import type React from "react"
import { useTranslation } from "react-i18next"
import { Select } from "@/components/ui/Select/Select"
import { Notice } from "@/components/ui/Notice/Notice"
import { WhenPredicateBuilder } from "@/components/mappings/WhenPredicateBuilder"
import { TagChipsInput } from "./TagChipsInput"
import type { MappingPredicate } from "@/types"

type Gate = "sempre" | "exists" | "equals" | "in" | "not" | "has_tag" | "lacks_tag"

interface EnrichWhenBuilderProps {
  value: Record<string, unknown> | null | undefined
  tagSuggestions?: string[]
  onChange: (next: Record<string, unknown> | null) => void
}

/** Descobre qual gate está no objeto. Um gate tem exatamente uma chave. */
function gateOf(value: Record<string, unknown> | null | undefined): Gate {
  if (!value) return "sempre"
  for (const k of ["exists", "equals", "in", "not", "has_tag", "lacks_tag"] as const) {
    if (k in value) return k
  }
  return "sempre"
}

function asTags(raw: unknown): string[] {
  if (typeof raw === "string") return [raw]
  if (Array.isArray(raw)) return raw.map(String).filter(Boolean)
  return []
}

/**
 * Editor da condição que decide se a regra roda.
 *
 * Duas famílias, com origens diferentes na DSL:
 *
 * - `exists | equals | in | not` olham o EVENTO e passam pelo mesmo compilador
 *   de predicado do mapeamento (`normalize/predicates.py`), então o builder
 *   recursivo de lá é reusado inteiro em vez de reimplementado.
 * - `has_tag | lacks_tag` olham as tags que regras ANTERIORES escreveram. São
 *   exclusivos do enriquecimento e só existem na raiz (`dsl.py:211-215`); não
 *   aparecem dentro de um `not`, e o formulário reflete isso.
 *
 * **Um gate tem exatamente uma chave** (`dsl.py:203-207`). Não existe `and`/`or`
 * na DSL, e isso é deliberado: condição composta se escreve encadeando regras,
 * uma produzindo a tag que a outra consome. É o que o botão "adicionar
 * alternativa" do editor de regras monta.
 */
export const EnrichWhenBuilder: React.FC<EnrichWhenBuilderProps> = ({
  value,
  tagSuggestions = [],
  onChange,
}) => {
  const { t } = useTranslation("enrichment")
  const gate = gateOf(value)

  function switchGate(next: Gate) {
    if (next === "sempre") return onChange(null)
    if (next === "has_tag" || next === "lacks_tag") return onChange({ [next]: [] })
    if (next === "exists") return onChange({ exists: "" })
    if (next === "equals") return onChange({ equals: { source: "", value: "" } })
    if (next === "in") return onChange({ in: { source: "", values: [] } })
    return onChange({ not: { exists: "" } })
  }

  const options: Array<{ value: Gate; label: string }> = [
    { value: "sempre", label: t("policies.versions.when.always") },
    { value: "exists", label: t("policies.versions.when.exists") },
    { value: "equals", label: t("policies.versions.when.equals") },
    { value: "in", label: t("policies.versions.when.in") },
    { value: "not", label: t("policies.versions.when.not") },
    { value: "has_tag", label: t("policies.versions.when.hasTag") },
    { value: "lacks_tag", label: t("policies.versions.when.lacksTag") },
  ]

  const isTagGate = gate === "has_tag" || gate === "lacks_tag"

  /** Gate sintaticamente válido que NUNCA casa. Ver o aviso mais abaixo. */
  const incompleteReason: string | null = (() => {
    if (!value) return null
    if (gate === "exists" && !String(value.exists ?? "").trim()) {
      return "policies.versions.when.emptySource"
    }
    if (gate === "equals") {
      const inner = (value.equals ?? {}) as Record<string, unknown>
      if (!String(inner.source ?? "").trim()) return "policies.versions.when.emptySource"
    }
    if (gate === "in") {
      const inner = (value.in ?? {}) as Record<string, unknown>
      if (!String(inner.source ?? "").trim()) return "policies.versions.when.emptySource"
      if (!Array.isArray(inner.values) || inner.values.length === 0) {
        return "policies.versions.when.emptyValues"
      }
    }
    return null
  })()

  return (
    <div className="space-y-2 rounded-md border border-border-subtle p-3" data-testid="when-builder">
      <Select
        label={t("policies.versions.when.label")}
        value={gate}
        onValueChange={(v) => switchGate(v as Gate)}
        options={options}
        size="sm"
        helperText={t("policies.versions.when.hint")}
      />

      {isTagGate && (
        <>
          <TagChipsInput
            label={t(
              gate === "has_tag"
                ? "policies.versions.when.hasTagField"
                : "policies.versions.when.lacksTagField",
            )}
            value={asTags(value?.[gate])}
            suggestions={tagSuggestions}
            onChange={(tags) => onChange({ [gate]: tags })}
            placeholder="asset_known"
            data-testid="when-tags"
          />
          {asTags(value?.[gate]).length === 0 && (
            <Notice variant="warning" title={t("policies.versions.when.tagRequired")} />
          )}
        </>
      )}

      {!isTagGate && gate !== "sempre" && (
        <>
          <WhenPredicateBuilder
            value={value as MappingPredicate}
            onChange={(next) => onChange((next as Record<string, unknown> | null) ?? null)}
          />
          {/* O compilador ACEITA `{in: {source, values: []}}` e `{exists: ""}`.
              Publicar assim dá 201, e em runtime o gate é sempre falso: a regra
              não roda, não conta hit, miss, skip nem erro, e some dos painéis
              como se nenhum evento tivesse o campo. Só a UI pode alertar. */}
          {incompleteReason && <Notice variant="warning" title={t(incompleteReason)} />}
        </>
      )}
    </div>
  )
}

export default EnrichWhenBuilder
