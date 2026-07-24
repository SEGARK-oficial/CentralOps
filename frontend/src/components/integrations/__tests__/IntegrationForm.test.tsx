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

import { render, screen, fireEvent, waitFor, within } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest"
import { IntegrationForm } from "@/components/integrations/IntegrationForm"
import * as api from "@/services/api"
import i18n from "@/i18n"
import type {
  Integration,
  IntegrationCollectionFilters,
  Organization,
  ProviderPlatformRead,
} from "@/types"

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

// Estado de edição mutável por teste (default Community — mesmo padrão do
// OrganizationsPage.test). O form usa useEdition p/ badge "Enterprise" nos
// tiles partner/organization.
const editionState = vi.hoisted(() => ({ isEnterprise: false }))
vi.mock("@/contexts/EditionContext", () => ({
  useEdition: () => ({
    edition: editionState.isEnterprise ? "enterprise" : "community",
    features: [],
    plan: null,
    seats: null,
    maxOrganizations: null,
    expiresAt: null,
    expiredInGrace: false,
    isEnterprise: editionState.isEnterprise,
    loading: false,
    error: null,
    hasFeature: () => false,
    refresh: vi.fn(),
  }),
}))

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
  editionState.isEnterprise = false
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

  it("Community: tile sophos_partner ganha badge 'Enterprise'; card base sophos não", async () => {
    renderForm()
    const partnerTile = await screen.findByTestId("tile-card-sophos_partner")
    expect(within(partnerTile).getByText("Enterprise")).toBeInTheDocument()
    const baseTile = screen.getByTestId("tile-card-sophos")
    expect(within(baseTile).queryByText("Enterprise")).not.toBeInTheDocument()
  })

  it("Community: selecionar sophos_partner exibe o aviso 'requer licença Enterprise'", async () => {
    renderForm()
    fireEvent.click(await screen.findByTestId("tile-card-sophos_partner"))
    expect(screen.getByText(/requer licença Enterprise/i)).toBeInTheDocument()
  })

  it("Enterprise: sem badge no tile partner e sem aviso de licença", async () => {
    editionState.isEnterprise = true
    renderForm()
    const partnerTile = await screen.findByTestId("tile-card-sophos_partner")
    expect(within(partnerTile).queryByText("Enterprise")).not.toBeInTheDocument()
    fireEvent.click(partnerTile)
    expect(screen.queryByText(/requer licença Enterprise/i)).not.toBeInTheDocument()
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

  it("catálogo declara filtro no stream: badge aparece e a criação avisa que é depois", async () => {
    mockedApi.getProviderPlatforms.mockResolvedValue([
      {
        ...CATALOG[2],
        streams: [
          {
            stream: "detections",
            schedule_seconds: 120,
            filters: [
              {
                key: "min_rule_level",
                label: "Nível mínimo",
                type: "int_range",
                default: 0,
                min: 0,
                max: 16,
              },
            ],
          },
        ],
      },
    ])
    renderForm()
    fireEvent.click(await screen.findByTestId("tile-card-wazuh"))
    expect(await screen.findByText("1 filtro de coleta")).toBeInTheDocument()
    expect(screen.getByText(/configurados na edição/i)).toBeInTheDocument()
    // Sem integração ainda não há onde pendurar filtro — a seção não existe.
    expect(screen.queryByTestId("collection-filters-section")).not.toBeInTheDocument()
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

// ── Filtros de coleta na edição ──────────────────────────────────────────────
//
// O schema vem do backend; o formulário não conhece vendor nenhum. Aqui prova-se
// o CONTRATO: carrega ao abrir, salva por endpoint próprio ANTES do update e
// aborta tudo se o backend recusar o filtro.

// Indexer já configurado: sem isso a validação do próprio formulário barra o
// submit antes de chegar nos filtros, e os testes abaixo mediriam outra coisa.
const WAZUH_INTEGRATION: Integration = {
  id: 42,
  organization_id: 10,
  organization_name: "Org A",
  name: "Wazuh Zaffari",
  platform: "wazuh",
  is_active: true,
  is_authenticated: true,
  auth_status: "healthy",
  capabilities: [],
  indexer_url: "https://idx:9200",
  indexer_username: "admin",
  indexer_password_configured: true,
  verify_ssl: true,
}

const FILTERS_PAYLOAD: IntegrationCollectionFilters = {
  integration_id: 42,
  platform: "wazuh",
  filters: {},
  available_filters: {
    detections: [
      {
        key: "min_rule_level",
        label: "Nível mínimo da regra do Wazuh",
        type: "int_range",
        default: 0,
        min: 0,
        max: 16,
        help_text: "Só coleta alertas neste nível ou acima.",
        warning_text: "O que for filtrado aqui NUNCA entra na plataforma.",
      },
    ],
  },
}

function renderEdit(onSubmit = vi.fn().mockResolvedValue(undefined)) {
  render(
    <IntegrationForm
      mode="edit"
      integration={WAZUH_INTEGRATION}
      organizations={ORGS}
      onSubmit={onSubmit}
    />,
  )
  return onSubmit
}

describe("IntegrationForm — filtros de coleta (edição)", () => {
  it("renderiza a seção a partir do schema que o backend devolveu", async () => {
    mockedApi.getIntegrationCollectionFilters.mockResolvedValue(FILTERS_PAYLOAD)
    renderEdit()
    expect(await screen.findByTestId("collection-filters-section")).toBeInTheDocument()
    expect(mockedApi.getIntegrationCollectionFilters).toHaveBeenCalledWith(42)
    expect(screen.getByText("Nível mínimo da regra do Wazuh")).toBeInTheDocument()
    expect(screen.getByTestId("collection-filter-input-min_rule_level")).toHaveValue(0)
  })

  it("plataforma sem filtro declarado: a seção não existe", async () => {
    mockedApi.getIntegrationCollectionFilters.mockResolvedValue({
      ...FILTERS_PAYLOAD,
      available_filters: {},
    })
    renderEdit()
    await screen.findByLabelText(/Indexer URL/)
    expect(screen.queryByTestId("collection-filters-section")).not.toBeInTheDocument()
  })

  it("sem alteração de filtro, o PUT não é chamado", async () => {
    mockedApi.getIntegrationCollectionFilters.mockResolvedValue(FILTERS_PAYLOAD)
    const onSubmit = renderEdit()
    await screen.findByTestId("collection-filters-section")
    fireEvent.click(screen.getByRole("button", { name: /Salvar alterações/i }))
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(mockedApi.updateIntegrationCollectionFilters).not.toHaveBeenCalled()
  })

  it("filtro ligado e confirmado vai no PUT próprio, antes do update", async () => {
    mockedApi.getIntegrationCollectionFilters.mockResolvedValue(FILTERS_PAYLOAD)
    mockedApi.updateIntegrationCollectionFilters.mockResolvedValue({
      ...FILTERS_PAYLOAD,
      filters: { detections: { min_rule_level: 7 } },
    })
    const onSubmit = renderEdit()
    await screen.findByTestId("collection-filters-section")

    const input = screen.getByTestId("collection-filter-input-min_rule_level")
    fireEvent.change(input, { target: { value: "7" } })
    fireEvent.blur(input)
    fireEvent.click(await screen.findByTestId("collection-filters-confirm-dialog-confirm"))

    fireEvent.click(screen.getByRole("button", { name: /Salvar alterações/i }))

    await waitFor(() => expect(mockedApi.updateIntegrationCollectionFilters).toHaveBeenCalledWith(42, {
      detections: { min_rule_level: 7 },
    }))
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
  })

  it("422 no filtro aborta o submit inteiro (nada é alterado)", async () => {
    mockedApi.getIntegrationCollectionFilters.mockResolvedValue(FILTERS_PAYLOAD)
    mockedApi.updateIntegrationCollectionFilters.mockRejectedValue(
      new Error("min_rule_level: 99 fora de [0, 16]"),
    )
    const onSubmit = renderEdit()
    await screen.findByTestId("collection-filters-section")

    const input = screen.getByTestId("collection-filter-input-min_rule_level")
    fireEvent.change(input, { target: { value: "7" } })
    fireEvent.blur(input)
    fireEvent.click(await screen.findByTestId("collection-filters-confirm-dialog-confirm"))
    fireEvent.click(screen.getByRole("button", { name: /Salvar alterações/i }))

    expect(await screen.findByText(/não foram salvos e nada mais foi alterado/i)).toBeInTheDocument()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it("falha ao LER os filtros não desenha a seção vazia (isso afirmaria 'sem filtro')", async () => {
    mockedApi.getIntegrationCollectionFilters.mockRejectedValue(new Error("403"))
    renderEdit()
    expect(await screen.findByText(/Filtros de coleta indisponíveis/i)).toBeInTheDocument()
    expect(screen.queryByTestId("collection-filters-section")).not.toBeInTheDocument()
  })
})
