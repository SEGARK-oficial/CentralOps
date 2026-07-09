/**
 * IntegrationForm — testes do bloco de credenciais por plataforma.
 *
 * Cobre as decisões de UX:
 * - Sophos: o card base "sophos" é TENANT-ONLY (sem o antigo toggle
 *   "Tipo de conta Sophos"); Partner/Organization são tiles próprios
 *   (sophos_partner/sophos_organization) que caem no caminho genérico.
 * - Wazuh: o INDEXER é a fonte obrigatória (alertas/detecções); o MANAGER é
 *   opcional (saúde/agentes) atrás do toggle "Habilitar Manager".
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest"
import { IntegrationForm } from "@/components/integrations/IntegrationForm"
import * as api from "@/services/api"
import i18n from "@/i18n"
import type { Organization, ProviderPlatformRead } from "@/types"

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

// jsdom's default navigator.language is "en-US", which the app's language
// detector picks up over the pt fallback — force pt here so assertions below
// (written against the PT catalog copy) match what a PT-BR user actually sees.
beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

const CATALOG: ProviderPlatformRead[] = [
  {
    platform: "sophos",
    display_name: "Sophos Central",
    category: "EDR / XDR",
    description: "Sophos Central — tenant único.",
    icon_id: "shield",
    docs_url: null,
    auth_fields: [
      { key: "client_id", label: "Client ID", type: "string", required: true },
      { key: "client_secret", label: "Client Secret", type: "secret", required: true },
      { key: "region", label: "Região", type: "string", required: false },
    ],
    streams: [],
    supports_test: true,
  },
  {
    platform: "sophos_partner",
    display_name: "Sophos Central — Partner",
    category: "EDR / XDR",
    description: "MSSP — descobre tenants.",
    icon_id: "shield",
    docs_url: null,
    auth_fields: [
      { key: "client_id", label: "Client ID", type: "string", required: true },
      { key: "client_secret", label: "Client Secret", type: "secret", required: true },
    ],
    streams: [],
    supports_test: true,
  },
  {
    platform: "wazuh",
    display_name: "Wazuh",
    category: "SIEM",
    description: "Wazuh Manager + Indexer.",
    icon_id: "server",
    docs_url: null,
    auth_fields: [],
    streams: [],
    supports_test: false,
  },
]

const ORGS: Organization[] = [
  { id: 10, name: "Org A", slug: "org-a", is_active: true, integration_count: 0 },
]

function renderForm(onSubmit = vi.fn().mockResolvedValue(undefined)) {
  render(<IntegrationForm mode="create" organizations={ORGS} onSubmit={onSubmit} />)
  return onSubmit
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedApi.getProviderPlatforms.mockResolvedValue(CATALOG)
  mockedApi.testProviderConnection.mockResolvedValue({ ok: true, detail: "ok" })
})

describe("IntegrationForm — Sophos", () => {
  it("card base 'sophos' não exibe mais o toggle 'Tipo de conta Sophos'", async () => {
    renderForm()
    await screen.findByTestId("tile-card-sophos")
    expect(screen.queryByText("Tipo de conta Sophos")).not.toBeInTheDocument()
    // tenant-only: Client ID + Client Secret + Região
    expect(screen.getByLabelText(/Client ID/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Client Secret/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Região/)).toBeInTheDocument()
  })

  it("submit do tenant envia client_id/secret/region e NÃO envia kind", async () => {
    const onSubmit = renderForm()
    await screen.findByTestId("tile-card-sophos")
    fireEvent.change(screen.getByLabelText(/Nome/), { target: { value: "Sophos T" } })
    fireEvent.change(screen.getByLabelText(/Organização/), { target: { value: "10" } })
    fireEvent.change(screen.getByLabelText(/Client ID/), { target: { value: "cid" } })
    fireEvent.change(screen.getByLabelText(/Client Secret/), { target: { value: "csec" } })
    fireEvent.change(screen.getByLabelText(/Região/), { target: { value: "us-east" } })
    fireEvent.click(screen.getByRole("button", { name: /Criar integração/i }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    const payload = onSubmit.mock.calls[0][0] as Record<string, unknown>
    expect(payload).toMatchObject({
      platform: "sophos",
      client_id: "cid",
      client_secret: "csec",
      region: "us-east",
    })
    expect(payload.kind).toBeUndefined()
  })

  it("tile Partner mostra nota de auto-discovery e NÃO mostra campo Região", async () => {
    renderForm()
    fireEvent.click(await screen.findByTestId("tile-card-sophos_partner"))
    expect(screen.getByText(/todos os tenants/i)).toBeInTheDocument()
    expect(screen.queryByLabelText(/Região/)).not.toBeInTheDocument()
  })
})

describe("IntegrationForm — Wazuh", () => {
  it("Indexer é obrigatório e visível; Manager é opcional atrás do toggle", async () => {
    renderForm()
    fireEvent.click(await screen.findByTestId("tile-card-wazuh"))

    // Indexer (fonte) — obrigatório e já visível
    expect(screen.getByText("Indexer API")).toBeInTheDocument()
    expect(screen.getByText("Obrigatório")).toBeInTheDocument()
    expect(screen.getByLabelText(/Indexer URL/)).toBeInTheDocument()

    // Manager — opcional, campos escondidos até habilitar
    expect(screen.getByText("Manager API")).toBeInTheDocument()
    expect(screen.getByText("Opcional")).toBeInTheDocument()
    expect(screen.queryByLabelText(/Manager URL/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByLabelText(/Habilitar Manager/))
    expect(screen.getByLabelText(/Manager URL/)).toBeInTheDocument()
  })

  it("bloqueia o submit quando o Indexer URL não foi preenchido", async () => {
    const onSubmit = renderForm()
    fireEvent.click(await screen.findByTestId("tile-card-wazuh"))
    fireEvent.change(screen.getByLabelText(/Nome/), { target: { value: "Wazuh 1" } })
    fireEvent.change(screen.getByLabelText(/Organização/), { target: { value: "10" } })
    fireEvent.click(screen.getByRole("button", { name: /Criar integração/i }))

    expect(await screen.findByText(/Wazuh requer Indexer URL/)).toBeInTheDocument()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it("submit Indexer-only envia indexer_* e NÃO envia campos do Manager", async () => {
    const onSubmit = renderForm()
    fireEvent.click(await screen.findByTestId("tile-card-wazuh"))
    fireEvent.change(screen.getByLabelText(/Nome/), { target: { value: "Wazuh 1" } })
    fireEvent.change(screen.getByLabelText(/Organização/), { target: { value: "10" } })
    fireEvent.change(screen.getByLabelText(/Indexer URL/), { target: { value: "https://idx:9200" } })
    fireEvent.change(screen.getByLabelText(/Usuário do Indexer/), { target: { value: "iu" } })
    fireEvent.change(screen.getByLabelText(/Senha do Indexer/), { target: { value: "ip" } })
    fireEvent.click(screen.getByRole("button", { name: /Criar integração/i }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    const payload = onSubmit.mock.calls[0][0] as Record<string, unknown>
    expect(payload).toMatchObject({
      platform: "wazuh",
      indexer_url: "https://idx:9200",
      indexer_username: "iu",
      indexer_password: "ip",
    })
    expect(payload.manager_url).toBeUndefined()
    expect(payload.manager_api_username).toBeUndefined()
  })
})
