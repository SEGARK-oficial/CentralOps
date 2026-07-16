/**
 * Testes do CollectorConfigForm após a migração.
 * A seção "Destino Wazuh" foi removida do form global; o componente agora
 * exibe um Notice/CTA apontando para a página /destinations.
 * Cobre: render do CTA, render das seções globais mantidas, acessibilidade.
 */

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { ConfigPage } from "@/pages/ConfigPage"
import * as api from "@/services/api"
import { useAuth } from "@/contexts/AuthContext"
import type { CollectorConfig, EmailConfig } from "@/types"
import { vi } from "vitest"
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
// ConfigPage renderiza <EditionInfoCard> (useEdition) — mock Community.
vi.mock("@/contexts/EditionContext", () => ({
  useEdition: () => ({
    edition: "community",
    features: [],
    plan: null,
    seats: null,
    maxOrganizations: null,
    expiresAt: null,
    isEnterprise: false,
    loading: false,
    error: null,
    hasFeature: () => false,
    refresh: vi.fn(),
  }),
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
  is_active: true,
  permissions: [],
}

const emailConfig: EmailConfig = {
  id: 1,
  smtp_host: "smtp.example.com",
  smtp_port: 587,
  smtp_user: "admin@example.com",
  smtp_password_configured: true,
  use_tls: true,
  sender: "no-reply@example.com",
}

function makeConfig(overrides: Partial<CollectorConfig> = {}): CollectorConfig {
  return {
    id: 1,
    is_persisted: true,
    config_version: "1.0.0",
    updated_at: "2026-04-25T10:00:00Z",
    wazuh_syslog_host: "192.168.3.211",
    wazuh_syslog_port: 514,
    wazuh_syslog_use_tls: false,
    wazuh_ca_bundle: null,
    wazuh_dispatch_mode: "syslog",
    wazuh_syslog_format: "rfc3164",
    collector_jsonl_dir: "/var/log/centralops/collectors",
    collector_batch_size: 200,
    collector_batch_flush_seconds: 5,
    dedupe_ttl_days: 7,
    domain_concurrency_limits: {},
    rate_limits_by_vendor: {},
    ...overrides,
  }
}

function setupMocks(config: CollectorConfig) {
  mockedUseAuth.mockReturnValue({
    user: adminUser,
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
    refreshUser: vi.fn(),
  } as never)

  mockedApi.getEmailConfig.mockResolvedValue(emailConfig)
  mockedApi.listEmails.mockResolvedValue([])
  mockedApi.getCollectorConfig.mockResolvedValue(config)
  mockedApi.testCollectorConfig.mockResolvedValue({ mode: "syslog", results: [] })
  mockedApi.updateCollectorConfig.mockResolvedValue(config)
  mockedApi.listCollectorVendors.mockResolvedValue([])
  mockedApi.listDestinations.mockResolvedValue([])
}

async function renderCollectorTab(config: CollectorConfig) {
  setupMocks(config)
  render(
    <MemoryRouter>
      <ConfigPage />
    </MemoryRouter>,
  )
  const collectorTab = await screen.findByRole("tab", { name: /Coleta & Entrega/i })
  await act(async () => {
    fireEvent.click(collectorTab)
  })
  await waitFor(() => screen.getByTestId("destinations-cta"))
}

describe("CollectorConfigForm — CTA de destinos", () => {
  beforeEach(() => vi.clearAllMocks())

  it("exibe o Notice CTA apontando para /destinations", async () => {
    await renderCollectorTab(makeConfig())

    const cta = screen.getByTestId("destinations-cta")
    expect(cta).toBeInTheDocument()
    expect(cta).toHaveTextContent(/Destinos/i)
  })

  it("o CTA contém um link para /destinations", async () => {
    await renderCollectorTab(makeConfig())

    const cta = screen.getByTestId("destinations-cta")
    const link = cta.querySelector("a")
    expect(link).not.toBeNull()
    expect(link).toHaveAttribute("href", "/destinations")
  })

  it("seção 'Buffer & Dedupe' permanece visível", async () => {
    await renderCollectorTab(makeConfig())

    expect(screen.getByText("Buffer & Dedupe")).toBeInTheDocument()
  })

  it("campos de batch size e flush seconds são renderizados", async () => {
    await renderCollectorTab(makeConfig())

    expect(screen.getByLabelText(/Tamanho do lote/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/Flush após/i)).toBeInTheDocument()
  })

  it("seção 'Concorrência por domínio' permanece visível", async () => {
    await renderCollectorTab(makeConfig())

    expect(screen.getByText("Concorrência por domínio")).toBeInTheDocument()
  })

  it("seção de rate-limits por vendor permanece visível", async () => {
    await renderCollectorTab(makeConfig())

    expect(screen.getByText("Rate-limits por vendor")).toBeInTheDocument()
  })

  it("não exibe o banner de dispatch (removido)", async () => {
    await renderCollectorTab(makeConfig())

    expect(screen.queryByTestId("dispatch-status-banner")).not.toBeInTheDocument()
  })

  it("não exibe o select de formato syslog (removido)", async () => {
    await renderCollectorTab(makeConfig())

    expect(screen.queryByTestId("wazuh-syslog-format-select")).not.toBeInTheDocument()
  })

  it("botão 'Salvar configuração' fica desabilitado quando form está limpo", async () => {
    await renderCollectorTab(makeConfig())

    const saveBtn = screen.getByRole("button", { name: /Salvar configuração/i })
    expect(saveBtn).toBeDisabled()
  })

  it("alterar batch size habilita o botão Salvar", async () => {
    await renderCollectorTab(makeConfig())

    const batchInput = screen.getByLabelText(/Tamanho do lote/i)
    await act(async () => {
      fireEvent.change(batchInput, { target: { value: "500" } })
    })

    const saveBtn = screen.getByRole("button", { name: /Salvar configuração/i })
    expect(saveBtn).not.toBeDisabled()
  })

  it("salvar envia apenas os campos globais (sem wazuh_*)", async () => {
    setupMocks(makeConfig())
    render(
      <MemoryRouter>
        <ConfigPage />
      </MemoryRouter>,
    )
    const collectorTab = await screen.findByRole("tab", { name: /Coleta & Entrega/i })
    await act(async () => { fireEvent.click(collectorTab) })
    await waitFor(() => screen.getByTestId("destinations-cta"))

    const batchInput = screen.getByLabelText(/Tamanho do lote/i)
    await act(async () => {
      fireEvent.change(batchInput, { target: { value: "300" } })
    })

    const saveBtn = screen.getByRole("button", { name: /Salvar configuração/i })
    await act(async () => { fireEvent.click(saveBtn) })

    await waitFor(() => {
      const call = mockedApi.updateCollectorConfig.mock.calls[0]?.[0]
      expect(call).toBeDefined()
      expect(call).toHaveProperty("collector_batch_size", 300)
      // campos wazuh_* não devem ser enviados pelo form global
      expect(call).not.toHaveProperty("wazuh_syslog_host")
      expect(call).not.toHaveProperty("wazuh_dispatch_mode")
      expect(call).not.toHaveProperty("wazuh_syslog_format")
    })
  })
})
