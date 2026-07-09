/**
 * RuleRow
 * Linha de uma regra de mapping — suporta modo view (read-only) e edit.
 * Sprint 2: inputs editáveis, botões ↑ ↓ Remover.
 * Sprint 3: tooltips de ajuda em edit mode, controlled expanded via props.
 */

import type React from "react"
import { memo, useCallback, useMemo, useState } from "react"
import { ChevronDownIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"
import type { MappingRule, ArrayBuilderRule } from "@/types"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Select } from "@/components/ui/Select/Select"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import { HelpTooltip } from "@/components/ui/HelpTooltip/HelpTooltip"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { JMESPathInput } from "@/components/mappings/JMESPathInput"
import { ArrayBuilderEditor } from "@/components/mappings/ArrayBuilderEditor"
import { WhenPredicateBuilder } from "@/components/mappings/WhenPredicateBuilder"
import { DOCS } from "@/lib/docs"
import { useTypeCasts } from "@/hooks/useTypeCasts"

const LEARN_MORE = DOCS.ruleAnatomy

// ── Required marker (i18n aria-label) ────────────────────────────────────────

const RequiredMark: React.FC = () => {
  const { t } = useTranslation("mappings")
  return (
    <span className="text-danger-500" aria-label={t("common:states.required")}>
      *
    </span>
  )
}

// ── Shared label + tooltip component ─────────────────────────────────────────

interface FieldLabelProps {
  htmlFor: string
  text: string
  required?: boolean
  tooltipLabel: string
  tooltipDescription: string
  tooltipExample?: string
}

const FieldLabel: React.FC<FieldLabelProps> = ({
  htmlFor,
  text,
  required,
  tooltipLabel,
  tooltipDescription,
  tooltipExample,
}) => (
  <label
    htmlFor={htmlFor}
    className="text-sm font-medium text-text flex items-center gap-1"
  >
    {text}
    {required && (
      <RequiredMark />
    )}
    <HelpTooltip
      label={tooltipLabel}
      description={tooltipDescription}
      example={tooltipExample}
      learnMoreHref={LEARN_MORE}
    />
  </label>
)

// ── Types ─────────────────────────────────────────────────────────────────────

interface RuleRowViewProps {
  rule: MappingRule
  mode: "view"
  expanded?: boolean
  onToggleExpand?: () => void
}

interface RuleRowEditProps {
  rule: MappingRule
  mode: "edit"
  /**
   * Index desta regra no array pai — passado de volta nos callbacks para
   * evitar closures inline no RulesEditor que causam re-renders em cascata.
   */
  index: number
  onChange: (index: number, updated: MappingRule) => void
  onRemove: (index: number) => void
  onMoveUp: (index: number) => void
  onMoveDown: (index: number) => void
  canMoveUp?: boolean
  canMoveDown?: boolean
  /** Controlled expanded state. Se omitido, inicia colapsado e gerencia
   *  internamente. Quando passado, o pai controla. */
  expanded?: boolean
  onToggleExpand?: () => void
  /**
   * Sugestões de campos JMESPath para autocomplete no input "source".
   * Obtidas via GET /mappings/{id}/discover-fields e passadas pelo pai
   * (RulesEditor → MappingEditorPage). Quando vazio, o input se comporta
   * como texto livre normal — sem dropdown, sem erro.
   */
  jmespathSuggestions?: string[]
}

type RuleRowProps = RuleRowViewProps | RuleRowEditProps

// ── RuleRowView (read-only) ───────────────────────────────────────────────────

