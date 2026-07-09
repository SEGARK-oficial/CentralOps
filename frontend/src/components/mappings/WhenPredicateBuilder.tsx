/**
 * WhenPredicateBuilder
 * Editor visual recursivo para o campo `when` de uma ScalarMappingRule.
 *
 * Suporta os 4 operadores que o backend aceita hoje (Fase 2.3):
 *   exists | equals | in | not
 *
 * Profundidade máxima: 4 níveis. Além disso o nó exibe mensagem de erro
 * em vez de renderizar um novo builder — evita UI ilegível.
 */

import type React from "react"
import { useId } from "react"
import { useTranslation } from "react-i18next"
import type { MappingPredicate } from "@/types"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Select } from "@/components/ui/Select/Select"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import { HelpTooltip } from "@/components/ui/HelpTooltip/HelpTooltip"

// ── Types ──────────────────────────────────────────────────────────────────────

type Operator = "exists" | "equals" | "in" | "not"

export interface WhenPredicateBuilderProps {
  value: MappingPredicate | null | undefined
  onChange: (next: MappingPredicate | null) => void
  /** Nível de profundidade atual — controlado internamente na recursão. */
  depth?: number
}

// ── Constants ─────────────────────────────────────────────────────────────────

const MAX_DEPTH = 4

function useOperatorOptions(): { value: Operator; label: string }[] {
  const { t } = useTranslation("mappings")
  return [
    { value: "exists", label: t("whenBuilder.operators.exists.label") },
    { value: "equals", label: t("whenBuilder.operators.equals.label") },
    { value: "in", label: t("whenBuilder.operators.in.label") },
    { value: "not", label: t("whenBuilder.operators.not.label") },
  ]
}

