/**
 * ArrayBuilderEditor
 * Body de uma regra kind="array_builder".
 * Renderizado pelo RuleRow quando rule.kind === "array_builder".
 *
 * Campos de nível de regra: target, skip_null, dedup_by.
 * Tabela de items com colunas: name, type, type_id, source, explode, skip_null, ações.
 */

import type React from "react"
import { useCallback, useId } from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"
import type { ArrayBuilderRule, ArrayBuilderItem } from "@/types"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { ArrayBuilderItemRow } from "@/components/mappings/ArrayBuilderItemRow"

// ── Props ─────────────────────────────────────────────────────────────────────

export interface ArrayBuilderEditorProps {
  rule: ArrayBuilderRule
  index: number
  onChange: (index: number, updated: ArrayBuilderRule) => void
  jmespathSuggestions?: string[]
}

// ── Default para um novo item ────────────────────────────────────────────────

function newItem(): ArrayBuilderItem {
  return { name: "", type: "", type_id: 0, source: "" }
}

// ── Component ─────────────────────────────────────────────────────────────────

export const ArrayBuilderEditor: React.FC<ArrayBuilderEditorProps> = ({
  rule,
  index,
  onChange,
  jmespathSuggestions = [],
}) => {
  const { t } = useTranslation("mappings")
  const uid = useId()
  const targetId = `${uid}-target`
  const skipNullId = `${uid}-skip-null`
  const dedupById = `${uid}-dedup-by`

  const update = useCallback(
    (partial: Partial<ArrayBuilderRule>) => {
      onChange(index, { ...rule, ...partial })
    },
    [index, onChange, rule],
  )

  // ── dedup_by — comma-separated → string[] ─────────────────────────────────

  const dedupByDisplay = (rule.dedup_by ?? []).join(", ")

  const handleDedupByChange = useCallback(
    (raw: string) => {
      const parsed = raw
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean)
      update({ dedup_by: parsed.length > 0 ? parsed : undefined })
    },
    [update],
  )

  // ── Items handlers ─────────────────────────────────────────────────────────

  const handleItemChange = useCallback(
    (itemIndex: number, updated: ArrayBuilderItem) => {
      const next = [...rule.items]
      next[itemIndex] = updated
      update({ items: next })
    },
    [rule.items, update],
  )

  const handleItemRemove = useCallback(
    (itemIndex: number) => {
      update({ items: rule.items.filter((_, i) => i !== itemIndex) })
    },
    [rule.items, update],
  )

  const handleItemMoveUp = useCallback(
    (itemIndex: number) => {
      if (itemIndex === 0) return
      const next = [...rule.items]
      ;[next[itemIndex - 1], next[itemIndex]] = [next[itemIndex], next[itemIndex - 1]]
      update({ items: next })
    },
    [rule.items, update],
  )

  const handleItemMoveDown = useCallback(
    (itemIndex: number) => {
      if (itemIndex === rule.items.length - 1) return
      const next = [...rule.items]
      ;[next[itemIndex], next[itemIndex + 1]] = [next[itemIndex + 1], next[itemIndex]]
      update({ items: next })
    },
    [rule.items, update],
  )

  function handleAddItem() {
    update({ items: [...rule.items, newItem()] })
  }

  // skip_null default true (backend default)
  const skipNull = rule.skip_null ?? true

  return (
    <div
      className="flex flex-col gap-3 px-3 pb-3 pt-1 border-t border-border"
      data-testid="array-builder-editor"
    >
      {/* ── Campos de nível de regra ─────────────────────────────────────── */}

      <div className="flex flex-wrap gap-3 items-end">
        {/* target */}
        <div className="flex flex-col gap-1 flex-1 min-w-[200px]">
          <label
            htmlFor={targetId}
            className="text-xs font-medium text-text-secondary"
          >
            target
            <span className="ml-1 text-danger-500" aria-label={t("common:states.required")}>*</span>
          </label>
          <Input
            id={targetId}
            value={rule.target}
            onChange={(e) => update({ target: e.target.value })}
            placeholder="ex: normalized.observables"
            className="h-8 text-xs font-mono"
          />
        </div>

        {/* dedup_by */}
        <div className="flex flex-col gap-1 flex-1 min-w-[200px]">
          <label
            htmlFor={dedupById}
            className="text-xs font-medium text-text-secondary"
          >
            dedup_by
          </label>
          <Input
            id={dedupById}
            value={dedupByDisplay}
            onChange={(e) => handleDedupByChange(e.target.value)}
            placeholder="value"
            className="h-8 text-xs"
            aria-describedby={`${dedupById}-help`}
          />
          <p
            id={`${dedupById}-help`}
            className="text-xs text-text-tertiary"
          >
            {t("arrayBuilder.dedupByHint")}
          </p>
        </div>

        {/* skip_null */}
        <label
          className="flex items-center gap-2 cursor-pointer text-sm shrink-0 pb-1"
        >
          <input
            id={skipNullId}
            type="checkbox"
            checked={skipNull}
            onChange={(e) => update({ skip_null: e.target.checked })}
            className="h-4 w-4 rounded border-border text-primary-600"
            aria-label={t("arrayBuilder.skipNullAriaLabel")}
          />
          <span className="text-sm text-text">skip_null</span>
          <span className="text-xs text-text-tertiary">{t("arrayBuilder.skipNullHint")}</span>
        </label>
      </div>

      {/* ── Tabela de items ─────────────────────────────────────────────── */}

      <div className="flex flex-col gap-2">
        <span className="text-xs font-semibold text-text-secondary uppercase tracking-wide">
          {t("arrayBuilder.itemsCount", { count: rule.items.length })}
        </span>

        {rule.items.length > 0 ? (
          <div className="overflow-x-auto rounded-md border border-border">
            <table className="w-full text-xs border-collapse min-w-[640px]">
              <thead>
                <tr className="bg-surface-secondary border-b border-border">
                  <th
                    scope="col"
                    className={cn(
                      "px-2 py-1.5 text-left text-xs font-semibold text-text-secondary",
                      "min-w-[100px]",
                    )}
                  >
                    name
                  </th>
                  <th
                    scope="col"
                    className={cn(
                      "px-2 py-1.5 text-left text-xs font-semibold text-text-secondary",
                      "min-w-[110px]",
                    )}
                  >
                    type
                  </th>
                  <th
                    scope="col"
                    className="px-2 py-1.5 text-left text-xs font-semibold text-text-secondary min-w-[70px]"
                  >
                    type_id
                  </th>
                  <th
                    scope="col"
                    className="px-2 py-1.5 text-left text-xs font-semibold text-text-secondary min-w-[200px]"
                  >
                    {t("arrayBuilder.columns.source")}
                  </th>
                  <th
                    scope="col"
                    className="px-2 py-1.5 text-center text-xs font-semibold text-text-secondary"
                    title={t("arrayBuilder.columns.explodeTitle")}
                  >
                    explode
                  </th>
                  <th
                    scope="col"
                    className="px-2 py-1.5 text-center text-xs font-semibold text-text-secondary"
                    title={t("arrayBuilder.columns.skipNullTitle")}
                  >
                    skip_null
                  </th>
                  <th scope="col" className="px-2 py-1.5 text-right text-xs font-semibold text-text-secondary">
                    ações
                  </th>
                </tr>
              </thead>
              <tbody>
                {rule.items.map((item, itemIndex) => (
                  <ArrayBuilderItemRow
                    key={itemIndex}
                    item={item}
                    index={itemIndex}
                    ruleSkipNull={skipNull}
                    onChange={handleItemChange}
                    onRemove={handleItemRemove}
                    onMoveUp={handleItemMoveUp}
                    onMoveDown={handleItemMoveDown}
                    canMoveUp={itemIndex > 0}
                    canMoveDown={itemIndex < rule.items.length - 1}
                    jmespathSuggestions={jmespathSuggestions}
                  />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div
            className="rounded-md border border-dashed border-border py-4 text-center text-xs text-text-tertiary"
            data-testid="array-builder-empty-items"
          >
            {t("arrayBuilder.emptyItems")}
          </div>
        )}

        <Button
          variant="outline"
          size="xs"
          onClick={handleAddItem}
          type="button"
          className="self-start mt-1"
          data-testid="add-array-builder-item"
        >
          {t("arrayBuilder.addItem")}
        </Button>
      </div>
    </div>
  )
}

export default ArrayBuilderEditor
