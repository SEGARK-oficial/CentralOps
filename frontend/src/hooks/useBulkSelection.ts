/**
 * useBulkSelection
 * ----------------
 * Hook genérico de seleção em massa para listas paginadas/filtradas.
 *
 * Conceitos:
 * - "Visíveis" = `visibleItems` passados pelo caller (já filtrados/paginados).
 * - "Selecionável" = item para o qual `isSelectable` retorna true. Itens
 *   não-selecionáveis são ignorados em `toggleAllVisible` e na contagem
 *   usada para o estado do header checkbox.
 * - A seleção persiste IDs entre re-renders, então itens fora da página
 *   atual permanecem selecionados (isso é intencional — o caller decide
 *   se quer limpar entre paginações ou manter).
 *
 * O hook retorna `headerCheckboxState`:
 *   - 'checked'        — todos os visíveis selecionáveis estão selecionados
 *   - 'indeterminate'  — alguns dos visíveis selecionáveis estão selecionados
 *   - 'unchecked'      — nenhum visível selecionável está selecionado
 *
 * `toggleAllVisible`:
 *   - se algum visível-selecionável já está selecionado, remove TODOS os
 *     visíveis-selecionáveis da seleção
 *   - caso contrário, adiciona todos os visíveis-selecionáveis à seleção
 */

import { useCallback, useMemo, useState } from "react"

export type HeaderCheckboxState = "unchecked" | "checked" | "indeterminate"

export interface UseBulkSelectionOptions<T> {
  /** Items currently visible (after filters/pagination). */
  visibleItems: T[]
  /** Stable ID extractor for each item. */
  getId: (item: T) => string
  /** Optional predicate. Items returning false are excluded from
   *  `toggleAllVisible` and from header-state counting. Default: always true. */
  isSelectable?: (item: T) => boolean
}

export interface UseBulkSelectionResult {
  /** Selected ID set (immutable from caller's POV — use the returned helpers). */
  selected: Set<string>
  /** Convenience predicate. */
  isSelected: (id: string) => boolean
  /** Toggle a single id in/out of the selection. */
  toggleOne: (id: string) => void
  /** Bulk-toggle all visible-selectable items.
   *  - If any visible-selectable is selected, REMOVE all visible-selectable.
   *  - Otherwise, ADD all visible-selectable. */
  toggleAllVisible: () => void
  /** Clear the entire selection (including items not in `visibleItems`). */
  clearSelection: () => void
  /** How many of the visible-selectable items are currently selected. */
  selectedVisibleCount: number
  /** How many visible items are selectable (= visibleItems.filter(isSelectable).length). */
  visibleSelectableCount: number
  /** Tri-state for the header checkbox UI. */
  headerCheckboxState: HeaderCheckboxState
}

export function useBulkSelection<T>(
  opts: UseBulkSelectionOptions<T>,
): UseBulkSelectionResult {
  const { visibleItems, getId, isSelectable } = opts

  const [selected, setSelected] = useState<Set<string>>(() => new Set())

  // IDs of currently-visible-AND-selectable items.
  const visibleSelectableIds = useMemo(() => {
    if (!isSelectable) return visibleItems.map(getId)
    const ids: string[] = []
    for (const item of visibleItems) {
      if (isSelectable(item)) ids.push(getId(item))
    }
    return ids
  }, [visibleItems, getId, isSelectable])

  const visibleSelectableCount = visibleSelectableIds.length

  const selectedVisibleCount = useMemo(() => {
    let n = 0
    for (const id of visibleSelectableIds) {
      if (selected.has(id)) n += 1
    }
    return n
  }, [visibleSelectableIds, selected])

  const headerCheckboxState: HeaderCheckboxState = useMemo(() => {
    if (visibleSelectableCount === 0 || selectedVisibleCount === 0) return "unchecked"
    if (selectedVisibleCount >= visibleSelectableCount) return "checked"
    return "indeterminate"
  }, [visibleSelectableCount, selectedVisibleCount])

  const isSelected = useCallback((id: string) => selected.has(id), [selected])

  const toggleOne = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const toggleAllVisible = useCallback(() => {
    setSelected((prev) => {
      // Snapshot the visible-selectable set at call time.
      const next = new Set(prev)
      let anyVisibleSelected = false
      for (const id of visibleSelectableIds) {
        if (next.has(id)) {
          anyVisibleSelected = true
          break
        }
      }
      if (anyVisibleSelected) {
        for (const id of visibleSelectableIds) next.delete(id)
      } else {
        for (const id of visibleSelectableIds) next.add(id)
      }
      return next
    })
  }, [visibleSelectableIds])

  const clearSelection = useCallback(() => {
    setSelected((prev) => (prev.size === 0 ? prev : new Set()))
  }, [])

  return {
    selected,
    isSelected,
    toggleOne,
    toggleAllVisible,
    clearSelection,
    selectedVisibleCount,
    visibleSelectableCount,
    headerCheckboxState,
  }
}

export default useBulkSelection