function useOperatorHelp(): Record<Operator, { description: string; example: string }> {
  const { t } = useTranslation("mappings")
  return {
    exists: {
      description: t("whenBuilder.operators.exists.description"),
      example: "data.alert.severity",
    },
    equals: {
      description: t("whenBuilder.operators.equals.description"),
      example: 'source: "severity" / value: "high"',
    },
    in: {
      description: t("whenBuilder.operators.in.description"),
      example: "values: high\ncritical",
    },
    not: {
      description: t("whenBuilder.operators.not.description"),
      example: 'not { exists: "optional_field" }',
    },
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Deriva o operador atual a partir do predicado.
 * Retorna null se o predicado for nulo/indefinido.
 */
function getOperator(pred: MappingPredicate | null | undefined): Operator | null {
  if (pred == null) return null
  if ("exists" in pred) return "exists"
  if ("equals" in pred) return "equals"
  if ("in" in pred) return "in"
  if ("not" in pred) return "not"
  return null
}

/**
 * Faz o melhor esforço pra converter uma string em número.
 * Retorna o número se for parseable como inteiro ou float; string caso contrário.
 * Usado para serializar corretamente os valores de `equals` e `in`.
 */
function tryParseScalar(raw: string): unknown {
  const trimmed = raw.trim()
  if (trimmed === "") return trimmed
  const asNum = Number(trimmed)
  if (!Number.isNaN(asNum) && trimmed !== "") return asNum
  return trimmed
}

/**
 * Converte o valor bruto de `equals.value` para string para exibir no input.
 */
function scalarToString(v: unknown): string {
  if (v == null) return ""
  return String(v)
}

/**
 * Converte o valor bruto de `in.values` para textarea (uma por linha).
 */
function valuesToLines(values: unknown[]): string {
  return values.map((v) => String(v ?? "")).join("\n")
}

/**
 * Converte textarea (uma por linha) para lista de scalares.
 */
function linesToValues(raw: string): unknown[] {
  return raw
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "")
    .map(tryParseScalar)
}

// ── Component ─────────────────────────────────────────────────────────────────

export const WhenPredicateBuilder: React.FC<WhenPredicateBuilderProps> = ({
  value,
  onChange,
  depth = 0,
}) => {
  const { t } = useTranslation("mappings")
  const uid = useId()
  const OPERATOR_OPTIONS = useOperatorOptions()
  const OPERATOR_HELP = useOperatorHelp()

  // Sem predicado: exibe apenas botão "Adicionar condição"
  if (value == null) {
    return (
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => onChange({ exists: "" })}
        data-testid="when-add-condition"
      >
        {t("whenBuilder.addCondition")}
      </Button>
    )
  }

  // Limite de profundidade atingido — guard de segurança
  if (depth >= MAX_DEPTH) {
    return (
      <p
        className="text-xs text-danger-500 italic"
        data-testid="when-max-depth"
        role="alert"
      >
        {t("whenBuilder.maxDepthReached")}
      </p>
    )
  }

  const operator = getOperator(value)

  // Troca de operador: cria um novo predicado com valor padrão para o operador selecionado
  function handleOperatorChange(next: Operator) {
    if (next === "exists") onChange({ exists: "" })
    else if (next === "equals") onChange({ equals: { source: "", value: "" } })
    else if (next === "in") onChange({ in: { source: "", values: [] } })
    else if (next === "not") onChange({ not: { exists: "" } })
  }

  const operatorHelpId = `${uid}-op-help`
  const operatorHelp = operator ? OPERATOR_HELP[operator] : null

  return (
    <div
      className="flex flex-col gap-2 border border-border rounded-md p-2 bg-surface-secondary"
      data-testid="when-predicate-builder"
      data-depth={depth}
    >
      {/* Operador + remover */}
      <div className="flex items-center gap-2">
        <div className="flex-1">
          <Select
            id={`${uid}-operator`}
            aria-label={t("whenBuilder.operatorSelectAriaLabel")}
            options={OPERATOR_OPTIONS}
            value={operator ?? ""}
            onValueChange={(v) => handleOperatorChange(v as Operator)}
            data-testid="when-operator-select"
          />
        </div>
        {operatorHelp && (
          <HelpTooltip
            label={t("whenBuilder.operatorTooltipLabel", { operator })}
            description={operatorHelp.description}
            example={operatorHelp.example}
          />
        )}
        <Button
          type="button"
          variant="ghost"
          size="xs"
          onClick={() => onChange(null)}
          aria-label={t("whenBuilder.removeCondition")}
          data-testid="when-remove-condition"
        >
          {t("common:actions.remove")}
        </Button>
      </div>

      {/* Campos contextuais por operador */}
      {operator === "exists" && (
        <ExistsFields
          uid={uid}
          source={"exists" in value ? (value as { exists: string }).exists : ""}
          onSourceChange={(src) => onChange({ exists: src })}
        />
      )}

      {operator === "equals" && "equals" in value && (
        <EqualsFields
          uid={uid}
          source={(value as { equals: { source: string; value: unknown } }).equals.source}
          equalValue={(value as { equals: { source: string; value: unknown } }).equals.value}
          onSourceChange={(src) =>
            onChange({ equals: { source: src, value: (value as { equals: { source: string; value: unknown } }).equals.value } })
          }
          onValueChange={(val) =>
            onChange({ equals: { source: (value as { equals: { source: string; value: unknown } }).equals.source, value: val } })
          }
        />
      )}

      {operator === "in" && "in" in value && (
        <InFields
          uid={uid}
          source={(value as { in: { source: string; values: unknown[] } }).in.source}
          values={(value as { in: { source: string; values: unknown[] } }).in.values}
          onSourceChange={(src) =>
            onChange({ in: { source: src, values: (value as { in: { source: string; values: unknown[] } }).in.values } })
          }
          onValuesChange={(vals) =>
            onChange({ in: { source: (value as { in: { source: string; values: unknown[] } }).in.source, values: vals } })
          }
        />
      )}

      {operator === "not" && "not" in value && (
        <div className="pl-2 border-l-2 border-border">
          <p className="text-xs text-text-secondary mb-1">{t("whenBuilder.negatedSubCondition")}</p>
          <WhenPredicateBuilder
            value={(value as { not: MappingPredicate }).not}
            onChange={(child) => {
              if (child == null) {
                // Se o filho foi removido, o "not" fica com exists vazio como placeholder
                onChange({ not: { exists: "" } })
              } else {
                onChange({ not: child })
              }
            }}
            depth={depth + 1}
          />
        </div>
      )}

      {/* Erro leve: source vazio para operadores que exigem source */}
      {(operator === "exists" || operator === "equals" || operator === "in") && (() => {
        const src =
          operator === "exists"
            ? ("exists" in value ? (value as { exists: string }).exists : "")
            : operator === "equals"
              ? ("equals" in value ? (value as { equals: { source: string; value: unknown } }).equals.source : "")
              : ("in" in value ? (value as { in: { source: string; values: unknown[] } }).in.source : "")
        return src.trim() === "" ? (
          <p
            id={operatorHelpId}
            className="text-xs text-warning-600"
            role="alert"
            data-testid="when-source-empty-warning"
          >
            {t("whenBuilder.sourceEmptyWarning")}
          </p>
        ) : null
      })()}
    </div>
  )
}