const RuleRowViewInner: React.FC<RuleRowViewProps> = ({
  rule,
  expanded: controlledExpanded,
  onToggleExpand,
}) => {
  const { t } = useTranslation("mappings")
  const [internalExpanded, setInternalExpanded] = useState(false)

  const isControlled = controlledExpanded !== undefined
  const expanded = isControlled ? controlledExpanded : internalExpanded

  function handleToggle() {
    if (isControlled && onToggleExpand) {
      onToggleExpand()
    } else {
      setInternalExpanded((prev) => !prev)
    }
  }

  const isArrayBuilder = rule.kind === "array_builder"

  const sourceDisplay =
    isArrayBuilder
      ? null
      : rule.source != null
        ? rule.source
        : rule.const !== undefined
          ? JSON.stringify(rule.const)
          : "—"

  const hasDetails = isArrayBuilder
    ? false // view de array_builder é apenas o header — não expande nada extra
    : rule.value_map != null || rule.type_cast != null || rule.default !== undefined

  return (
    <div
      data-testid={`rule-row-${rule.target}`}
      className="border border-border rounded-md bg-surface"
    >
      <div className="flex items-center gap-2 px-3 py-2">
        <span className="font-mono text-xs text-text min-w-0 flex-1 truncate" title={rule.target}>
          {rule.target}
        </span>
        <span className="text-text-tertiary text-xs shrink-0">←</span>
        {isArrayBuilder ? (
          <span className="text-text-tertiary text-xs min-w-0 flex-1 truncate italic">
            {t("ruleRow.arrayPlaceholder")}
          </span>
        ) : (
          <span
            className={cn(
              "font-mono text-xs min-w-0 flex-1 truncate",
              rule.source != null ? "text-primary-700" : "text-text-secondary",
            )}
            title={sourceDisplay ?? "—"}
          >
            {sourceDisplay}
          </span>
        )}

        <div className="flex items-center gap-1 shrink-0">
          {isArrayBuilder && (
            <Badge
              variant="success"
              size="sm"
              data-testid="array-builder-chip"
            >
              {t("ruleRow.arrayBuilderChip", { count: rule.items.length })}
            </Badge>
          )}
          {!isArrayBuilder && rule.required && (
            <Badge variant="danger" size="sm">{t("common:states.required")}</Badge>
          )}
          {!isArrayBuilder && rule.type_cast && (
            <Badge variant="primary" size="sm">{rule.type_cast}</Badge>
          )}
          {!isArrayBuilder && rule.value_map != null && (
            <Badge variant="warning" size="sm">value_map</Badge>
          )}
        </div>

        {hasDetails && (
          <button
            type="button"
            aria-expanded={expanded}
            aria-label={expanded ? t("ruleRow.collapseDetails") : t("ruleRow.expandDetails")}
            onClick={handleToggle}
            className="text-text-tertiary hover:text-text transition-colors focus-visible:outline-2 focus-visible:outline-primary-500 rounded"
          >
            <ChevronDownIcon
              size={14}
              className={cn("transition-transform", expanded && "rotate-180")}
            />
          </button>
        )}
      </div>

      {hasDetails && expanded && (
        <div className="border-t border-border bg-surface-secondary px-3 py-2 rounded-b-md space-y-1">
          {!isArrayBuilder && rule.default !== undefined && (
            <div className="flex gap-2 text-xs">
              <span className="text-text-secondary font-medium w-16 shrink-0">{t("ruleRow.defaultLabel")}</span>
              <span className="font-mono text-text">{JSON.stringify(rule.default)}</span>
            </div>
          )}
          {!isArrayBuilder && rule.type_cast && (
            <div className="flex gap-2 text-xs">
              <span className="text-text-secondary font-medium w-16 shrink-0">{t("ruleRow.castLabel")}</span>
              <span className="font-mono text-primary-700">{rule.type_cast}</span>
            </div>
          )}
          {!isArrayBuilder && rule.value_map != null && (
            <div className="flex gap-2 text-xs">
              <span className="text-text-secondary font-medium w-16 shrink-0">{t("ruleRow.valueMapLabel")}</span>
              <pre className="font-mono text-text-secondary text-xs overflow-auto">
                {JSON.stringify(rule.value_map, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

const RuleRowView = memo(RuleRowViewInner)
RuleRowView.displayName = "RuleRowView"

// ── RuleRowEdit ───────────────────────────────────────────────────────────────

const RuleRowEditInner: React.FC<RuleRowEditProps> = ({
  rule,
  index,
  onChange,
  onRemove,
  onMoveUp,
  onMoveDown,
  canMoveUp,
  canMoveDown,
  expanded: controlledExpanded,
  onToggleExpand,
  jmespathSuggestions = [],
}) => {
  const { t } = useTranslation("mappings")
  const [internalExpanded, setInternalExpanded] = useState(false)
  const [valuemapError, setValuemapError] = useState<string | null>(null)
  const [confirmRemoveOpen, setConfirmRemoveOpen] = useState(false)

  const { data: typeCasts, loading: typeCastsLoading, error: typeCastsError } = useTypeCasts()

  const typeCastOptions = useMemo(() => {
    if (typeCastsError || typeCastsLoading || typeCasts === null) return []
    return [
      { value: "", label: t("ruleRow.typeCastNone") },
      ...typeCasts.map((cast) => ({ value: cast.name, label: cast.name })),
    ]
  }, [typeCasts, typeCastsLoading, typeCastsError, t])

  const typeCastPlaceholder = typeCastsLoading
    ? t("ruleRow.loading")
    : typeCastsError
      ? t("ruleRow.loadError")
      : t("ruleRow.typeCastNone")

  const isArrayBuilder = rule.kind === "array_builder"

  // Narrow para ScalarMappingRule para acesso seguro aos campos scalar.
  // Usado em hooks e em derivados — não torna a branch segura no JSX sozinho.
  const scalarRuleRef = !isArrayBuilder
    ? (rule as import("@/types").ScalarMappingRule)
    : null

  // Tooltip para o cast selecionado (description + signature) — apenas scalar.
  const selectedCastDescriptor = useMemo(() => {
    if (!scalarRuleRef?.type_cast || !typeCasts) return null
    return typeCasts.find((c) => c.name === scalarRuleRef.type_cast) ?? null
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isArrayBuilder, scalarRuleRef?.type_cast, typeCasts])

  // Derivado da rule, não estado local — apenas usado em scalar
  const sourceType: "source" | "const" =
    scalarRuleRef?.const !== undefined ? "const" : "source"

  const isControlled = controlledExpanded !== undefined
  const editExpanded = isControlled ? controlledExpanded : internalExpanded
  const handleEditToggle = isControlled
    ? onToggleExpand
    : () => setInternalExpanded((prev) => !prev)

  const update = useCallback(
    (partial: Partial<MappingRule>) => {
      onChange(index, { ...rule, ...partial } as MappingRule)
    },
    [index, onChange, rule],
  )

  const handleSourceTypeChange = useCallback(
    (type: "source" | "const") => {
      // Only valid for scalar rules — guard is enforced by the caller
      const r = rule as import("@/types").ScalarMappingRule
      if (type === "source") {
        // eslint-disable-next-line @typescript-eslint/no-unused-vars
        const { const: _removed, ...rest } = r
        onChange(index, { ...rest, source: "" } as MappingRule)
      } else {
        // eslint-disable-next-line @typescript-eslint/no-unused-vars
        const { source: _removed, ...rest } = r
        onChange(index, { ...rest, const: "" } as MappingRule)
      }
    },
    [index, onChange, rule],
  )

  const handleValueMapChange = useCallback(
    (raw: string) => {
      if (!raw.trim()) {
        setValuemapError(null)
        // eslint-disable-next-line @typescript-eslint/no-unused-vars
        const { value_map: _removed, ...rest } = rule as import("@/types").ScalarMappingRule
        onChange(index, { ...rest })
        return
      }
      try {
        const parsed = JSON.parse(raw)
        setValuemapError(null)
        update({ value_map: parsed })
      } catch {
        setValuemapError(t("ruleRow.invalidValueMap"))
      }
    },
    [index, onChange, rule, update, t],
  )

  const handleRequestRemove = useCallback(() => setConfirmRemoveOpen(true), [])
  const handleConfirmRemove = useCallback(() => {
    setConfirmRemoveOpen(false)
    onRemove(index)
  }, [index, onRemove])
  const handleMoveUp = useCallback(() => onMoveUp(index), [index, onMoveUp])
  const handleMoveDown = useCallback(() => onMoveDown(index), [index, onMoveDown])

  // Stable IDs baseados no target (target é estável por linha)
  const targetId = `target-${rule.target}`
  const sourceId = `source-${rule.target}`
  const constId = `const-${rule.target}`
  const defaultId = `default-${rule.target}`
  const valuemapId = `valuemap-${rule.target}`
  const typeCastId = `typecast-${rule.target}`

  const sourcePreview = isArrayBuilder
    ? ""
    : sourceType === "source"
      ? (rule as { source?: string | null }).source ?? ""
      : (rule as { const?: unknown }).const !== undefined
        ? String((rule as { const?: unknown }).const)
        : ""

  return (
    <div
      data-testid={`rule-row-${rule.target}`}
      className={cn(
        "border rounded-md bg-surface",
        editExpanded ? "border-primary-300" : "border-border",
      )}
    >
      {/* Header compacto — sempre visível.
          flex-wrap permite quebrar em mobile quando badges+botão não cabem. */}
      <div className="flex flex-wrap items-center gap-2 px-3 py-2">
        <button
          type="button"
          aria-expanded={editExpanded}
          aria-label={editExpanded ? t("ruleRow.collapseRule") : t("ruleRow.expandRule")}
          onClick={handleEditToggle}
          className="text-text-tertiary hover:text-text transition-colors focus-visible:outline-2 focus-visible:outline-primary-500 rounded shrink-0"
        >
          <ChevronDownIcon
            size={14}
            className={cn("transition-transform", !editExpanded && "-rotate-90")}
          />
        </button>
        {/* min-w-0 + flex-1 para que truncate funcione dentro do flex */}
        <span className="font-mono text-xs text-text min-w-0 flex-1 truncate" title={rule.target}>
          {rule.target || <span className="text-text-tertiary italic">{t("ruleRow.newFieldPlaceholder")}</span>}
        </span>
        <span className="text-text-tertiary text-xs shrink-0">←</span>
        {isArrayBuilder ? (
          <span className="text-text-tertiary text-xs min-w-0 flex-1 truncate italic">{t("ruleRow.arrayPlaceholder")}</span>
        ) : (
          <span
            className={cn(
              "font-mono text-xs min-w-0 flex-1 truncate",
              sourcePreview ? "text-primary-700" : "text-text-tertiary",
            )}
            title={sourcePreview || "—"}
          >
            {sourcePreview || "—"}
          </span>
        )}
        <div className="flex flex-wrap items-center gap-1 shrink-0">
          {isArrayBuilder && (
            <Badge
              variant="success"
              size="sm"
              data-testid="array-builder-chip"
            >
              {t("ruleRow.arrayBuilderChip", { count: rule.items.length })}
            </Badge>
          )}
          {!isArrayBuilder && rule.required && <Badge variant="danger" size="sm">{t("common:states.required")}</Badge>}
          {!isArrayBuilder && rule.type_cast && <Badge variant="primary" size="sm">{rule.type_cast}</Badge>}
          {!isArrayBuilder && rule.value_map != null && <Badge variant="warning" size="sm">value_map</Badge>}
        </div>
        <Button
          variant="danger"
          size="xs"
          onClick={handleRequestRemove}
          type="button"
          className="shrink-0"
        >
          {t("common:actions.remove")}
        </Button>
      </div>

      <ConfirmDialog
        open={confirmRemoveOpen}
        title={t("ruleRow.removeConfirm.title")}
        description={
          <>
            {t("ruleRow.removeConfirm.before")}{" "}
            <span className="font-mono text-text">
              {rule.target || t("ruleRow.newFieldPlaceholder")}
            </span>{" "}
            {t("ruleRow.removeConfirm.after")}
          </>
        }
        confirmLabel={t("common:actions.remove")}
        cancelLabel={t("common:actions.cancel")}
        confirmVariant="danger"
        onConfirm={handleConfirmRemove}
        onClose={() => setConfirmRemoveOpen(false)}
      />

      {/* Body array_builder — delegado ao ArrayBuilderEditor */}
      {isArrayBuilder && editExpanded && (
        <ArrayBuilderEditor
          rule={rule as ArrayBuilderRule}
          index={index}
          onChange={(i, updated) => onChange(i, updated)}
          jmespathSuggestions={jmespathSuggestions}
        />
      )}

      {/* Body scalar — só renderiza quando expandido e não é array_builder */}
      {!isArrayBuilder && editExpanded && (() => {
        // Narrow to ScalarMappingRule so scalar-only fields are accessible.
        const scalarRule = rule as import("@/types").ScalarMappingRule
        return (
        <div className="flex flex-col">

          {/* Seção 1: Identificação */}
          <fieldset className="border-t border-border pt-3 mt-1 pb-1 px-3">
            <legend className="text-xs font-semibold text-text-secondary uppercase tracking-wide px-1">
              {t("ruleRow.sections.identification")}
            </legend>
            <div className="flex flex-col gap-3 mt-2">
              {/* target */}
              <div className="flex flex-col gap-1.5">
                <FieldLabel
                  htmlFor={targetId}
                  text="target"
                  required
                  tooltipLabel="target"
                  tooltipDescription={t("ruleRow.tooltips.target.description")}
                  tooltipExample="normalized.severity_id"
                />
                <Input
                  id={targetId}
                  value={rule.target}
                  onChange={(e) => update({ target: e.target.value })}
                  placeholder={t("ruleRow.placeholders.target")}
                  className="w-full"
                />
              </div>
            </div>
          </fieldset>

          {/* Seção 2: Origem do valor */}
          <fieldset className="border-t border-border pt-3 mt-1 pb-1 px-3">
            <legend className="text-xs font-semibold text-text-secondary uppercase tracking-wide px-1">
              {t("ruleRow.sections.valueOrigin")}
            </legend>
            <div className="flex flex-col gap-3 mt-2">
              {/* source vs const toggle */}
              <div className="flex flex-col gap-1.5">
                <span className="text-sm font-medium text-text flex items-center gap-1">
                  {t("ruleRow.sourceType.label")}
                  <HelpTooltip
                    label={t("ruleRow.sourceType.label")}
                    description={t("ruleRow.sourceType.description")}
                    learnMoreHref={LEARN_MORE}
                  />
                </span>
                <div
                  className="flex gap-4"
                  role="radiogroup"
                  aria-label={t("ruleRow.sourceType.radioGroupAriaLabel")}
                >
                  <label className="flex items-center gap-1.5 cursor-pointer text-sm">
                    <input
                      type="radio"
                      name={`source-type-${rule.target}`}
                      value="source"
                      checked={sourceType === "source"}
                      onChange={() => handleSourceTypeChange("source")}
                      className="h-4 w-4 text-primary-600"
                    />
                    {t("ruleRow.sourceType.sourceOption")}
                  </label>
                  <label className="flex items-center gap-1.5 cursor-pointer text-sm">
                    <input
                      type="radio"
                      name={`source-type-${rule.target}`}
                      value="const"
                      checked={sourceType === "const"}
                      onChange={() => handleSourceTypeChange("const")}
                      className="h-4 w-4 text-primary-600"
                    />
                    {t("ruleRow.sourceType.constOption")}
                  </label>
                </div>
              </div>

              {/* source input */}
              {sourceType === "source" && (
                <div className="flex flex-col gap-1.5">
                  <FieldLabel
                    htmlFor={sourceId}
                    text={t("ruleRow.fields.source")}
                    tooltipLabel={t("ruleRow.fields.source")}
                    tooltipDescription={t("ruleRow.tooltips.source.description")}
                    tooltipExample="data.alert.severity"
                  />
                  {jmespathSuggestions.length > 0 ? (
                    <JMESPathInput
                      id={sourceId}
                      value={scalarRule.source ?? ""}
                      onChange={(v) => update({ source: v || null })}
                      suggestions={jmespathSuggestions}
                      placeholder={t("ruleRow.placeholders.source")}
                    />
                  ) : (
                    <Input
                      id={sourceId}
                      value={scalarRule.source ?? ""}
                      onChange={(e) => update({ source: e.target.value || null })}
                      placeholder={t("ruleRow.placeholders.source")}
                      className="w-full"
                    />
                  )}
                </div>
              )}

              {/* const input */}
              {sourceType === "const" && (
                <div className="flex flex-col gap-1.5">
                  <FieldLabel
                    htmlFor={constId}
                    text="const"
                    tooltipLabel="const"
                    tooltipDescription={t("ruleRow.tooltips.const.description")}
                    tooltipExample="Sophos Central"
                  />
                  <Input
                    id={constId}
                    value={scalarRule.const !== undefined ? String(scalarRule.const) : ""}
                    onChange={(e) => update({ const: e.target.value })}
                    placeholder={t("ruleRow.placeholders.const")}
                    className="w-full"
                  />
                </div>
              )}
            </div>
          </fieldset>

          {/* Seção 3: Transformação */}
          <fieldset className="border-t border-border pt-3 mt-1 pb-1 px-3">
            <legend className="text-xs font-semibold text-text-secondary uppercase tracking-wide px-1">
              {t("ruleRow.sections.transformation")}
            </legend>
            <div className="flex flex-col gap-3 mt-2">
              {/* default + type_cast lado a lado em md+ */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div className="flex flex-col gap-1.5">
                  <FieldLabel
                    htmlFor={defaultId}
                    text={t("ruleRow.fields.defaultOptional")}
                    tooltipLabel="default"
                    tooltipDescription={t("ruleRow.tooltips.default.description")}
                    tooltipExample="0"
                  />
                  <Input
                    id={defaultId}
                    value={scalarRule.default !== undefined ? String(scalarRule.default) : ""}
                    onChange={(e) => update({ default: e.target.value || undefined })}
                    placeholder={t("ruleRow.placeholders.default")}
                    className="w-full"
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <FieldLabel
                    htmlFor={typeCastId}
                    text="type_cast"
                    tooltipLabel="type_cast"
                    tooltipDescription={t("ruleRow.tooltips.typeCast.description")}
                    tooltipExample="iso_to_epoch"
                  />
                  <Select
                    id={typeCastId}
                    options={typeCastOptions}
                    value={scalarRule.type_cast ?? ""}
                    disabled={typeCastsLoading || !!typeCastsError}
                    placeholder={typeCastPlaceholder}
                    onValueChange={(v) => {
                      const cast = (v as string) || null
                      update({ type_cast: cast })
                    }}
                  />
                  {selectedCastDescriptor && (
                    <p
                      className="text-xs text-text-secondary"
                      title={t("ruleRow.castSignatureTitle", { description: selectedCastDescriptor.description, signature: selectedCastDescriptor.signature })}
                    >
                      <span className="font-mono text-text">{selectedCastDescriptor.signature}</span>
                      {" — "}
                      {selectedCastDescriptor.description}
                    </p>
                  )}
                </div>
              </div>

              {/* value_map — full width abaixo */}
              <div className="flex flex-col gap-1.5">
                <FieldLabel
                  htmlFor={valuemapId}
                  text={t("ruleRow.fields.valueMapOptional")}
                  tooltipLabel="value_map"
                  tooltipDescription={t("ruleRow.tooltips.valueMap.description")}
                  tooltipExample='{"high": 4, "medium": 3, "low": 1}'
                />
                <Textarea
                  id={valuemapId}
                  rows={2}
                  defaultValue={scalarRule.value_map != null ? JSON.stringify(scalarRule.value_map, null, 2) : ""}
                  onChange={(e) => handleValueMapChange(e.target.value)}
                  error={valuemapError ?? undefined}
                  placeholder='{"original": "mapeado"}'
                />
              </div>
            </div>
          </fieldset>

          {/* Seção 4: Validação */}
          <fieldset className="border-t border-border pt-3 mt-1 pb-1 px-3">
            <legend className="text-xs font-semibold text-text-secondary uppercase tracking-wide px-1">
              {t("ruleRow.sections.validation")}
            </legend>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-2">
              {/* required toggle */}
              <label className="flex items-center gap-2 cursor-pointer text-sm">
                <input
                  type="checkbox"
                  checked={scalarRule.required ?? false}
                  onChange={(e) => update({ required: e.target.checked })}
                  className="h-4 w-4 rounded border-border text-primary-600"
                />
                {t("common:states.required")}
                <HelpTooltip
                  label={t("common:states.required")}
                  description={t("ruleRow.tooltips.required.description")}
                  learnMoreHref={LEARN_MORE}
                />
              </label>

              {/* expected_always_default toggle */}
              <label
                htmlFor={`expected-always-default-${rule.target}`}
                className="flex items-center gap-2 cursor-pointer text-sm"
              >
                <input
                  id={`expected-always-default-${rule.target}`}
                  type="checkbox"
                  checked={scalarRule.expected_always_default ?? false}
                  onChange={(e) => update({ expected_always_default: e.target.checked })}
                  className="h-4 w-4 rounded border-border text-primary-600"
                />
                {t("ruleRow.intentionalDefault.label")}
                <HelpTooltip
                  label={t("ruleRow.intentionalDefault.label")}
                  description={t("ruleRow.intentionalDefault.description")}
                  learnMoreHref={LEARN_MORE}
                />
              </label>
            </div>
          </fieldset>

          {/* Seção 5: Condicional */}
          <fieldset className="border-t border-border pt-3 mt-1 pb-1 px-3">
            <legend className="text-xs font-semibold text-text-secondary uppercase tracking-wide px-1">
              {t("ruleRow.sections.conditional")}
            </legend>
            <div className="flex flex-col gap-1.5 mt-2">
              <span className="text-sm font-medium text-text flex items-center gap-1">
                {t("ruleRow.whenCondition.label")}
                <HelpTooltip
                  label={t("ruleRow.whenCondition.label")}
                  description={t("ruleRow.whenCondition.description")}
                  example='{ exists: "data.severity" }'
                  learnMoreHref={LEARN_MORE}
                />
              </span>
              <WhenPredicateBuilder
                value={scalarRule.when ?? null}
                onChange={(next) => {
                  // Usar undefined (não null) para não serializar a chave no JSON final.
                  // O spread em `update` preserva chaves undefined — setamos explicitamente
                  // para forçar a remoção via re-criação do objeto.
                  if (next === null) {
                    const { when: _removed, ...rest } = scalarRule
                    onChange(index, { ...rest } as MappingRule)
                  } else {
                    update({ when: next })
                  }
                }}
              />
            </div>
          </fieldset>

          {/* Reorder actions — botão Remover já está no header */}
          <div className="flex items-center gap-1 border-t border-border pt-2 px-3 pb-2 mt-2">
            <Button
              variant="ghost"
              size="xs"
              onClick={handleMoveUp}
              disabled={!canMoveUp}
              aria-label={t("ruleRow.moveUp")}
              type="button"
            >
              ↑
            </Button>
            <Button
              variant="ghost"
              size="xs"
              onClick={handleMoveDown}
              disabled={!canMoveDown}
              aria-label={t("ruleRow.moveDown")}
              type="button"
            >
              ↓
            </Button>
          </div>
        </div>
        )
      })()}
    </div>
  )
}

const RuleRowEdit = memo(RuleRowEditInner)
RuleRowEdit.displayName = "RuleRowEdit"

// ── RuleRow dispatcher ────────────────────────────────────────────────────────

const RuleRowInner: React.FC<RuleRowProps> = (props) => {
  if (props.mode === "view") {
    return <RuleRowView {...props} />
  }
  return <RuleRowEdit {...props} />
}

export const RuleRow = memo(RuleRowInner)
RuleRow.displayName = "RuleRow"

export default RuleRow
