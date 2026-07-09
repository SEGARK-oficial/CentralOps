/**
 * Testes de Sparkline (Fase 4 / C5)
 * Cobre: 0 pontos, 1 ponto, N pontos, variante danger, acessibilidade.
 */

import { render, screen } from "@testing-library/react"
import { describe, it, expect } from "vitest"
import { Sparkline } from "@/components/observability/Sparkline"

const TS = 1718640000000 // timestamp fixo para evitar flakiness

describe("Sparkline", () => {
  // ── 0 pontos ──────────────────────────────────────────────────────────

  it("exibe 'sem dados' quando array é vazio", () => {
    render(<Sparkline points={[]} label="Eventos" />)
    expect(screen.getByText(/sem dados/i)).toBeInTheDocument()
  })

  it("exibe o label no texto 'sem dados'", () => {
    render(<Sparkline points={[]} label="CPU" />)
    expect(screen.getByText(/CPU/)).toBeInTheDocument()
  })

  // ── 1 ponto ───────────────────────────────────────────────────────────

  it("renderiza marcador (SVG) com 1 ponto em vez de 'sem dados'", () => {
    const { container } = render(<Sparkline points={[[TS, 42]]} label="Latência" />)
    // deve existir SVG — não cai no fallback "sem dados"
    expect(container.querySelector("svg")).toBeInTheDocument()
    expect(screen.queryByText(/sem dados/i)).not.toBeInTheDocument()
  })

  it("exibe o último valor com 1 ponto", () => {
    render(<Sparkline points={[[TS, 42]]} label="Latência" />)
    expect(screen.getByText("42.00")).toBeInTheDocument()
  })

  it("exibe o label com 1 ponto", () => {
    render(<Sparkline points={[[TS, 42]]} label="Latência" />)
    // label aparece no texto ao lado e no aria-label
    expect(screen.getAllByText("Latência").length).toBeGreaterThan(0)
  })

  // ── N pontos ──────────────────────────────────────────────────────────

  it("renderiza SVG com múltiplos pontos", () => {
    const pts: [number, number][] = [
      [TS,       10],
      [TS + 1e4, 20],
      [TS + 2e4, 15],
    ]
    const { container } = render(<Sparkline points={pts} label="Tráfego" />)
    expect(container.querySelector("svg")).toBeInTheDocument()
    expect(container.querySelector("path")).toBeInTheDocument()
  })

  it("exibe último valor corretamente com N pontos", () => {
    const pts: [number, number][] = [
      [TS,       10],
      [TS + 1e4, 20],
      [TS + 2e4, 99],
    ]
    render(<Sparkline points={pts} label="Tráfego" />)
    expect(screen.getByText("99.00")).toBeInTheDocument()
  })

  it("ignora valores NaN no array", () => {
    const pts: [number, number | string][] = [
      [TS,       10],
      [TS + 1e4, "nan"],
      [TS + 2e4, 5],
    ]
    // não deve crashar; NaN filtrado
    const { container } = render(<Sparkline points={pts} label="Mix" />)
    expect(container.querySelector("svg")).toBeInTheDocument()
  })

  // ── Variante danger ───────────────────────────────────────────────────

  it("variante danger renderiza SVG (canal visual distinto do neutral)", () => {
    const pts: [number, number][] = [[TS, 1], [TS + 1e4, 2]]
    const { container: cDanger  } = render(<Sparkline points={pts} label="D" variant="danger" />)
    const { container: cNeutral } = render(<Sparkline points={pts} label="N" variant="neutral" />)

    // [0]=area fill, [1]=linha principal com stroke
    const lineDanger  = cDanger.querySelectorAll("path")[1]
    const lineNeutral = cNeutral.querySelectorAll("path")[1]

    expect(lineDanger).toBeInTheDocument()
    expect(lineNeutral).toBeInTheDocument()

    // as duas linhas devem ter stroke de cores diferentes
    const dStroke  = lineDanger?.getAttribute("stroke")
    const nStroke  = lineNeutral?.getAttribute("stroke")
    expect(dStroke).toBeDefined()
    expect(nStroke).toBeDefined()
    expect(dStroke).not.toEqual(nStroke)
  })

  it("variante danger usa cor danger-500 (#ef4444)", () => {
    const pts: [number, number][] = [[TS, 1], [TS + 1e4, 2]]
    const { container } = render(<Sparkline points={pts} label="Erros" variant="danger" />)
    // a linha principal deve ter stroke com a cor de danger
    const linePath = container.querySelectorAll("path")[1] // [0]=area, [1]=linha
    expect(linePath?.getAttribute("stroke")).toBe("#ef4444")
  })

  // ── Acessibilidade ────────────────────────────────────────────────────

  it("SVG tem role=img e aria-label", () => {
    const pts: [number, number][] = [[TS, 1], [TS + 1e4, 2]]
    const { container } = render(<Sparkline points={pts} label="Acessível" />)
    const svg = container.querySelector("svg")
    expect(svg?.getAttribute("role")).toBe("img")
    expect(svg?.getAttribute("aria-label")).toContain("Acessível")
  })

  it("aria-label contém o último valor", () => {
    const pts: [number, number][] = [[TS, 1], [TS + 1e4, 77]]
    const { container } = render(<Sparkline points={pts} label="Val" />)
    const svg = container.querySelector("svg")
    expect(svg?.getAttribute("aria-label")).toContain("77")
  })

  it("SVG tem <title> interno para tooltip nativo", () => {
    const pts: [number, number][] = [[TS, 1], [TS + 1e4, 2]]
    const { container } = render(<Sparkline points={pts} label="TitleTest" />)
    const title = container.querySelector("svg > title")
    expect(title).toBeInTheDocument()
  })

  // ── Variantes restantes ───────────────────────────────────────────────

  it.each(["neutral", "primary", "success", "warning"] as const)(
    "variante %s renderiza sem crashar",
    (variant) => {
      const pts: [number, number][] = [[TS, 5], [TS + 1e4, 10]]
      expect(() => render(<Sparkline points={pts} label="V" variant={variant} />)).not.toThrow()
    },
  )

  // ── Props opcionais ───────────────────────────────────────────────────

  it("showBaseline adiciona elemento de linha", () => {
    const pts: [number, number][] = [[TS, 5], [TS + 1e4, 10]]
    const { container } = render(<Sparkline points={pts} label="Base" showBaseline />)
    const lines = container.querySelectorAll("line")
    expect(lines.length).toBeGreaterThan(0)
  })
})
