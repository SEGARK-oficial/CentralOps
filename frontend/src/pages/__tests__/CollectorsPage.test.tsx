import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import CollectorsPage from "@/pages/CollectorsPage"
import * as api from "@/services/api"
import { useAuth } from "@/contexts/AuthContext"
import type { CollectionState, CollectorSummary, CollectorVendor } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/services/api")
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: vi.fn(),
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
}))

const mockedApi = vi.mocked(api)
const mockedUseAuth = vi.mocked(useAuth)

const adminUser = {
  id: "u-admin",
  username: "admin",
  display_name: "Admin",
  role: "admin" as const,
  organization_id: null,
  organization_name: null,
}

const sampleVendors: CollectorVendor[] = [
  {
    platform: "sophos",
    stream: "alerts",
    queue: "collect.priority",
    task_name: "collectors.collect_vendor_logs_priority",
    schedule_seconds: 60,
  },
  {
    platform: "microsoft_defender",
    stream: "incidents",
    queue: "collect.priority",
    task_name: "collectors.collect_vendor_logs_priority",
    schedule_seconds: 120,
  },
]

const sampleStates: CollectionState[] = [
  {
    integration_id: 42,
    integration_name: "ACME Sophos",
    organization_id: 7,
    organization_name: "ACME Corp",
    platform: "sophos",
    stream: "alerts",
    cursor: { from_ts: "2026-04-23T10:00:00Z" },
    last_success_at: new Date().toISOString(),
    last_attempt_at: new Date().toISOString(),
    last_error: null,
    consecutive_failures: 0,
    events_collected_total: 19384,
    updated_at: new Date().toISOString(),
  },
]

const sampleSummary: CollectorSummary = {
  integrations_tracked: 1,
  vendors_registered: 2,
  events_collected_total: 19384,
  integrations_with_errors: 0,
  stale_minutes_max: 2,
  per_platform: [
    { platform: "sophos", integrations: 1, events_collected_total: 19384, errors: 0 },
  ],
}

describe("CollectorsPage", () => {
  beforeEach(() => {
    mockedUseAuth.mockReturnValue({
      user: adminUser,
      loading: false,
      login: vi.fn(),
      logout: vi.fn(),
      refreshUser: vi.fn(),
    } as never)

    mockedApi.listCollectorVendors.mockResolvedValue(sampleVendors)
    mockedApi.listCollectionState.mockResolvedValue(sampleStates)
    mockedApi.getCollectorSummary.mockResolvedValue(sampleSummary)
  })

  it("renderiza KPIs, vendors e linhas da tabela", async () => {
    render(
      <MemoryRouter>
        <CollectorsPage />
      </MemoryRouter>,
    )

    // Cabeçalho (RF: título "Collectors" no PageHeader)
    expect(screen.getByText("Collectors")).toBeInTheDocument()

    // KPIs vêm depois do fetch
    await waitFor(() =>
      expect(screen.getByText("Integrações monitoradas")).toBeInTheDocument(),
    )
    // "19.384" aparece tanto no KPI quanto na célula da tabela (formato pt-BR).
    expect(screen.getAllByText("19.384").length).toBeGreaterThanOrEqual(1)

    // Vendors pill com "sophos · alerts"
    expect(screen.getByText(/sophos · alerts/i)).toBeInTheDocument()

    // Linha da tabela
    expect(screen.getByText("ACME Corp")).toBeInTheDocument()
    expect(screen.getByText("ACME Sophos")).toBeInTheDocument()
  })

  it("dispara triggerCollection ao clicar no botão Trigger", async () => {
    mockedApi.triggerCollection.mockResolvedValue({
      task_id: "abc12345-abcd-ef00-1234-567890abcdef",
      queue: "collect.priority",
      integration_id: 42,
      stream: "alerts",
    })

    render(
      <MemoryRouter>
        <CollectorsPage />
      </MemoryRouter>,
    )

    await waitFor(() => screen.getByText("ACME Sophos"))

    const triggerButton = screen.getByRole("button", { name: /trigger/i })
    await act(async () => {
      fireEvent.click(triggerButton)
    })

    expect(mockedApi.triggerCollection).toHaveBeenCalledWith(42, "alerts")
    await waitFor(() =>
      expect(screen.getByText(/coleta enfileirada/i)).toBeInTheDocument(),
    )
  })

  it("colapsa a lista de vendors e permite filtrar quando há muitos (escala 200+)", async () => {
    const many: CollectorVendor[] = Array.from({ length: 30 }, (_, i) => ({
      platform: `vendor${i}`,
      stream: "alerts",
      queue: "collect.bulk",
      task_name: "collectors.collect_vendor_logs_bulk",
      schedule_seconds: 300,
    }))
    mockedApi.listCollectorVendors.mockResolvedValue(many)

    render(
      <MemoryRouter>
        <CollectorsPage />
      </MemoryRouter>,
    )

    // Contagem sempre visível; a lista NÃO é renderizada inline (colapsada).
    await waitFor(() =>
      expect(screen.getByText("Vendors registrados (30)")).toBeInTheDocument(),
    )
    expect(screen.queryByText(/vendor0 · alerts/i)).not.toBeInTheDocument()

    // Expandir → busca + badges aparecem.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /ver todos/i }))
    })
    const search = screen.getByLabelText("Filtrar vendors registrados")
    expect(screen.getByText(/vendor0 · alerts/i)).toBeInTheDocument()

    // Filtrar → só o vendor correspondente permanece.
    await act(async () => {
      fireEvent.change(search, { target: { value: "vendor7" } })
    })
    expect(screen.getByText(/vendor7 · alerts/i)).toBeInTheDocument()
    expect(screen.queryByText(/vendor0 · alerts/i)).not.toBeInTheDocument()

    // Fechar e reabrir → o filtro é limpo (não fica preso na busca anterior).
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /ocultar/i }))
    })
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /ver todos/i }))
    })
    expect(
      (screen.getByLabelText("Filtrar vendors registrados") as HTMLInputElement).value,
    ).toBe("")
    expect(screen.getByText(/vendor0 · alerts/i)).toBeInTheDocument()
  })

  it("mostra empty state quando não há coletas", async () => {
    mockedApi.listCollectionState.mockResolvedValue([])
    mockedApi.getCollectorSummary.mockResolvedValue({
      integrations_tracked: 0,
      vendors_registered: 2,
      events_collected_total: 0,
      integrations_with_errors: 0,
      stale_minutes_max: null,
      per_platform: [],
    })

    render(
      <MemoryRouter>
        <CollectorsPage />
      </MemoryRouter>,
    )

    await waitFor(() =>
      expect(screen.getByText(/nenhuma coleta registrada/i)).toBeInTheDocument(),
    )
  })
})
