/**
 * Testes de useBulkSelection — cobre comportamento de seleção em massa
 * com filtragem, tri-state header, isSelectable opt-out e clear.
 */

import { act, renderHook } from "@testing-library/react"
import { useBulkSelection } from "@/hooks/useBulkSelection"

interface Item {
  id: string
  kind?: "tenant" | "partner"
}

const a: Item = { id: "a", kind: "tenant" }
const b: Item = { id: "b", kind: "tenant" }
const c: Item = { id: "c", kind: "tenant" }
const partner: Item = { id: "p", kind: "partner" }

const getId = (it: Item) => it.id

describe("useBulkSelection — empty / inicial", () => {
  it("selected é vazio inicialmente", () => {
    const { result } = renderHook(() =>
      useBulkSelection<Item>({ visibleItems: [a, b, c], getId }),
    )
    expect(result.current.selected.size).toBe(0)
    expect(result.current.selectedVisibleCount).toBe(0)
    expect(result.current.visibleSelectableCount).toBe(3)
  })

  it("headerCheckboxState='unchecked' quando ninguém selecionado", () => {
    const { result } = renderHook(() =>
      useBulkSelection<Item>({ visibleItems: [a, b], getId }),
    )
    expect(result.current.headerCheckboxState).toBe("unchecked")
  })

  it("headerCheckboxState='unchecked' com lista vazia", () => {
    const { result } = renderHook(() =>
      useBulkSelection<Item>({ visibleItems: [], getId }),
    )
    expect(result.current.headerCheckboxState).toBe("unchecked")
    expect(result.current.visibleSelectableCount).toBe(0)
  })
})

describe("useBulkSelection — toggleOne", () => {
  it("liga e desliga um único id", () => {
    const { result } = renderHook(() =>
      useBulkSelection<Item>({ visibleItems: [a, b], getId }),
    )
    act(() => result.current.toggleOne("a"))
    expect(result.current.isSelected("a")).toBe(true)
    expect(result.current.selectedVisibleCount).toBe(1)

    act(() => result.current.toggleOne("a"))
    expect(result.current.isSelected("a")).toBe(false)
    expect(result.current.selectedVisibleCount).toBe(0)
  })

  it("seleção parcial gera 'indeterminate'", () => {
    const { result } = renderHook(() =>
      useBulkSelection<Item>({ visibleItems: [a, b, c], getId }),
    )
    act(() => result.current.toggleOne("a"))
    expect(result.current.headerCheckboxState).toBe("indeterminate")
  })

  it("seleção total gera 'checked'", () => {
    const { result } = renderHook(() =>
      useBulkSelection<Item>({ visibleItems: [a, b], getId }),
    )
    act(() => result.current.toggleOne("a"))
    act(() => result.current.toggleOne("b"))
    expect(result.current.headerCheckboxState).toBe("checked")
  })
})

describe("useBulkSelection — toggleAllVisible", () => {
  it("liga todos quando ninguém estava ligado", () => {
    const { result } = renderHook(() =>
      useBulkSelection<Item>({ visibleItems: [a, b, c], getId }),
    )
    act(() => result.current.toggleAllVisible())
    expect(result.current.selectedVisibleCount).toBe(3)
    expect(result.current.headerCheckboxState).toBe("checked")
  })

  it("desliga todos quando algum visível estava ligado (mesmo parcial)", () => {
    const { result } = renderHook(() =>
      useBulkSelection<Item>({ visibleItems: [a, b, c], getId }),
    )
    act(() => result.current.toggleOne("a"))
    expect(result.current.headerCheckboxState).toBe("indeterminate")
    act(() => result.current.toggleAllVisible())
    expect(result.current.selectedVisibleCount).toBe(0)
    expect(result.current.headerCheckboxState).toBe("unchecked")
  })

  it("desliga todos quando todos visíveis estavam ligados", () => {
    const { result } = renderHook(() =>
      useBulkSelection<Item>({ visibleItems: [a, b], getId }),
    )
    act(() => result.current.toggleAllVisible())
    expect(result.current.headerCheckboxState).toBe("checked")
    act(() => result.current.toggleAllVisible())
    expect(result.current.selectedVisibleCount).toBe(0)
  })
})

describe("useBulkSelection — clearSelection", () => {
  it("limpa seleção mesmo de itens fora dos visíveis", () => {
    const { result, rerender } = renderHook(
      ({ items }: { items: Item[] }) =>
        useBulkSelection<Item>({ visibleItems: items, getId }),
      { initialProps: { items: [a, b, c] } },
    )
    act(() => result.current.toggleOne("a"))
    act(() => result.current.toggleOne("b"))

    // muda os visíveis (simula filtragem) — "a" e "b" continuam selecionados
    // internamente embora não estejam visíveis
    rerender({ items: [c] })
    expect(result.current.selected.has("a")).toBe(true)
    expect(result.current.selected.has("b")).toBe(true)
    expect(result.current.selectedVisibleCount).toBe(0)

    act(() => result.current.clearSelection())
    expect(result.current.selected.size).toBe(0)
  })
})

describe("useBulkSelection — isSelectable filtra do toggle e do count", () => {
  it("itens não-selecionáveis são ignorados em toggleAllVisible", () => {
    const { result } = renderHook(() =>
      useBulkSelection<Item>({
        visibleItems: [a, b, partner],
        getId,
        isSelectable: (it) => it.kind !== "partner",
      }),
    )
    expect(result.current.visibleSelectableCount).toBe(2)
    act(() => result.current.toggleAllVisible())
    expect(result.current.selected.has("a")).toBe(true)
    expect(result.current.selected.has("b")).toBe(true)
    expect(result.current.selected.has("p")).toBe(false)
    expect(result.current.headerCheckboxState).toBe("checked")
  })

  it("toggleOne ainda permite ligar manualmente um item não-selecionável", () => {
    // toggleOne é por ID — não consulta isSelectable. O caller pode/decide
    // bloquear na UI. Documentamos esse comportamento aqui.
    const { result } = renderHook(() =>
      useBulkSelection<Item>({
        visibleItems: [a, partner],
        getId,
        isSelectable: (it) => it.kind !== "partner",
      }),
    )
    act(() => result.current.toggleOne("p"))
    expect(result.current.selected.has("p")).toBe(true)
    // ...mas ele NÃO conta para selectedVisibleCount nem afeta header.
    expect(result.current.selectedVisibleCount).toBe(0)
    expect(result.current.headerCheckboxState).toBe("unchecked")
  })
})

describe("useBulkSelection — estabilidade do retorno", () => {
  it("toggleOne e clearSelection são estáveis entre re-renders (não dependem de props)", () => {
    const items = [a, b]
    const { result, rerender } = renderHook(
      ({ items }: { items: Item[] }) =>
        useBulkSelection<Item>({ visibleItems: items, getId }),
      { initialProps: { items } },
    )
    const t1 = result.current.toggleOne
    const cs1 = result.current.clearSelection
    rerender({ items }) // mesma referência
    expect(result.current.toggleOne).toBe(t1)
    expect(result.current.clearSelection).toBe(cs1)
  })
})
