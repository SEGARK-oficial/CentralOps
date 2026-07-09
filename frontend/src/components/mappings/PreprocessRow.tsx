/**
 * PreprocessRow
 * Editor de uma única operação de pré-processamento (PreprocessOp).
 * Segue a mesma densidade visual do RuleRow: borda, header compacto,
 * reorder ↑↓, remoção.
 */

import type React from "react"
import { useCallback, useId, useState } from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import type { PreprocessOp } from "@/types"

// ── Constante de ops disponíveis — adicionar aqui para novos ops ──────────────
export const PREPROCESS_OPS: Array<PreprocessOp["op"]> = ["json_parse"]

// ── Props ─────────────────────────────────────────────────────────────────────

export interface PreprocessRowProps {
  op: PreprocessOp
  index: number
  onChange: (index: number, updated: PreprocessOp) => void
  onRemove: (index: number) => void
  onMoveUp: (index: number) => void
  onMoveDown: (index: number) => void
  canMoveUp: boolean
  canMoveDown: boolean
}

// ── Component ─────────────────────────────────────────────────────────────────

export const PreprocessRow: React.FC<PreprocessRowProps> = ({
  op,
  index,
  onChange,
  onRemove,
  onMoveUp,
  onMoveDown,
  canMoveUp,
  canMoveDown,
}) => {
  const { t } = useTranslation("mappings")
  const uid = useId()
  const opSelectId = `${uid}-op`
  const sourceId = `${uid}-source`
  const targetId = `${uid}-target`
  const tolerantId = `${uid}-tolerant`

  const [targetError, setTargetError] = useState<string | null>(null)

  const update = useCallback(
    (partial: Partial<PreprocessOp>) => {
      onChange(index, { ...op, ...partial })
    },
    [index, onChange, op],
  )

  const handleTargetChange = useCallback(
    (value: string) => {
      if (value && !value.startsWith("_")) {
        setTargetError(t("preprocessRow.targetMustStartWithUnderscore"))
      } else {
        setTargetError(null)
      }
      update({ target: value })
    },
    [update, t],
  )

  const handleRemove = useCallback(() => onRemove(index), [index, onRemove])
  const handleMoveUp = useCallback(() => onMoveUp(index), [index, onMoveUp])
  const handleMoveDown = useCallback(() => onMoveDown(index), [index, onMoveDown])

  return (
    <div
      data-testid={`preprocess-row-${index}`}
      className="border border-border rounded-md bg-surface"
    >
      {/* Header compacto — sempre visível */}
      <div className="flex items-center gap-2 px-3 py-2">
        {/* Op badge (resumo no collapsed) */}
        <span className="font-mono text-xs text-primary-700 shrink-0">{op.op}</span>
        <span className="text-text-tertiary text-xs shrink-0">→</span>
        <span className="font-mono text-xs text-text flex-1 truncate" title={op.target || "—"}>
          {op.target || <span className="text-text-tertiary italic">_campo</span>}
        </span>

        <Button
          variant="danger"
          size="xs"
          onClick={handleRemove}
          type="button"
          aria-label={t("preprocessRow.removeAriaLabel")}
          className="shrink-0"
        >
          {t("common:actions.remove")}
        </Button>
      </div>

      {/* Body — campos sempre visíveis (ops são simples e não usam collapse) */}
      <div className="flex flex-col gap-3 px-3 pb-3 pt-1 border-t border-border">
        {/* op */}
        <div className="flex flex-col gap-1">
          <label htmlFor={opSelectId} className="text-xs font-medium text-text-secondary">
            {t("preprocessRow.operationLabel")}
          </label>
          <select
            id={opSelectId}
            value={op.op}
            onChange={(e) => update({ op: e.target.value as PreprocessOp["op"] })}
            className={cn(
              "h-9 w-full rounded-md border border-border bg-surface px-3",
              "text-sm text-text focus:outline-none focus:ring-2 focus:ring-primary-500",
              "disabled:cursor-not-allowed disabled:opacity-50",
            )}
          >
            {PREPROCESS_OPS.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
        </div>

        {/* source */}
        <div className="flex flex-col gap-1">
          <label htmlFor={sourceId} className="text-xs font-medium text-text-secondary">
            {t("preprocessRow.fields.source")}{" "}
            <span className="font-normal text-text-tertiary">(JMESPath)</span>
          </label>
          <Input
            id={sourceId}
            value={op.source}
            onChange={(e) => update({ source: e.target.value })}
            placeholder="ex: data.raw_json"
            aria-label={t("preprocessRow.sourceAriaLabel")}
          />
        </div>

        {/* target */}
        <div className="flex flex-col gap-1">
          <label htmlFor={targetId} className="text-xs font-medium text-text-secondary">
            {t("preprocessRow.fields.target")}{" "}
            <span className="font-normal text-text-tertiary">{t("preprocessRow.targetHint")}</span>
          </label>
          <Input
            id={targetId}
            value={op.target}
            onChange={(e) => handleTargetChange(e.target.value)}
            placeholder="ex: _parsed"
            aria-label={t("preprocessRow.targetAriaLabel")}
            aria-describedby={targetError ? `${targetId}-error` : undefined}
            aria-invalid={targetError ? true : undefined}
          />
          {targetError && (
            <p
              id={`${targetId}-error`}
              role="alert"
              className="text-xs text-danger-600"
              data-testid={`preprocess-row-${index}-target-error`}
            >
              {targetError}
            </p>
          )}
        </div>

        {/* tolerant */}
        <label className="flex items-center gap-2 cursor-pointer text-sm">
          <input
            id={tolerantId}
            type="checkbox"
            checked={op.tolerant}
            onChange={(e) => update({ tolerant: e.target.checked })}
            className="h-4 w-4 rounded border-border text-primary-600"
            aria-label={t("preprocessRow.tolerantAriaLabel")}
          />
          <span className="text-sm text-text">{t("preprocessEditor.tolerant")}</span>
          <span className="text-xs text-text-tertiary">
            {t("preprocessRow.tolerantHint")}
          </span>
        </label>

        {/* Reorder */}
        <div className="flex items-center gap-1 border-t border-border pt-2">
          <Button
            variant="ghost"
            size="xs"
            onClick={handleMoveUp}
            disabled={!canMoveUp}
            aria-label={t("preprocessRow.moveUp")}
            type="button"
          >
            ↑
          </Button>
          <Button
            variant="ghost"
            size="xs"
            onClick={handleMoveDown}
            disabled={!canMoveDown}
            aria-label={t("preprocessRow.moveDown")}
            type="button"
          >
            ↓
          </Button>
        </div>
      </div>
    </div>
  )
}

export default PreprocessRow
