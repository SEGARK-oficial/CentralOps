/**
 * Testes do CollectorConfigForm — seções globais mantidas após a migração
 * (remoção da seção "Destino Wazuh" do form global).
 * Cobre: dedupe TTL, rate-limits por vendor, concorrência por domínio,
 * botão Testar (global), acessibilidade via teclado no campo TTL.
 */

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { ConfigPage } from "@/pages/ConfigPage"
import * as api from "@/services/api"
import { useAuth } from "@/contexts/AuthContext"
import type { CollectorConfig, EmailConfig } from "@/types"
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

const baseCollectorConfig: CollectorConfig = {
  id: 1,
  is_persisted: true,
  config_version: "1.0.0",
  updated_at: "2026-04-25T10:00:00Z",
  wazuh_syslog_host: "wazuh.interno",
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

function setupDefaultMocks(config: CollectorConfig = baseCollectorConfig) {
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

async function renderConfigCollectorTab() {
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

describe("CollectorConfigForm — seções globais", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setupDefaultMocks()
  })

  it("renderiza o campo TTL de dedupe com o valor padrão", async () => {
    await renderConfigCollectorTab()

    const ttlInput = screen.getByLabelText(/TTL de dedupe/i)
    expect(ttlInput).toBeInTheDocument()
    expect((ttlInput as HTMLInputElement).value).toBe("7")
  })

  it("renderiza o campo flush após (segundos)", async () => {
    await renderConfigCollectorTab()

    const flushInput = screen.getByLabelText(/Flush após/i)
    expect(flushInput).toBeInTheDocument()
    expect((flushInput as HTMLInputElement).value).toBe("5")
  })

  it("renderiza a tabela de concorrência por domínio", async () => {
    await renderConfigCollectorTab()

    expect(screen.getByText("Concorrência por domínio")).toBeInTheDocument()
  })

  it("renderiza a tabela de rate-limits por vendor", async () => {
    await renderConfigCollectorTab()

    expect(screen.getByText("Rate-limits por vendor")).toBeInTheDocument()
  })

  it("alterar TTL de dedupe habilita o botão Salvar", async () => {
    await renderConfigCollectorTab()

    const ttlInput = screen.getByLabelText(/TTL de dedupe/i)
    await act(async () => {
      fireEvent.change(ttlInput, { target: { value: "14" } })
    })

    const saveBtn = screen.getByRole("button", { name: /Salvar configuração/i })
    expect(saveBtn).not.toBeDisabled()
  })

  it("salvar envia collector_batch_size corretamente", async () => {
    await renderConfigCollectorTab()

    const batchInput = screen.getByLabelText(/Tamanho do lote/i)
    await act(async () => {
      fireEvent.change(batchInput, { target: { value: "400" } })
    })

    const saveBtn = screen.getByRole("button", { name: /Salvar configuração/i })
    await act(async () => { fireEvent.click(saveBtn) })

    await waitFor(() => {
      expect(mockedApi.updateCollectorConfig).toHaveBeenCalledWith(
        expect.objectContaining({ collector_batch_size: 400 }),
      )
    })
  })

  it("botão Testar chama o endpoint de test", async () => {
    await renderConfigCollectorTab()

    const testBtn = screen.getByRole("button", { name: /^Testar$/i })
    expect(testBtn).not.toBeDisabled()

    await act(async () => { fireEvent.click(testBtn) })

    await waitFor(() => {
      expect(mockedApi.testCollectorConfig).toHaveBeenCalled()
    })
  })

  it("botão Testar fica desabilitado quando há mudanças não salvas", async () => {
    await renderConfigCollectorTab()

    const batchInput = screen.getByLabelText(/Tamanho do lote/i)
    await act(async () => {
      fireEvent.change(batchInput, { target: { value: "999" } })
    })

    const testBtn = screen.getByRole("button", { name: /^Testar$/i })
    expect(testBtn).toBeDisabled()
  })

  it("campo TTL é acessível via teclado (focus/change)", async () => {
    await renderConfigCollectorTab()

    const ttlInput = screen.getByLabelText(/TTL de dedupe/i)
    ttlInput.focus()
    expect(document.activeElement).toBe(ttlInput)

    await act(async () => {
      fireEvent.change(ttlInput, { target: { value: "30" } })
    })
    expect((ttlInput as HTMLInputElement).value).toBe("30")
  })

  it("a seção Wazuh não aparece mais no form global", async () => {
    await renderConfigCollectorTab()

    expect(screen.queryByText("Destino Wazuh")).not.toBeInTheDocument()
    expect(screen.queryByTestId("wazuh-syslog-format-select")).not.toBeInTheDocument()
    expect(screen.queryByTestId("wazuh-format-test-button")).not.toBeInTheDocument()
  })
})
