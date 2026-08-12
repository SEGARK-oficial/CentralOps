/**
 * ExecutionPanel tests.
 *
 * O painel existe para responder "isso está funcionando?". O que estes testes
 * protegem é a leitura, não o layout: a mensagem do provedor tem que aparecer,
 * "não disparou" tem que ser distinguível de "0% de acerto", e o filtro de
 * falhas tem que ir ao servidor (o ring tem 200 posições e filtrar só na tela
 * esconderia falha que ficou fora da página).
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest"
import { ExecutionPanel } from "../ExecutionPanel"
import * as api from "@/services/api"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

const NOW = Math.floor(Date.now() / 1000)

beforeEach(() => {
  vi.clearAllMocks()
  mockedApi.getEnrichmentMetrics.mockResolvedValue({
    organization_id: 1,
    range_minutes: 60,
    policy_name: null,
    rules: [],
  })
  mockedApi.getEnrichmentActivity.mockResolvedValue({ organization_id: 1, entries: [] })
})

describe("ExecutionPanel", () => {
  it("mostra vazio quando ainda não houve ciclo", async () => {
    render(<ExecutionPanel />)
    expect(await screen.findByText("Nada registrado ainda")).toBeInTheDocument()
  })

  it("mostra a mensagem do provedor na consulta que falhou", async () => {
    // Sem isto o operador só vê "falhou" e precisa abrir log de worker para
    // descobrir que o token está errado.
    mockedApi.getEnrichmentActivity.mockResolvedValue({
      organization_id: 1,
      entries: [
        {
          ts: NOW - 30,
          kind: "remote_batch",
          ok: false,
          rule_id: "ti-remota",
          enricher: "taxii",
          source: "opencti-prod",
          reason: "http_401",
          detail: "HTTPStatusError: 401 Unauthorized",
          keys: 40,
          latency_ms: 812,
        },
      ],
    })

    render(<ExecutionPanel />)

    expect(await screen.findByText(/401 Unauthorized/)).toBeInTheDocument()
    expect(screen.getByText("ti-remota")).toBeInTheDocument()
    expect(screen.getByText("consulta remota")).toBeInTheDocument()
  })

  it("distingue regra muda de regra com 0% de acerto", async () => {
    mockedApi.getEnrichmentMetrics.mockResolvedValue({
      organization_id: 1,
      range_minutes: 60,
      policy_name: "contexto-de-ativo",
      rules: [
        { rule_id: "nunca-rodou", enricher: "table_cidr", source: null, hit: 0, miss: 0, skipped: 0, error: 0 },
        { rule_id: "so-erra", enricher: "table_cidr", source: null, hit: 0, miss: 100, skipped: 0, error: 0 },
      ],
    })

    render(<ExecutionPanel />)
    await screen.findByTestId("metric-nunca-rodou")

    // Muda: a política pode estar desligada. Mandar mexer na tabela seria o
    // conselho errado.
    expect(screen.getByTestId("metric-nunca-rodou")).toHaveTextContent("Não disparou na janela")
    // Rodou e não casou nada: aí sim o problema é o dado.
    expect(screen.getByTestId("metric-so-erra")).toHaveTextContent("100%")
  })

  it("filtra falhas no servidor, não só na tela", async () => {
    render(<ExecutionPanel />)
    await screen.findByText("Nada registrado ainda")

    fireEvent.click(screen.getByTestId("toggle-only-failures"))

    await waitFor(() =>
      expect(mockedApi.getEnrichmentActivity).toHaveBeenLastCalledWith(
        expect.objectContaining({ only_failures: true }),
      ),
    )
  })

  it("exige escolher a organização quando o token vê várias", async () => {
    // Não existe enriquecimento global: sem `organization_id` a API responde
    // 422. Escolher a primeira e deixar visível é melhor que o erro seco.
    render(
      <ExecutionPanel
        organizations={[
          { id: 7, name: "Cliente A" },
          { id: 9, name: "Cliente B" },
        ]}
      />,
    )

    await waitFor(() =>
      expect(mockedApi.getEnrichmentMetrics).toHaveBeenCalledWith(
        expect.objectContaining({ organization_id: 7 }),
      ),
    )
    expect(screen.getByLabelText("Organização")).toBeInTheDocument()
  })

  it("não pede organização quando o token vê uma só", async () => {
    render(<ExecutionPanel organizations={[{ id: 7, name: "Cliente A" }]} />)
    await screen.findByText("Nada registrado ainda")
    expect(screen.queryByLabelText("Organização")).not.toBeInTheDocument()
  })
  it("diz qual política está valendo", async () => {
    // O worker aplica UMA política por organização, a mais antiga habilitada.
    // Sem dizer qual, editar a outra parece simplesmente não ter efeito.
    mockedApi.getEnrichmentMetrics.mockResolvedValue({
      organization_id: 1,
      range_minutes: 60,
      policy_name: "contexto-de-ativo",
      rules: [
        { rule_id: "cmdb", enricher: "table_cidr", source: null, hit: 10, miss: 1, skipped: 0, error: 0 },
      ],
    })

    render(<ExecutionPanel />)
    expect(await screen.findByTestId("active-policy")).toHaveTextContent("contexto-de-ativo")
  })
})