// ── Sub-field components ───────────────────────────────────────────────────────

interface ExistsFieldsProps {
  uid: string
  source: string
  onSourceChange: (src: string) => void
}

const ExistsFields: React.FC<ExistsFieldsProps> = ({ uid, source, onSourceChange }) => {
  const { t } = useTranslation("mappings")
  return (
    <Input
      id={`${uid}-exists-source`}
      aria-label={t("whenBuilder.existsSourceAriaLabel")}
      value={source}
      onChange={(e) => onSourceChange(e.target.value)}
      placeholder="ex: data.alert.severity"
      aria-invalid={source.trim() === "" ? "true" : "false"}
      data-testid="when-exists-source"
    />
  )
}

interface EqualsFieldsProps {
  uid: string
  source: string
  equalValue: unknown
  onSourceChange: (src: string) => void
  onValueChange: (val: unknown) => void
}

const EqualsFields: React.FC<EqualsFieldsProps> = ({
  uid,
  source,
  equalValue,
  onSourceChange,
  onValueChange,
}) => {
  const { t } = useTranslation("mappings")
  return (
    <div className="flex flex-col gap-1.5">
      <Input
        id={`${uid}-equals-source`}
        aria-label={t("whenBuilder.equalsSourceAriaLabel")}
        value={source}
        onChange={(e) => onSourceChange(e.target.value)}
        placeholder="ex: data.severity"
        aria-invalid={source.trim() === "" ? "true" : "false"}
        data-testid="when-equals-source"
      />
      <Input
        id={`${uid}-equals-value`}
        aria-label={t("whenBuilder.equalsValueAriaLabel")}
        value={scalarToString(equalValue)}
        onChange={(e) => {
          // Tenta parsear como número; se não parsear, mantém como string
          onValueChange(tryParseScalar(e.target.value))
        }}
        placeholder={t("whenBuilder.equalsValuePlaceholder")}
        data-testid="when-equals-value"
      />
    </div>
  )
}

interface InFieldsProps {
  uid: string
  source: string
  values: unknown[]
  onSourceChange: (src: string) => void
  onValuesChange: (vals: unknown[]) => void
}

const InFields: React.FC<InFieldsProps> = ({
  uid,
  source,
  values,
  onSourceChange,
  onValuesChange,
}) => {
  const { t } = useTranslation("mappings")
  return (
    <div className="flex flex-col gap-1.5">
      <Input
        id={`${uid}-in-source`}
        aria-label={t("whenBuilder.inSourceAriaLabel")}
        value={source}
        onChange={(e) => onSourceChange(e.target.value)}
        placeholder="ex: data.severity"
        aria-invalid={source.trim() === "" ? "true" : "false"}
        data-testid="when-in-source"
      />
      <Textarea
        id={`${uid}-in-values`}
        aria-label={t("whenBuilder.inValuesAriaLabel")}
        rows={3}
        defaultValue={valuesToLines(values)}
        onChange={(e) => {
          // Parseia cada linha como número quando possível
          onValuesChange(linesToValues(e.target.value))
        }}
        placeholder={"high\ncritical\n4"}
        data-testid="when-in-values"
      />
      <p className="text-xs text-text-tertiary">
        {t("whenBuilder.inValuesHint")}
      </p>
    </div>
  )
}

export default WhenPredicateBuilder
