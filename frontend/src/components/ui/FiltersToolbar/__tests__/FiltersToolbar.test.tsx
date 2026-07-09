/**
 * Testes de FiltersToolbar.
 */

import { useState } from "react"
import { fireEvent, render, screen, waitFor, act } from "@testing-library/react"
import { FiltersToolbar } from "@/components/ui/FiltersToolbar/FiltersToolbar"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

// Wrapper para testar input controlado + debounce
function ControlledHarness({
  initialValue = "",
  debounceMs = 300,
  onDebouncedChange,
  hasActiveFilters,
  onReset,
  children,
}: {
  initialValue?: string
  debounceMs?: number
  onDebouncedChange?: (v: string) => void
  hasActiveFilters?: boolean
  onReset?: () => void
  children?: React.ReactNode
}) {
  const [value, setValue] = useState(initialValue)
  return (
    <FiltersToolbar
      search={{
        value,
        onChange: setValue,
        placeholder: "Buscar...",
        label: "Buscar",
        ariaLabel: "Buscar registros",
        debounceMs,
        onDebouncedChange,
      }}
      hasActiveFilters={hasActiveFilters}
      onReset={onReset}
    >
      {children}
    </FiltersToolbar>
  )
}

describe("FiltersToolbar — render básico", () => {
  it("renderiza o container com data-testid padrão", () => {
    render(<ControlledHarness />)
    expect(screen.getByTestId("filters-toolbar")).toBeInTheDocument()
  })

  it("renderiza search input com aria-label correto", () => {
    render(<ControlledHarness />)
    expect(screen.getByRole("textbox", { name: /buscar registros/i })).toBeInTheDocument()
  })

  it("renderiza children passados como slot", () => {
    render(
      <ControlledHarness>
        <div data-testid="custom-select">Custom Select</div>
      </ControlledHarness>,
    )
    expect(screen.getByTestId("custom-select")).toBeInTheDocument()
  })

  it("não renderiza search se prop search ausente", () => {
    render(<FiltersToolbar>{null}</FiltersToolbar>)
    expect(screen.queryByTestId("filters-toolbar-search")).not.toBeInTheDocument()
  })
})

describe("FiltersToolbar — search controlado", () => {
  it("digitar atualiza valor controlado imediatamente", () => {
    render(<ControlledHarness />)
    const input = screen.getByRole("textbox", { name: /buscar registros/i }) as HTMLInputElement
    fireEvent.change(input, { target: { value: "alpha" } })
    expect(input.value).toBe("alpha")
  })
})

describe("FiltersToolbar — debounce", () => {
  it("dispara onDebouncedChange só após debounceMs", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const onDebouncedChange = vi.fn()
      render(<ControlledHarness debounceMs={300} onDebouncedChange={onDebouncedChange} />)
      // mount já dispara uma vez com valor inicial ""
      onDebouncedChange.mockClear()

      const input = screen.getByRole("textbox", { name: /buscar registros/i })
      fireEvent.change(input, { target: { value: "alpha" } })

      // imediatamente, callback ainda não foi chamado
      expect(onDebouncedChange).not.toHaveBeenCalled()

      act(() => {
        vi.advanceTimersByTime(350)
      })
      await waitFor(() => expect(onDebouncedChange).toHaveBeenCalledWith("alpha"))
    } finally {
      vi.useRealTimers()
    }
  })

  it("digitação contínua não dispara debounce intermediário", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const onDebouncedChange = vi.fn()
      render(<ControlledHarness debounceMs={300} onDebouncedChange={onDebouncedChange} />)
      onDebouncedChange.mockClear()

      const input = screen.getByRole("textbox", { name: /buscar registros/i })
      fireEvent.change(input, { target: { value: "a" } })
      act(() => vi.advanceTimersByTime(100))
      fireEvent.change(input, { target: { value: "ab" } })
      act(() => vi.advanceTimersByTime(100))
      fireEvent.change(input, { target: { value: "abc" } })
      // só após 300ms da última digitação
      act(() => vi.advanceTimersByTime(350))

      await waitFor(() => expect(onDebouncedChange).toHaveBeenCalledWith("abc"))
      // chamadas com valores intermediários não devem ter ocorrido
      expect(onDebouncedChange).not.toHaveBeenCalledWith("a")
      expect(onDebouncedChange).not.toHaveBeenCalledWith("ab")
    } finally {
      vi.useRealTimers()
    }
  })
})

describe("FiltersToolbar — botão Resetar", () => {
  it("não aparece quando hasActiveFilters=false", () => {
    const onReset = vi.fn()
    render(<ControlledHarness hasActiveFilters={false} onReset={onReset} />)
    expect(screen.queryByTestId("filters-toolbar-reset")).not.toBeInTheDocument()
  })

  it("não aparece sem onReset mesmo com hasActiveFilters=true", () => {
    render(<ControlledHarness hasActiveFilters />)
    expect(screen.queryByTestId("filters-toolbar-reset")).not.toBeInTheDocument()
  })

  it("aparece quando hasActiveFilters=true e onReset definido", () => {
    const onReset = vi.fn()
    render(<ControlledHarness hasActiveFilters onReset={onReset} />)
    expect(screen.getByTestId("filters-toolbar-reset")).toBeInTheDocument()
  })

  it("clicar dispara onReset", () => {
    const onReset = vi.fn()
    render(<ControlledHarness hasActiveFilters onReset={onReset} />)
    fireEvent.click(screen.getByTestId("filters-toolbar-reset"))
    expect(onReset).toHaveBeenCalledTimes(1)
  })

  it("botão Resetar tem aria-label 'Resetar filtros'", () => {
    const onReset = vi.fn()
    render(<ControlledHarness hasActiveFilters onReset={onReset} />)
    expect(screen.getByTestId("filters-toolbar-reset")).toHaveAttribute(
      "aria-label",
      "Resetar filtros",
    )
  })
})

describe("FiltersToolbar — data-testid customizável", () => {
  it("permite override do test-id", () => {
    const onReset = vi.fn()
    render(
      <FiltersToolbar
        search={{
          value: "x",
          onChange: () => {},
          ariaLabel: "Buscar tenants",
        }}
        hasActiveFilters
        onReset={onReset}
        data-testid="partner-filters"
      >
        {null}
      </FiltersToolbar>,
    )
    expect(screen.getByTestId("partner-filters")).toBeInTheDocument()
    expect(screen.getByTestId("partner-filters-search")).toBeInTheDocument()
    expect(screen.getByTestId("partner-filters-reset")).toBeInTheDocument()
  })
})
