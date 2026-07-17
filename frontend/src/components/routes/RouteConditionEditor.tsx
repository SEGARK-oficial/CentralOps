import type React from "react"
import { useMemo } from "react"
import { useTranslation } from "react-i18next"
import { PlusIcon, Trash2Icon } from "lucide-react"
import { Button } from "@/components/ui/Button/Button"
import { Select } from "@/components/ui/Select/Select"
import { Input } from "@/components/ui/Input/Input"
import { CONDITION_OPERATORS, useConditionOperatorLabels, type ConditionOperator } from "@/lib/operatorLabels"
import type { RouteCondition } from "@/types"

// Espelha routing.engine.ALLOWED_FIELDS no backend. Os OPERADORES vêm de
// CONDITION_OPERATORS (fonte única em @/lib/operatorLabels, espelha ALLOWED_OPS);
// aqui o value permanece cru — só o label exibido é amigável.
const FIELDS = [
  "severity_id",
  "vendor",
  "organization_id",
  "event_type",
  "stream",
  "integration_id",
  "customer_id",
] as const
const NUMERIC = new Set(["severity_id", "organization_id", "integration_id", "customer_id"])

type Op = ConditionOperator
interface Clause {
  field: string
  op: Op
  value: string
}

function coerce(field: string, raw: string): unknown {
  return NUMERIC.has(field) && raw.trim() !== "" && !Number.isNaN(Number(raw)) ? Number(raw) : raw
}

/** clauses → condition JSON ({} = catch-all). */
export function clausesToCondition(clauses: Clause[]): RouteCondition {
  const cond: RouteCondition = {}
  // When promoting a field to an op-map, FOLD any pre-existing `eq` scalar back
  // in as {eq: scalar} instead of dropping it (review MEDIUM: spreading a scalar
  // as an object yields {}, silently broadening the rule). Keeps round-trip
  // identity for {eq, gte}-style maps.
  const opMap = (field: string): Record<string, unknown> => {
    const cur = cond[field]
    if (typeof cur === "object" && cur !== null && !Array.isArray(cur)) {
      return cur as Record<string, unknown>
    }
    return cur !== undefined ? { eq: cur } : {}
  }
  for (const c of clauses) {
    if (!c.field) continue
    if (c.op === "eq" && cond[c.field] === undefined) {
      cond[c.field] = coerce(c.field, c.value) // lone eq → scalar shorthand
    } else if (c.op === "exists") {
      cond[c.field] = { ...opMap(c.field), exists: c.value === "true" }
    } else if (c.op === "in" || c.op === "nin") {
      const arr = c.value.split(",").map((v) => coerce(c.field, v.trim())).filter((v) => v !== "")
      cond[c.field] = { ...opMap(c.field), [c.op]: arr }
    } else {
      cond[c.field] = { ...opMap(c.field), [c.op]: coerce(c.field, c.value) }
    }
  }
  return cond
}

/** condition JSON → clauses (for edit). */
export function conditionToClauses(cond: RouteCondition): Clause[] {
  const out: Clause[] = []
  for (const [field, spec] of Object.entries(cond || {})) {
    if (spec !== null && typeof spec === "object" && !Array.isArray(spec)) {
      for (const [op, val] of Object.entries(spec as Record<string, unknown>)) {
        out.push({
          field,
          op: op as Op,
          value: Array.isArray(val) ? (val as unknown[]).join(",") : String(val),
        })
      }
    } else {
      out.push({ field, op: "eq", value: String(spec) })
    }
  }
  return out
}

interface Props {
  clauses: Clause[]
  onChange: (next: Clause[]) => void
  disabled?: boolean
}

export const RouteConditionEditor: React.FC<Props> = ({ clauses, onChange, disabled }) => {
  const { t } = useTranslation("routing")
  const { options: operatorOptions } = useConditionOperatorLabels()
  const preview = useMemo(() => JSON.stringify(clausesToCondition(clauses)), [clauses])

  const set = (i: number, patch: Partial<Clause>) =>
    onChange(clauses.map((c, idx) => (idx === i ? { ...c, ...patch } : c)))
  const add = () => onChange([...clauses, { field: "severity_id", op: "gte", value: "" }])
  const remove = (i: number) => onChange(clauses.filter((_, idx) => idx !== i))

  return (
    <div className="space-y-3">
      {clauses.length === 0 && (
        <p className="text-xs text-text-tertiary">
          {t("conditionEditor.noConditions")}
        </p>
      )}
      {clauses.map((c, i) => (
        <div key={i} className="flex flex-wrap items-end gap-2">
          <div className="min-w-[150px] flex-1">
            <Select
              label={i === 0 ? t("conditionEditor.fieldLabel") : undefined}
              value={c.field}
              options={FIELDS.map((f) => ({ value: f, label: f }))}
              disabled={disabled}
              onValueChange={(v) => set(i, { field: String(v) })}
            />
          </div>
          <div className="w-40">
            <Select
              label={i === 0 ? t("conditionEditor.opLabel") : undefined}
              aria-label={t("conditionEditor.opSelectAriaLabel")}
              value={c.op}
              options={operatorOptions(CONDITION_OPERATORS)}
              disabled={disabled}
              onValueChange={(v) => set(i, { op: v as Op })}
            />
          </div>
          <div className="min-w-[140px] flex-1">
            {c.op === "exists" ? (
              <Select
                label={i === 0 ? t("conditionEditor.valueLabel") : undefined}
                value={c.value || "true"}
                options={[{ value: "true", label: "true" }, { value: "false", label: "false" }]}
                disabled={disabled}
                onValueChange={(v) => set(i, { value: String(v) })}
              />
            ) : (
              <Input
                label={i === 0 ? t("conditionEditor.valueLabel") : undefined}
                value={c.value}
                placeholder={c.op === "in" || c.op === "nin" ? t("conditionEditor.valueListPlaceholder") : t("conditionEditor.valuePlaceholder")}
                disabled={disabled}
                onChange={(e) => set(i, { value: e.target.value })}
              />
            )}
          </div>
          <Button type="button" variant="ghost" size="sm" onClick={() => remove(i)} disabled={disabled} leftIcon={<Trash2Icon size={14} />} aria-label={t("conditionEditor.removeConditionAria")} />
        </div>
      ))}
      <div className="flex items-center justify-between">
        <Button type="button" variant="outline" size="sm" onClick={add} disabled={disabled} leftIcon={<PlusIcon size={14} />}>
          {t("conditionEditor.addCondition")}
        </Button>
        <code className="truncate font-mono text-xs text-text-tertiary" title={preview}>
          {preview}
        </code>
      </div>
    </div>
  )
}
