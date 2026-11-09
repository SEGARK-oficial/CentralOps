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
  // Plataforma SEM bloco customizado (caminho genérico de `auth_fields`
  // dinâmicos, ex.: ninjaone/microsoft_defender no catálogo real) — usada nos
  // testes de foco do R2-8.3 para o campo DINÂMICO (chave só existe em runtime).
  {
    platform: "ninjaone",
    display_name: "NinjaOne",
    category: "RMM",
    description: "RMM genérico.",
    icon_id: null,
    docs_url: null,
    auth_fields: [
      { key: "api_token", label: "API Token", type: "secret", required: true },
      { key: "instance_region", label: "Região da instância", type: "select", required: true, options: ["us", "eu"] },
    ],
    streams: [],
    supports_test: false,
  },
  // Só-coleta com colunas genéricas (client_id/tenant_id/base_url) + segredo.
  {
    platform: "azure",
    display_name: "Microsoft Azure",
    category: "Cloud",
    description: "Activity Log, Entra e recursos.",
    icon_id: null,
    docs_url: null,
    auth_fields: [
      { key: "tenant_id", label: "Tenant ID", type: "string", required: true },
      { key: "client_id", label: "Client ID", type: "string", required: true },
      { key: "client_secret", label: "Client Secret", type: "secret", required: true },
      { key: "base_url", label: "Fonte: Workspace ID ou namespace do Event Hubs", type: "string", required: false },
    ],
    streams: [],
    supports_test: true,
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
    expect(screen.getByText(/cada tenant desta credencial/i)).toBeInTheDocument()
    expect(screen.queryByLabelText(/Região/)).not.toBeInTheDocument()
  })

  it("Community: tile sophos_partner ganha badge 'Enterprise'; card base sophos não", async () => {
    renderForm()
    const partnerTile = await screen.findByTestId("tile-card-sophos_partner")
    expect(within(partnerTile).getByText("Enterprise")).toBeInTheDocument()
    const baseTile = screen.getByTestId("tile-card-sophos")
    expect(within(baseTile).queryByText("Enterprise")).not.toBeInTheDocument()
  })

  it("Community: selecionar sophos_partner exibe o aviso 'Importar tenants exige Enterprise'", async () => {
    renderForm()
    fireEvent.click(await screen.findByTestId("tile-card-sophos_partner"))
    expect(screen.getByText(/Importar tenants exige Enterprise/i)).toBeInTheDocument()
  })

  it("Enterprise: sem badge no tile partner e sem aviso de licença", async () => {
    editionState.isEnterprise = true
    renderForm()
    const partnerTile = await screen.findByTestId("tile-card-sophos_partner")
    expect(within(partnerTile).queryByText("Enterprise")).not.toBeInTheDocument()
    fireEvent.click(partnerTile)
    expect(screen.queryByText(/Importar tenants exige Enterprise/i)).not.toBeInTheDocument()
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

// ── R2-8.3: foco no 1º campo inválido (useFirstInvalidFocus) ────────────────
//
// Antes, `noValidate` + um único banner genérico no topo era tudo: quem usa
// teclado/leitor de tela lia "preencha X" e tinha que CAÇAR manualmente qual
// dos N campos do form era o culpado. Cada teste abaixo prova as DUAS metades
// do contrato: o banner aparece E o foco/aria-invalid vão para o campo certo.
describe("IntegrationForm — R2-8.3 (foco no campo inválido)", () => {
  it("nome vazio foca o campo Nome", async () => {
    renderForm()
    await screen.findByTestId("tile-card-sophos")
    fireEvent.change(screen.getByLabelText(/Organização/), { target: { value: "10" } })
    fireEvent.click(screen.getByRole("button", { name: /Criar integração/i }))

    expect(await screen.findByRole("alert")).toHaveTextContent(/nome/i)
    expect(document.activeElement).toBe(screen.getByLabelText(/^Nome/))
    expect(screen.getByLabelText(/^Nome/)).toHaveAttribute("aria-invalid", "true")
  })

  it("organização vazia foca o select de Organização", async () => {
    renderForm()
    await screen.findByTestId("tile-card-sophos")
    fireEvent.change(screen.getByLabelText(/Nome/), { target: { value: "Sophos T" } })
    fireEvent.click(screen.getByRole("button", { name: /Criar integração/i }))

    expect(await screen.findByRole("alert")).toHaveTextContent(/organiza/i)
    expect(document.activeElement).toBe(screen.getByLabelText(/Organização/))
    expect(screen.getByLabelText(/Organização/)).toHaveAttribute("aria-invalid", "true")
  })

  it("Sophos: Client ID vazio foca o campo Client ID (não fica só no banner)", async () => {
    renderForm()
    await screen.findByTestId("tile-card-sophos")
    fireEvent.change(screen.getByLabelText(/Nome/), { target: { value: "Sophos T" } })
    fireEvent.change(screen.getByLabelText(/Organização/), { target: { value: "10" } })
    fireEvent.click(screen.getByRole("button", { name: /Criar integração/i }))

    expect(await screen.findByRole("alert")).toBeInTheDocument()
    expect(document.activeElement).toBe(screen.getByLabelText(/^Client ID/))
    expect(screen.getByLabelText(/^Client ID/)).toHaveAttribute("aria-invalid", "true")
  })

  it("Wazuh: Indexer URL vazio foca o campo Indexer URL", async () => {
    renderForm()
    fireEvent.click(await screen.findByTestId("tile-card-wazuh"))
    fireEvent.change(screen.getByLabelText(/Nome/), { target: { value: "Wazuh 1" } })
    fireEvent.change(screen.getByLabelText(/Organização/), { target: { value: "10" } })
    fireEvent.click(screen.getByRole("button", { name: /Criar integração/i }))

    expect(await screen.findByRole("alert")).toBeInTheDocument()
    expect(document.activeElement).toBe(screen.getByLabelText(/Indexer URL/))
    expect(screen.getByLabelText(/Indexer URL/)).toHaveAttribute("aria-invalid", "true")
  })

  it("Wazuh: Manager habilitado sem URL foca o campo Manager URL", async () => {
    renderForm()
    fireEvent.click(await screen.findByTestId("tile-card-wazuh"))
    fireEvent.change(screen.getByLabelText(/Nome/), { target: { value: "Wazuh 1" } })
    fireEvent.change(screen.getByLabelText(/Organização/), { target: { value: "10" } })
    fireEvent.change(screen.getByLabelText(/Indexer URL/), { target: { value: "https://idx:9200" } })
    fireEvent.change(screen.getByLabelText(/Usuário do Indexer/), { target: { value: "iu" } })
    fireEvent.change(screen.getByLabelText(/Senha do Indexer/), { target: { value: "ip" } })
    fireEvent.click(screen.getByLabelText(/Habilitar Manager/))
    fireEvent.click(screen.getByRole("button", { name: /Criar integração/i }))

    expect(await screen.findByRole("alert")).toBeInTheDocument()
    expect(document.activeElement).toBe(screen.getByLabelText(/Manager URL/))
    expect(screen.getByLabelText(/Manager URL/)).toHaveAttribute("aria-invalid", "true")
  })

  it("campo dinâmico (chave de runtime) obrigatório vazio foca o próprio campo", async () => {
    renderForm()
    fireEvent.click(await screen.findByTestId("tile-card-ninjaone"))
    fireEvent.change(screen.getByLabelText(/Nome/), { target: { value: "Ninja 1" } })
    fireEvent.change(screen.getByLabelText(/Organização/), { target: { value: "10" } })
    fireEvent.click(screen.getByRole("button", { name: /Criar integração/i }))

    // `getByLabelText` com string exata não bate: o label carrega o "*" de
    // obrigatório como filho ("API Token*") — daí o regex, igual aos outros
    // campos do form (`/Nome/`, `/Client ID/`, …).
    expect(await screen.findByRole("alert")).toHaveTextContent(/API Token/)
    expect(document.activeElement).toBe(screen.getByLabelText(/^API Token/))
    expect(screen.getByLabelText(/^API Token/)).toHaveAttribute("aria-invalid", "true")
  })

  it("campo dinâmico do tipo select (opção inválida/vazia) foca o próprio select", async () => {
    renderForm()
    fireEvent.click(await screen.findByTestId("tile-card-ninjaone"))
    fireEvent.change(screen.getByLabelText(/Nome/), { target: { value: "Ninja 1" } })
    fireEvent.change(screen.getByLabelText(/Organização/), { target: { value: "10" } })
    fireEvent.change(screen.getByLabelText(/^API Token/), { target: { value: "tok" } })
    fireEvent.click(screen.getByRole("button", { name: /Criar integração/i }))

    expect(await screen.findByRole("alert")).toHaveTextContent(/Região da instância/)
    expect(document.activeElement).toBe(screen.getByLabelText(/^Região da instância/))
    expect(screen.getByLabelText(/^Região da instância/)).toHaveAttribute("aria-invalid", "true")
  })

  it("corrigir o campo e reenviar limpa o erro anterior (não empilha aria-invalid)", async () => {
    const onSubmit = renderForm()
    await screen.findByTestId("tile-card-sophos")
    fireEvent.click(screen.getByRole("button", { name: /Criar integração/i }))
    await screen.findByRole("alert")
    expect(screen.getByLabelText(/^Nome/)).toHaveAttribute("aria-invalid", "true")

    fireEvent.change(screen.getByLabelText(/^Nome/), { target: { value: "Sophos T" } })
    fireEvent.change(screen.getByLabelText(/Organização/), { target: { value: "10" } })
    fireEvent.change(screen.getByLabelText(/^Client ID/), { target: { value: "cid" } })
    fireEvent.change(screen.getByLabelText(/Client Secret/), { target: { value: "csec" } })
    fireEvent.click(screen.getByRole("button", { name: /Criar integração/i }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
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
  name: "Wazuh Matriz",
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

// SEC-04: docs_url vem do catálogo de plataformas (plugin-driven, ADR-0006/0007)
// — não é digitado pelo usuário nesta tela, mas `safeExternalHref` é a defesa
// em profundidade contra um catálogo malicioso/mal formado injetando
// `javascript:`/`data:` no link "Ver documentação".
// Filha de parceiro Sophos (parent_integration_id != null): a credencial é a do
// PARCEIRO. O form pedia client_id (obrigatório) e o enviava sempre — o operador
// gravava uma chave própria que o coletor ignorava. Agora o form não a oferece.
const SOPHOS_TENANT: Integration = {
  id: 77,
  organization_id: 10,
  organization_name: "Org A",
  name: "Sophos Cliente",
  platform: "sophos",
  kind: "tenant",
  is_active: true,
  is_authenticated: true,
  auth_status: "healthy",
  capabilities: [],
  client_id: "cid-proprio",
  region: "eu03",
}

function renderSophosEdit(integration: Integration) {
  const onSubmit = vi.fn().mockResolvedValue(undefined)
  render(
    <IntegrationForm
      mode="edit"
      integration={integration}
      organizations={ORGS}
      onSubmit={onSubmit}
    />,
  )
  return onSubmit
}

describe("IntegrationForm — Sophos filha de parceiro (edição)", () => {
  it("filha: sem Client ID/Secret, com aviso de que a credencial vem do parceiro", async () => {
    renderSophosEdit({ ...SOPHOS_TENANT, parent_integration_id: 5 })
    await screen.findByLabelText(/Região/)

    expect(screen.queryByLabelText(/Client ID/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/Client Secret/)).not.toBeInTheDocument()
    const notice = screen.getByTestId("partner-child-credentials-notice")
    expect(notice).toHaveTextContent(/credencial do parceiro/i)
    expect(notice).toHaveTextContent(/desvincule a organização/i)
  })

  it("filha: salvar não manda client_id nem client_secret (só nome, ativo e região)", async () => {
    const onSubmit = renderSophosEdit({ ...SOPHOS_TENANT, parent_integration_id: 5 })
    await screen.findByLabelText(/Região/)

    fireEvent.change(screen.getByLabelText(/Região/), { target: { value: "us01" } })
    fireEvent.click(screen.getByRole("button", { name: /Salvar alterações/i }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    const payload = onSubmit.mock.calls[0][0] as Record<string, unknown>
    expect(payload).toMatchObject({ name: "Sophos Cliente", is_active: true, region: "us01" })
    expect(payload).not.toHaveProperty("client_id")
    expect(payload).not.toHaveProperty("client_secret")
  })

  it("tenant com chave própria (sem parent): continua com Client ID/Secret e sem o aviso", async () => {
    const onSubmit = renderSophosEdit(SOPHOS_TENANT)
    expect(await screen.findByLabelText(/Client ID/)).toHaveValue("cid-proprio")
    expect(screen.getByLabelText(/Client Secret/)).toBeInTheDocument()
    expect(screen.queryByTestId("partner-child-credentials-notice")).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText(/Client Secret/), { target: { value: "novo" } })
    fireEvent.click(screen.getByRole("button", { name: /Salvar alterações/i }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(onSubmit.mock.calls[0][0]).toMatchObject({ client_id: "cid-proprio", client_secret: "novo" })
  })
})

describe("IntegrationForm — SEC-04 (docs_url)", () => {
  it("docs_url http(s) válido renderiza o link de documentação", async () => {
    mockedApi.getProviderPlatforms.mockResolvedValue([
      { ...CATALOG[0], docs_url: "https://docs.example.com/sophos" },
    ])
    renderForm()
    await screen.findByTestId("tile-card-sophos")
    const link = await screen.findByRole("link", { name: "Ver documentação de configuração" })
    expect(link).toHaveAttribute("href", "https://docs.example.com/sophos")
  })

  it("docs_url com esquema javascript: NÃO renderiza o link", async () => {
    mockedApi.getProviderPlatforms.mockResolvedValue([
      // eslint-disable-next-line no-script-url -- payload de teste: prova que o esquema É rejeitado.
      { ...CATALOG[0], docs_url: "javascript:alert(1)" },
    ])
    renderForm()
    await screen.findByTestId("tile-card-sophos")
    expect(screen.queryByRole("link", { name: "Ver documentação de configuração" })).not.toBeInTheDocument()
  })
})

// Edição de plataforma DINÂMICA (sem bloco próprio). O update não tinha ramo para
// elas: caía no do Wazuh, mandava indexer_* vazios e descartava os auth_fields —
// 200 sem gravar. Caso real: Fonte do Azure "configurada pela UI" e o coletor
// seguia com "nenhuma fonte configurada".
const AZURE_INTEGRATION: Integration = {
  id: 56,
  organization_id: 10,
  organization_name: "Org A",
  name: "Azure Matriz",
  platform: "azure",
  is_active: true,
  is_authenticated: false,
  auth_status: "unknown",
  capabilities: [],
  tenant_id: "11111111-1111-1111-1111-111111111111",
  client_id: "22222222-2222-2222-2222-222222222222",
  base_url: null,
}

function renderDynamicEdit(integration: Integration) {
  const onSubmit = vi.fn().mockResolvedValue(undefined)
  render(
    <IntegrationForm mode="edit" integration={integration} organizations={ORGS} onSubmit={onSubmit} />,
  )
  return onSubmit
}

const FONTE = /Fonte: Workspace ID/

describe("IntegrationForm — edição de plataforma dinâmica", () => {
  beforeEach(() => {
    mockedApi.getIntegrationCollectionFilters.mockResolvedValue({
      integration_id: 56, platform: "azure", filters: {}, available_filters: {},
    })
  })

  it("abre com as colunas gravadas preenchidas e o segredo vazio", async () => {
    renderDynamicEdit({ ...AZURE_INTEGRATION, base_url: "33333333-3333-3333-3333-333333333333" })
    expect(await screen.findByLabelText(FONTE)).toHaveValue("33333333-3333-3333-3333-333333333333")
    expect(screen.getByLabelText(/Tenant ID/)).toHaveValue(AZURE_INTEGRATION.tenant_id)
    expect(screen.getByLabelText(/Client ID/)).toHaveValue(AZURE_INTEGRATION.client_id)
    expect(screen.getByLabelText(/Client Secret/)).toHaveValue("")
  })

  it("preencher a Fonte envia base_url — sem exigir o segredo de novo e sem campos do Wazuh", async () => {
    const onSubmit = renderDynamicEdit(AZURE_INTEGRATION)
    fireEvent.change(await screen.findByLabelText(FONTE), {
      target: { value: " 44444444-4444-4444-4444-444444444444 " },
    })
    fireEvent.click(screen.getByRole("button", { name: /Salvar alterações/i }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    const payload = onSubmit.mock.calls[0][0] as Record<string, unknown>
    expect(payload).toMatchObject({
      base_url: "44444444-4444-4444-4444-444444444444",
      tenant_id: AZURE_INTEGRATION.tenant_id,
      client_id: AZURE_INTEGRATION.client_id,
    })
    expect(payload).not.toHaveProperty("client_secret")
    expect(payload).not.toHaveProperty("indexer_url")
    expect(payload).not.toHaveProperty("manager_url")
    expect(payload).not.toHaveProperty("verify_ssl")
  })

  it("digitar um segredo novo o envia (rotação)", async () => {
    const onSubmit = renderDynamicEdit(AZURE_INTEGRATION)
    fireEvent.change(await screen.findByLabelText(/Client Secret/), { target: { value: "novo-segredo" } })
    fireEvent.click(screen.getByRole("button", { name: /Salvar alterações/i }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(onSubmit.mock.calls[0][0]).toMatchObject({ client_secret: "novo-segredo" })
  })

  it("apagar uma coluna que tinha valor envia null (limpar é intencional)", async () => {
    const onSubmit = renderDynamicEdit({ ...AZURE_INTEGRATION, base_url: "meu-namespace" })
    fireEvent.change(await screen.findByLabelText(FONTE), { target: { value: "" } })
    fireEvent.click(screen.getByRole("button", { name: /Salvar alterações/i }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(onSubmit.mock.calls[0][0]).toMatchObject({ base_url: null })
  })

  it("coluna obrigatória apagada bloqueia o submit", async () => {
    const onSubmit = renderDynamicEdit(AZURE_INTEGRATION)
    fireEvent.change(await screen.findByLabelText(/Tenant ID/), { target: { value: "" } })
    fireEvent.click(screen.getByRole("button", { name: /Salvar alterações/i }))

    await waitFor(() => expect(screen.getByLabelText(/Tenant ID/)).toHaveFocus())
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it("segredo e chave sem coluna vazios não bloqueiam nem são enviados", async () => {
    // ninjaone no catálogo do teste: api_token (secret) e instance_region
    // (select sem coluna) — nenhum dos dois volta do backend.
    const onSubmit = renderDynamicEdit({
      ...AZURE_INTEGRATION, id: 57, platform: "ninjaone", tenant_id: undefined, client_id: undefined,
    })
    await screen.findByLabelText(/API Token/)
    fireEvent.click(screen.getByRole("button", { name: /Salvar alterações/i }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    const payload = onSubmit.mock.calls[0][0] as Record<string, unknown>
    expect(payload).not.toHaveProperty("api_token")
    expect(payload).not.toHaveProperty("instance_region")
    expect(payload).not.toHaveProperty("indexer_url")
  })
})

describe("IntegrationForm — AWS com papel cross-account", () => {
  const AWS: ProviderPlatformRead = {
    platform: "aws_cloudtrail",
    display_name: "AWS CloudTrail",
    category: "Cloud-audit",
    description: "CloudTrail via S3.",
    icon_id: "aws",
    docs_url: null,
    auth_fields: [
      { key: "client_id", label: "ARN do papel ou Access Key ID", type: "string", required: true },
      { key: "secret_access_key", label: "Secret Access Key", type: "secret", required: false },
      { key: "base_url", label: "Bucket S3", type: "string", required: true },
    ],
    streams: [],
    supports_test: true,
    auth_setup: "aws_assume_role",
  }
  const SETUP = {
    organization_id: 10,
    external_id: "centralops-1111-2222",
    trust_principal_arn: "arn:aws:iam::111122223333:role/centralops-broker",
    principal_error: null,
    template: { AWSTemplateFormatVersion: "2010-09-09" },
    template_filename: "centralops-readonly-role.json",
  }

  beforeEach(() => {
    mockedApi.getProviderPlatforms.mockResolvedValue([...CATALOG, AWS])
    mockedApi.getAwsRoleSetup.mockResolvedValue(SETUP)
  })

  it("só busca o External ID depois de escolher a organização, e mostra o da org", async () => {
    renderForm()
    fireEvent.click(await screen.findByTestId("tile-card-aws_cloudtrail"))
    const panel = await screen.findByTestId("aws-role-setup")
    expect(within(panel).getByText(/Selecione a organização/)).toBeInTheDocument()
    expect(mockedApi.getAwsRoleSetup).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText(/Organização/), { target: { value: "10" } })
    expect(await within(panel).findByText("centralops-1111-2222")).toBeInTheDocument()
    expect(mockedApi.getAwsRoleSetup).toHaveBeenCalledWith(10)
    expect(within(panel).getByText(SETUP.trust_principal_arn)).toBeInTheDocument()
  })

  it("sem identidade AWS no servidor, avisa em vez de mostrar principal vazio", async () => {
    mockedApi.getAwsRoleSetup.mockResolvedValue({
      ...SETUP,
      trust_principal_arn: null,
      principal_error: "GetCallerIdentity: credenciais AWS ausentes (NoCredentialsError).",
    })
    renderForm()
    fireEvent.click(await screen.findByTestId("tile-card-aws_cloudtrail"))
    fireEvent.change(screen.getByLabelText(/Organização/), { target: { value: "10" } })
    expect(await screen.findByText(/Identidade AWS do CentralOps não encontrada/)).toBeInTheDocument()
    expect(screen.getByText(/NoCredentialsError/)).toBeInTheDocument()
  })

  it("o teste de conexão leva a organização (o backend usa o External ID dela)", async () => {
    renderForm()
    fireEvent.click(await screen.findByTestId("tile-card-aws_cloudtrail"))
    fireEvent.change(screen.getByLabelText(/Organização/), { target: { value: "10" } })
    fireEvent.change(screen.getByLabelText(/ARN do papel/), {
      target: { value: "arn:aws:iam::123456789012:role/CentralOpsReadOnly" },
    })
    fireEvent.click(screen.getByRole("button", { name: /Testar conexão/i }))
    await waitFor(() => expect(mockedApi.testProviderConnection).toHaveBeenCalledTimes(1))
    const [platform, config] = mockedApi.testProviderConnection.mock.calls[0]
    expect(platform).toBe("aws_cloudtrail")
    expect(config).toMatchObject({
      client_id: "arn:aws:iam::123456789012:role/CentralOpsReadOnly",
      organization_id: 10,
    })
    // O External ID nunca sai do navegador para o backend: o servidor o resolve.
    expect(config).not.toHaveProperty("external_id")
  })

  it("plataforma sem auth_setup não mostra o painel", async () => {
    renderForm()
    fireEvent.click(await screen.findByTestId("tile-card-azure"))
    expect(screen.queryByTestId("aws-role-setup")).not.toBeInTheDocument()
  })
})

describe("IntegrationForm — AWS Organization (pai com filhas Enterprise)", () => {
  const AWS_ORG: ProviderPlatformRead = {
    platform: "aws_guardduty_org",
    display_name: "Amazon GuardDuty — AWS Organization",
    category: "Cloud-audit",
    description: "Uma integração por conta.",
    icon_id: "aws",
    docs_url: null,
    auth_fields: [
      { key: "client_id", label: "ARN do papel (conta de gerenciamento)", type: "string", required: true },
    ],
    streams: [],
    supports_test: true,
    auth_setup: "aws_assume_role",
    variant: "organization",
  }

  beforeEach(() => {
    mockedApi.getProviderPlatforms.mockResolvedValue([...CATALOG, AWS_ORG])
    mockedApi.getAwsRoleSetup.mockResolvedValue({
      organization_id: 10,
      external_id: "centralops-x",
      trust_principal_arn: "arn:aws:iam::111122223333:role/broker",
      principal_error: null,
      template: {},
      template_filename: "t.json",
    })
  })

  it("Community: o card do pai ganha o badge Enterprise pelo variant do catálogo", async () => {
    renderForm()
    const tile = await screen.findByTestId("tile-card-aws_guardduty_org")
    expect(within(tile).getByText("Enterprise")).toBeInTheDocument()
  })

  it("explica que as contas viram integrações nesta organização e que exige Enterprise", async () => {
    renderForm()
    fireEvent.click(await screen.findByTestId("tile-card-aws_guardduty_org"))
    expect(screen.getByText(/lista as contas da AWS Organization/)).toBeInTheDocument()
    expect(screen.getByText(/Criar as integrações das contas exige Enterprise/)).toBeInTheDocument()
    // O aviso do Sophos (Organization por tenant) não se aplica aqui.
    expect(screen.queryByText(/cada tenant desta credencial/i)).not.toBeInTheDocument()
  })
})
