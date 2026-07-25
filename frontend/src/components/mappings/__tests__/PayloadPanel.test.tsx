/**
 * PayloadPanel — aba Reservoir.
 *
 * Contexto que estes testes existem para travar: a aba foi um placeholder estático
 * por um release inteiro. O endpoint `GET /mappings/samples` e o EmptyState
 * "amostras indisponíveis" entraram no MESMO commit, e só o segundo foi ligado —
 * o painel nunca chamou API nenhuma. A tela anunciava indisponibilidade de algo
 * que existia, funcionava e já era consumido pelo MCP.
 *
 * Os quatro estados são distintos de propósito, porque significam coisas
 * diferentes para quem opera:
 *   - precisa escolher org  → "você não disse de qual tenant" (nem chega a buscar)
 *   - vazio                 → "ainda não passou tráfego" (não é falha)
 *   - erro                  → "a busca falhou" (com como tentar de novo)
 *   - com amostras          → escolher uma alimenta o dry-run
 */

import { describe, it, expect, vi, beforeEach } from "vitest"
import { render, screen, waitFor, fireEvent } from "@testing-library/react"
import { PayloadPanel } from "@/components/mappings/PayloadPanel"
import * as api from "@/services/api"

vi.mock("@/services/api")

const mockedGetSamples = vi.mocked(api.getMappingSamples)

const AMOSTRAS = [
  { id: "evt-1", type: "Event::Endpoint::Threat::Detected", severity: "high" },
  { id: "evt-2", type: "Event::Endpoint::Denc::NotEncrypted", severity: "medium" },
]

function renderPanel(props: Partial<React.ComponentProps<typeof PayloadPanel>> = {}) {
  return render(
    <PayloadPanel
      onRawEventChange={props.onRawEventChange ?? vi.fn()}
      vendor="sophos"
      eventType="sophos.alert"
      orgId={1}
      {...props}
    />,
  )
}

describe("PayloadPanel — reservoir", () => {
  beforeEach(() => vi.clearAllMocks())

  it("busca as amostras com as coordenadas do mapping e a org escolhida", async () => {
    mockedGetSamples.mockResolvedValue({
      vendor: "sophos",
      event_type: "sophos.alert",
      total_in_reservoir: 2,
      items: AMOSTRAS,
    })

    renderPanel()

    await waitFor(() => expect(mockedGetSamples).toHaveBeenCalled())
    expect(mockedGetSamples).toHaveBeenCalledWith(
      expect.objectContaining({ vendor: "sophos", event_type: "sophos.alert", org_id: 1 }),
      expect.anything(),
    )
  })

  it("lista as amostras e escolher uma alimenta o dry-run", async () => {
    const onRawEventChange = vi.fn()
    mockedGetSamples.mockResolvedValue({
      vendor: "sophos",
      event_type: "sophos.alert",
      total_in_reservoir: 2,
      items: AMOSTRAS,
    })

    renderPanel({ onRawEventChange })

    const lista = await screen.findByTestId("reservoir-samples")
    const botoes = lista.querySelectorAll("button")
    expect(botoes).toHaveLength(2)

    fireEvent.click(botoes[0])
    expect(onRawEventChange).toHaveBeenCalledWith(AMOSTRAS[0])
  })

  it("reservoir vazio não é apresentado como falha", async () => {
    mockedGetSamples.mockResolvedValue({
      vendor: "sophos",
      event_type: "sophos.alert",
      total_in_reservoir: 0,
      items: [],
    })

    renderPanel()

    // A copy antiga dizia "indisponíveis" e prometia o recurso para o futuro. Vazio
    // é um estado legítimo: o reservoir só enche quando eventos reais passam.
    expect(await screen.findByText(/ainda não há amostras/i)).toBeInTheDocument()
    expect(screen.queryByText(/indispon/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/em breve/i)).not.toBeInTheDocument()
  })

  it("erro de busca é distinguível de vazio e oferece tentar de novo", async () => {
    mockedGetSamples.mockRejectedValue(new Error("backend fora do ar"))

    renderPanel()

    expect(await screen.findByText(/backend fora do ar/i)).toBeInTheDocument()
    const retry = screen.getByRole("button", { name: /tentar de novo/i })

    mockedGetSamples.mockResolvedValue({
      vendor: "sophos",
      event_type: "sophos.alert",
      total_in_reservoir: 1,
      items: [AMOSTRAS[0]],
    })
    fireEvent.click(retry)

    expect(await screen.findByTestId("reservoir-samples")).toBeInTheDocument()
  })

  it("admin global sem org escolhida: pede o tenant e NÃO chama a API", async () => {
    renderPanel({ needsOrgChoice: true, orgId: null })

    expect(await screen.findByText(/escolha uma organização/i)).toBeInTheDocument()
    // O ponto do estado: sem tenant a resposta viria vazia e a tela mentiria
    // "não há amostras". Melhor não perguntar do que perguntar errado.
    expect(mockedGetSamples).not.toHaveBeenCalled()
    expect(screen.queryByText(/ainda não há amostras/i)).not.toBeInTheDocument()
  })
})
