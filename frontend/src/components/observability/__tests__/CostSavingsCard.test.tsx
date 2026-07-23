/**
 * CostSavingsCard tests — honestidade do funil de volume/custo.
 *
 * Contexto: em produção o card exibiu Coletado 604,6 MB / Entregue 346,4 MB /
 * Evitado 701,2 MB / Redução 66,9%. "Evitado > Coletado" é impossível como
 * balanço, mas NÃO é dupla contagem: bytes_in mede o evento cru (1×/evento) e
 * bytes_out/bytes_saved medem o envelope por entrega. Estes testes travam:
 *
 * - a sinalização do estado incoerente (em vez de exibi-lo em silêncio);
 * - a decomposição por causa (de ONDE veio a economia);
 * - a renderização da Redução vinda do backend, sem recalcular a fórmula;
 * - a distinção entre "não economizou" e "preço por GB não configurado";
 * - formatação em base 1000, a mesma que o pricer usa para faturar.
 */

import { render, screen, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest"
import { CostSavingsCard } from "../CostSavingsCard"
import * as api from "@/services/api"
import i18n from "@/i18n"
import type { CostSummary, CostSummaryRow } from "@/services/api"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/services/api", async () => {
  const actual = await vi.importActual<typeof api>("@/services/api")
  return { ...actual, getCostSummary: vi.fn() }
})
const mockedApi = vi.mocked(api)

function row(over: Partial<CostSummaryRow> = {}): CostSummaryRow {
  return {
    organization_id: 1,
    bytes_in: 1000,
    bytes_out: 400,
    events_in: 10,
    events_out: 4,
    out_in_byte_ratio: 0.4,
    reduction_active: true,
    bytes_saved: 300,
    bytes_saved_by_reason: {},
    reduction_pct: 0.4286,
    unit_mismatch: false,
    savings_usd_per_day: null,
    cost: null,
    ...over,
  }
}

function summary(over: Partial<CostSummary> = {}): CostSummary {
  return {
    window_minutes: 180,
    enabled: true,
    pricing_available: false,
    levers: { trim: true, sample: true, suppress: true, aggregate: false, drop: true },
    units: { bytes_in: "raw_event", bytes_out: "envelope_per_delivery", bytes_saved: "mixed" },
    rows: [row()],
    note: "",
    ...over,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe("CostSavingsCard — funil incoerente", () => {
  it("sinaliza quando Evitado supera Coletado, com os números do incidente", async () => {
    mockedApi.getCostSummary.mockResolvedValue(
      summary({
        rows: [
          row({
            bytes_in: 604_600_000,
            bytes_out: 346_400_000,
            bytes_saved: 701_200_000,
            reduction_pct: 0.6693,
            unit_mismatch: true,
          }),
        ],
      }),
    )
    render(<CostSavingsCard />)

    expect(await screen.findByTestId("unit-mismatch-notice")).toBeInTheDocument()
    // base 1000: 604.600.000 B = 604,6 MB (não 576,6 MiB rotulado "MB")
    expect(screen.getByText("604.6 MB")).toBeInTheDocument()
    expect(screen.getByText("701.2 MB")).toBeInTheDocument()
  })

  it("não sinaliza quando o funil é coerente", async () => {
    mockedApi.getCostSummary.mockResolvedValue(summary())
    render(<CostSavingsCard />)

    await waitFor(() => expect(mockedApi.getCostSummary).toHaveBeenCalled())
    expect(screen.queryByTestId("unit-mismatch-notice")).not.toBeInTheDocument()
  })
})

describe("CostSavingsCard — Redução", () => {
  it("renderiza o valor do backend em vez de recalcular (org única)", async () => {
    // bytes_saved/(out+saved) daria 42,9%; o backend manda 10% — o card deve
    // obedecer ao backend, provando que a fórmula não está duplicada aqui.
    mockedApi.getCostSummary.mockResolvedValue(
      summary({ rows: [row({ reduction_pct: 0.1 })] }),
    )
    render(<CostSavingsCard />)

    expect(await screen.findByText("10.0%")).toBeInTheDocument()
  })

  it("agrega com a mesma fórmula do backend quando há várias orgs", async () => {
    mockedApi.getCostSummary.mockResolvedValue(
      summary({
        rows: [
          row({ organization_id: 1, bytes_out: 100, bytes_saved: 100, reduction_pct: 0.5 }),
          row({ organization_id: 2, bytes_out: 300, bytes_saved: 100, reduction_pct: 0.25 }),
        ],
      }),
    )
    render(<CostSavingsCard />)

    // saved 200 / (out 400 + saved 200) = 33,3% — não a média de 0,5 e 0,25.
    expect(await screen.findByText("33.3%")).toBeInTheDocument()
  })
})

describe("CostSavingsCard — decomposição por causa", () => {
  it("mostra de onde veio a economia, ordenado por volume", async () => {
    mockedApi.getCostSummary.mockResolvedValue(
      summary({
        rows: [row({ bytes_saved_by_reason: { trim: 100, drop: 500 } })],
      }),
    )
    render(<CostSavingsCard />)

    const box = await screen.findByTestId("savings-by-reason")
    expect(box).toHaveTextContent("descarte por rota")
    expect(box).toHaveTextContent("poda")
    // maior primeiro: drop (500) antes de trim (100)
    expect(box.textContent?.indexOf("descarte por rota")).toBeLessThan(
      box.textContent?.indexOf("poda") ?? -1,
    )
  })

  it("omite o bloco quando nenhuma causa reportou volume", async () => {
    mockedApi.getCostSummary.mockResolvedValue(summary())
    render(<CostSavingsCard />)

    await waitFor(() => expect(mockedApi.getCostSummary).toHaveBeenCalled())
    expect(screen.queryByTestId("savings-by-reason")).not.toBeInTheDocument()
  })
})

describe("CostSavingsCard — Economia estimada", () => {
  it("distingue preço não configurado de economia zero", async () => {
    // Pricer EE registrado (pricing_available) porém savings nulo em todas as
    // linhas: é cost_per_gb ausente, não ausência de economia.
    mockedApi.getCostSummary.mockResolvedValue(
      summary({
        pricing_available: true,
        rows: [row({ bytes_saved: 300, savings_usd_per_day: null })],
      }),
    )
    render(<CostSavingsCard />)

    expect(await screen.findByTestId("pricing-unconfigured")).toBeInTheDocument()
    expect(screen.queryByText(/Economia estimada/i)).not.toBeInTheDocument()
  })

  it("mostra o valor quando o pricer devolve economia", async () => {
    mockedApi.getCostSummary.mockResolvedValue(
      summary({
        pricing_available: true,
        rows: [row({ savings_usd_per_day: 12.5, cost: { usd: 3, currency: "USD" } })],
      }),
    )
    render(<CostSavingsCard />)

    expect(await screen.findByText(/Economia estimada/i)).toBeInTheDocument()
    expect(screen.queryByTestId("pricing-unconfigured")).not.toBeInTheDocument()
  })
})
