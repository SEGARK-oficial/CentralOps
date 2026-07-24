/**
 * Sparkline: bucket parcial e resumo por média.
 *
 * BUG RELATADO: o card do destino exibia um número absurdamente baixo
 * ("11 eventos/min" num destino com ~101/min de média). Causa: o valor exibido
 * era sempre o ÚLTIMO ponto da série, e o último bucket de um contador por
 * minuto é o minuto CORRENTE, que ainda não fechou. Medido no deploy real: o
 * bucket parcial valia 5 contra uma média de 101 — subcontagem de 95%.
 */
import { render, screen } from "@testing-library/react"
import { describe, it, expect, beforeAll } from "vitest"
import { Sparkline } from "../Sparkline"
import i18n from "@/i18n"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

// 5 buckets cheios (100) + 1 parcial (5) — o formato do incidente.
const SERIES: [number, number][] = [
  [1, 100], [2, 100], [3, 100], [4, 100], [5, 100], [6, 5],
]

describe("Sparkline — bucket parcial", () => {
  it("por padrão mostra o último ponto (comportamento histórico preservado)", () => {
    render(<Sparkline points={SERIES} label="eventos/min" />)
    expect(screen.getByRole("img").getAttribute("aria-label")).toMatch(/5\.00/)
  })

  it("com dropPartialLast + mean, mostra a média dos buckets COMPLETOS", () => {
    render(<Sparkline points={SERIES} label="eventos/min" dropPartialLast summary="mean" />)
    // média dos 5 completos = 100, não os 5 do bucket parcial
    expect(screen.getByRole("img").getAttribute("aria-label")).toMatch(/100\.00/)
  })

  it("não descarta quando só existe um ponto (não vira 'sem dados')", () => {
    render(<Sparkline points={[[1, 42]]} label="eventos/min" dropPartialLast summary="mean" />)
    expect(screen.getByRole("img").getAttribute("aria-label")).toMatch(/42\.00/)
  })

  it("série vazia continua reportando ausência de dados", () => {
    render(<Sparkline points={[]} label="latência" dropPartialLast summary="mean" />)
    expect(screen.getByText(/sem dados/i)).toBeInTheDocument()
  })
})
