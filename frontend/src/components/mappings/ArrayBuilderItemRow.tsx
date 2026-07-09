/**
 * ArrayBuilderItemRow
 * Uma linha da tabela de items de um array_builder rule.
 * Colunas: name | type | type_id | source | explode | skip_null | delete
 * Reorder via botões ↑↓.
 */

import type React from "react"
import { useCallback, useId } from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"
import type { ArrayBuilderItem } from "@/types"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { JMESPathInput } from "@/components/mappings/JMESPathInput"

export interface ArrayBuilderItemRowProps {
  item: ArrayBuilderItem
  index: number
  /** skip_null herdado da regra pai — usado para mostrar estado indeterminate */
  ruleSkipNull?: boolean
  onChange: (index: number, updated: ArrayBuilderItem) => void
  onRemove: (index: number) => void
  onMoveUp: (index: number) => void
  onMoveDown: (index: number) => void
  canMoveUp: boolean
  canMoveDown: boolean
  jmespathSuggestions?: string[]
}

export const ArrayBuilderItemRow: React.FC<ArrayBuilderItemRowProps> = ({
  item,
  index,
  ruleSkipNull,
  onChange,
  onRemove,
  onMoveUp,
  onMoveDown,
  canMoveUp,
  canMoveDown,
  jmespathSuggestions = [],
}) => {
  const { t } = useTranslation("mappings")
  const uid = useId()
  const nameId = `${uid}-name`
  const typeId = `${uid}-type`
  const typeIdId = `${uid}-type-id`
  const sourceId = `${uid}-source`
  const explodeId = `${uid}-explode`
  const skipNullId = `${uid}-skip-null`

  const update = useCallback(
    (partial: Partial<ArrayBuilderItem>) => {
      onChange(index, { ...item, ...partial })
    },
    [index, onChange, item],
  )

  const handleRemove = useCallback(() => onRemove(index), [index, onRemove])
  const handleMoveUp = useCallback(() => onMoveUp(index), [index, onMoveUp])
  const handleMoveDown = useCallback(() => onMoveDown(index), [index, onMoveDown])

  // skip_null do item: undefined = herda do pai
  const itemSkipNull = item.skip_null
  const isInherited = itemSkipNull === undefined

  // O checkbox de skip_null pode estar em três estados:
  // - checked: item.skip_null === true
  // - unchecked: item.skip_null === false
  // - indeterminate: item.skip_null === undefined (herda do pai)
  const skipNullChecked = isInherited ? (ruleSkipNull ?? true) : itemSkipNull

  const handleSkipNullChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      if (isInherited) {
        // Primeira interação: sai do indeterminate, seta para o inverso do herdado
        update({ skip_null: !(ruleSkipNull ?? true) })
      } else {
        const next = e.target.checked
        // Se voltou ao mesmo valor do pai, remove o override (volta para indeterminate)
        if (next === (ruleSkipNull ?? true)) {
          const { skip_null: _removed, ...rest } = item
          onChange(index, rest as ArrayBuilderItem)
        } else {
          update({ skip_null: next })
        }
      }
    },
    [isInherited, ruleSkipNull, update, item, index, onChange],
  )

  return (
    <tr
      data-testid={`array-builder-item-${index}`}
      className="border-b border-border last:border-b-0 hover:bg-surface-secondary/50"
    >
      {/* name */}
      <td className="px-2 py-1.5 min-w-[100px]">
        <Input
          id={nameId}
          value={item.name}
          onChange={(e) => update({ name: e.target.value })}
          placeholder="src_ip"
          aria-label={t("arrayBuilder.itemFieldAriaLabel", { index: index + 1, field: "name" })}
          className="h-8 text-xs font-mono"
        />
      </td>

      {/* type (free text) */}
      <td className="px-2 py-1.5 min-w-[110px]">
        <Input
          id={typeId}
          value={item.type}
          onChange={(e) => update({ type: e.target.value })}
          placeholder="IP Address"
          aria-label={t("arrayBuilder.itemFieldAriaLabel", { index: index + 1, field: "type" })}
          className="h-8 text-xs"
        />
      </td>

      {/* type_id */}
      <td className="px-2 py-1.5 min-w-[70px]">
        <Input
          id={typeIdId}
          type="number"
          min={0}
          value={item.type_id}
          onChange={(e) => update({ type_id: Number(e.target.value) })}
          aria-label={t("arrayBuilder.itemFieldAriaLabel", { index: index + 1, field: "type_id" })}
          className="h-8 text-xs"
        />
      </td>

      {/* source */}
      <td className="px-2 py-1.5 min-w-[200px]">
        {jmespathSuggestions.length > 0 ? (
          <JMESPathInput
            id={sourceId}
            value={item.source}
            onChange={(v) => update({ source: v })}
            suggestions={jmespathSuggestions}
            placeholder="ex: data.clientIp"
          />
        ) : (
          <Input
            id={sourceId}
            value={item.source}
            onChange={(e) => update({ source: e.target.value })}
            placeholder="ex: data.clientIp"
            aria-label={t("arrayBuilder.itemFieldAriaLabel", { index: index + 1, field: "source" })}
            className="h-8 text-xs font-mono"
          />
        )}
      </td>

      {/* explode */}
      <td className="px-2 py-1.5 text-center">
        <input
          id={explodeId}
          type="checkbox"
          checked={item.explode ?? false}
          onChange={(e) => update({ explode: e.target.checked })}
          aria-label={t("arrayBuilder.itemFieldAriaLabel", { index: index + 1, field: "explode" })}
          className="h-4 w-4 rounded border-border text-primary-600 cursor-pointer"
        />
      </td>

      {/* skip_null — indeterminate quando herda do pai */}
      <td className="px-2 py-1.5 text-center">
        <input
          id={skipNullId}
          type="checkbox"
          checked={skipNullChecked}
          ref={(el) => {
            if (el) {
              el.indeterminate = isInherited
            }
          }}
          onChange={handleSkipNullChange}
          aria-label={
            isInherited
              ? t("arrayBuilder.skipNullAriaLabelInherited", { index: index + 1 })
              : t("arrayBuilder.itemFieldAriaLabel", { index: index + 1, field: "skip_null" })
          }
          title={isInherited ? t("arrayBuilder.skipNullInheritedTitle", { value: String(ruleSkipNull ?? true) }) : undefined}
          className={cn(
            "h-4 w-4 rounded border-border text-primary-600 cursor-pointer",
            isInherited && "opacity-60",
          )}
        />
      </td>

      {/* reorder + delete */}
      <td className="px-2 py-1.5">
        <div className="flex items-center gap-1 justify-end">
          <Button
            variant="ghost"
            size="xs"
            onClick={handleMoveUp}
            disabled={!canMoveUp}
            aria-label={t("arrayBuilder.moveItemUp", { index: index + 1 })}
            type="button"
          >
            ↑
          </Button>
          <Button
            variant="ghost"
            size="xs"
            onClick={handleMoveDown}
            disabled={!canMoveDown}
            aria-label={t("arrayBuilder.moveItemDown", { index: index + 1 })}
            type="button"
          >
            ↓
          </Button>
          <Button
            variant="danger"
            size="xs"
            onClick={handleRemove}
            aria-label={t("arrayBuilder.removeItem", { index: index + 1 })}
            type="button"
          >
            ✕
          </Button>
        </div>
      </td>
    </tr>
  )
}

export default ArrayBuilderItemRow
