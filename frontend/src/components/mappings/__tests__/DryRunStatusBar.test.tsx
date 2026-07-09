/**
 * Testes de DryRunStatusBar
 * Cobre: render padrão sem warnings, chip de warning (count + label),
 * singular vs plural, chip oculto durante loading.
 * Fase 4.1b
 */

import { render, screen, fireEvent } from "@testing-library/react"
import { DryRunStatusBar } from "@/components/mappings/DryRunStatusBar"
import type { DryRunDefaultHitWarning } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

const BASE_PROPS = {
  sampleSize: 10,
  okCount: 9,
  failCount: 1,
  isPending: false,
}

const WARNING_ONE: DryRunDefaultHitWarning = {
  target: "event.action",
  hit_rate: 1.0,
  hit_count: 10,
  sample_size: 10,
  expected_always_default: false,
}

const WARNING_TWO: DryRunDefaultHitWarning = {
  target: "event.user",
  hit_rate: 1.0,
  hit_count: 10,
  sample_size: 10,
  expected_always_default: false,
}

describe("DryRunStatusBar — render sem warnings (backward compat)", () => {
  it("renderiza 3 chips: amostras, OK, falhas", () => {
    render(<DryRunStatusBar {...BASE_PROPS} />)
    expect(screen.getByText("10 amostras")).toBeInTheDocument()
    expect(screen.getByText("9 OK")).toBeInTheDocument()
    expect(screen.getByText("1 falha")).toBeInTheDocument()
  })

  it("NÃO renderiza chip de warning quando default_hit_warnings está vazio", () => {
    render(<DryRunStatusBar {...BASE_PROPS} default_hit_warnings={[]} />)
    expect(screen.queryByTestId("default-hit-warnings-chip")).not.toBeInTheDocument()
  })

  it("NÃO renderiza chip de warning quando default_hit_warnings é omitido", () => {
    render(<DryRunStatusBar {...BASE_PROPS} />)
    expect(screen.queryByTestId("default-hit-warnings-chip")).not.toBeInTheDocument()
  })

  it("tem role=status e aria-live=polite", () => {
    render(<DryRunStatusBar {...BASE_PROPS} />)
    const bar = screen.getByTestId("dry-run-status-bar")
    expect(bar).toHaveAttribute("role", "status")
    expect(bar).toHaveAttribute("aria-live", "polite")
  })
})

describe("DryRunStatusBar — chip de warning presente", () => {
  it("renderiza o chip quando há 1 warning — texto singular '1 regra 100% default'", () => {
    render(
      <DryRunStatusBar
        {...BASE_PROPS}
        default_hit_warnings={[WARNING_ONE]}
      />,
    )
    const chip = screen.getByTestId("default-hit-warnings-chip")
    expect(chip).toBeInTheDocument()
    expect(chip).toHaveTextContent("1 regra 100% default")
  })

  it("renderiza o chip com plural '2 regras 100% default' quando há 2 warnings", () => {
    render(
      <DryRunStatusBar
        {...BASE_PROPS}
        default_hit_warnings={[WARNING_ONE, WARNING_TWO]}
      />,
    )
    const chip = screen.getByTestId("default-hit-warnings-chip")
    expect(chip).toHaveTextContent("2 regras 100% default")
  })

  it("chip tem aria-label descrevendo o warning count", () => {
    render(
      <DryRunStatusBar
        {...BASE_PROPS}
        default_hit_warnings={[WARNING_ONE]}
      />,
    )
    const chip = screen.getByTestId("default-hit-warnings-chip")
    expect(chip).toHaveAttribute("aria-label", expect.stringContaining("1 regra"))
  })

  it("chip é um <button> (não div)", () => {
    render(
      <DryRunStatusBar
        {...BASE_PROPS}
        default_hit_warnings={[WARNING_ONE]}
      />,
    )
    const chip = screen.getByTestId("default-hit-warnings-chip")
    expect(chip.tagName).toBe("BUTTON")
  })

  it("click no chip dispara onWarningsClick", () => {
    const onWarningsClick = vi.fn()
    render(
      <DryRunStatusBar
        {...BASE_PROPS}
        default_hit_warnings={[WARNING_ONE]}
        onWarningsClick={onWarningsClick}
      />,
    )
    fireEvent.click(screen.getByTestId("default-hit-warnings-chip"))
    expect(onWarningsClick).toHaveBeenCalledTimes(1)
  })
})

describe("DryRunStatusBar — chip oculto durante loading", () => {
  it("NÃO exibe o chip de warning quando isPending=true, mesmo com warnings", () => {
    render(
      <DryRunStatusBar
        {...BASE_PROPS}
        isPending={true}
        default_hit_warnings={[WARNING_ONE]}
      />,
    )
    expect(screen.queryByTestId("default-hit-warnings-chip")).not.toBeInTheDocument()
  })

  it("exibe o chip de warning quando isPending=false e há warnings", () => {
    render(
      <DryRunStatusBar
        {...BASE_PROPS}
        isPending={false}
        default_hit_warnings={[WARNING_ONE]}
      />,
    )
    expect(screen.getByTestId("default-hit-warnings-chip")).toBeInTheDocument()
  })
})
