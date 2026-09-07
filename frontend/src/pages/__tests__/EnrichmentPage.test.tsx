/**
 * EnrichmentPage tests.
 *
 * Cobre:
 * - Carrega catálogo/tabelas/políticas ao montar.
 * - Aba catálogo mostra os enrichers agrupados, com selo de egresso.
 * - Aba tabelas: estado vazio com ação de criar; card abre o modal de versões;
 *   apagar tabela passa pelo ConfirmDialog e chama a API.
 * - Aba políticas: estado vazio com ação de criar; card abre o modal de versões.
 * - Estado de erro mostra ErrorState com retry.
 */

import { render as rtlRender, screen, fireEvent, waitFor, within } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest"
import EnrichmentPage from "@/pages/EnrichmentPage"
import * as api from "@/services/api"
import type {
  EnricherCatalogItem,
  EnrichmentPolicy,
  EnrichmentSource,
  EnrichmentTable,
} from "@/services/api"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

vi.mock("@/services/api")

const platformContextValue: {
  organizations: Array<{ id: number; name: string }>
  selectedOrgId: number | null
} = {
  organizations: [{ id: 1, name: "Acme Corp" }],
  selectedOrgId: 1,
}

vi.mock("@/contexts/PlatformContext", () => ({
  usePlatform: () => platformContextValue,
}))

/**
 * A página passou a conter navegação de verdade (a aba de visão geral manda o
 * operador para Configuração › Enriquecimento quando o passo bloqueado é de
 * administrador global), então o componente exige um Router. Envolver aqui é o
 * acoplamento correto: na aplicação ele sempre está dentro de um.
 */
const render = (ui: React.ReactElement) =>
  rtlRender(<MemoryRouter>{ui}</MemoryRouter>)

const mockedApi = vi.mocked(api)

const enrichers: EnricherCatalogItem[] = [
  {
    name: "table_cidr",
    label: "Tabela CIDR",
    category: "table",
    description: "Casa por faixa CIDR.",
    icon_id: null,
    docs_url: null,
    tier: "stable",
    order: 0,
    mode: "local",
    key_kinds: ["ip"],
    supports_bulk: true,
    suggested_ttl_s: 3600,
    license: "core",
    egress: "none",
    required_secrets: [],
    output_fields: { site: "string" },
  },
  {
    name: "virustotal",
    label: "VirusTotal",
    category: "threat_intel",
    description: "Consulta reputação de indicadores.",
    icon_id: null,
    docs_url: null,
    tier: "beta",
    order: 1,
    mode: "remote",
    key_kinds: ["ip", "domain", "file_hash"],
    supports_bulk: true,
    suggested_ttl_s: 3600,
    license: "core",
    egress: "third_party",
    required_secrets: ["VIRUSTOTAL_API_KEY"],
    output_fields: { reputation: "string" },
  },
]

const table: EnrichmentTable = {
  id: "t1",
  organization_id: 1,
  name: "rede-corp",
  description: "Rede interna",
  match_mode: "cidr",
  key_kind: "ip",
  current_version_id: "v1",
  entry_count: 10,
  approx_bytes: 2048,
}

const policy: EnrichmentPolicy = {
  id: "p1",
  organization_id: 1,
  name: "contexto-de-ativo",
  description: "Marca ativos conhecidos",
  enabled: true,
  current_version_id: "v1",
  rule_count: 2,
}

function mockLoad({
  tables = [],
  policies = [],
  sources = [],
}: {
  tables?: EnrichmentTable[]
  policies?: EnrichmentPolicy[]
  sources?: EnrichmentSource[]
} = {}) {
  mockedApi.listEnrichers.mockResolvedValue(enrichers)
  mockedApi.listEnrichmentTables.mockResolvedValue(tables)
  mockedApi.listEnrichmentPolicies.mockResolvedValue(policies)
  mockedApi.listEnrichmentSources.mockResolvedValue(sources)
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedApi.listEnrichmentKeySources.mockResolvedValue({
    organization_id: 1,
    from_active_mappings: false,
    suggestions: [],
  })
  mockedApi.listEnrichmentTableVersions.mockResolvedValue([])
  mockedApi.listEnrichmentPolicyVersions.mockResolvedValue([])
  mockedApi.listEnrichmentSources.mockResolvedValue([])
  // O modal de versões hidrata o editor a partir da versão vigente ao abrir.
  mockedApi.getEnrichmentPolicyVersion.mockResolvedValue({
    id: "v1",
    version_number: 1,
    rules: [],
  })
  // O modal de tabela busca o CORPO da versão vigente para diferenciar contra o
  // arquivo importado.
  mockedApi.getEnrichmentTableVersion.mockResolvedValue({
    id: "v1",
    version_number: 1,
    entry_count: 0,
    approx_bytes: 0,
    rows: {},
  })
  // A aba de entrada agora é a visão geral, que consulta a prontidão. Sem
  // estes defaults todo teste começaria num ErrorState.
  mockedApi.getEnrichmentReadiness.mockResolvedValue({
    organization_id: 1,
    ready: true,
    steps: [],
    active_policy_name: "contexto-de-ativo",
  })
  mockedApi.getEnrichmentMetrics.mockResolvedValue({
    organization_id: 1,
    range_minutes: 60,
    policy_name: "contexto-de-ativo",
    rules: [],
  })
  mockedApi.getEnrichmentActivity.mockResolvedValue({
    organization_id: 1,
    entries: [],
  })
})

/** Espera a página montar na aba de entrada (visão geral). */
const aguardaCarregar = () => screen.findByTestId("readiness-panel")

/** Vai para o catálogo, que deixou de ser a aba padrão. */
const abreCatalogo = async () => {
  await aguardaCarregar()
  fireEvent.click(screen.getByRole("tab", { name: /Catálogo/i }))
  return screen.findByText("Tabela CIDR")
}

describe("EnrichmentPage", () => {
  it("carrega catálogo, tabelas e políticas ao montar", async () => {
    mockLoad({ tables: [table], policies: [policy] })
    render(<EnrichmentPage />)

    await waitFor(() => expect(mockedApi.listEnrichers).toHaveBeenCalled())
    expect(mockedApi.listEnrichmentTables).toHaveBeenCalled()
    expect(mockedApi.listEnrichmentPolicies).toHaveBeenCalled()
    expect(await abreCatalogo()).toBeInTheDocument()
  })

  it("mostra o catálogo agrupado por categoria com selo de egresso", async () => {
    mockLoad()
    render(<EnrichmentPage />)

    expect(await abreCatalogo()).toBeInTheDocument()
    expect(screen.getByText("VirusTotal")).toBeInTheDocument()
    expect(screen.getByText("sem egresso")).toBeInTheDocument()
    expect(screen.getByText("envia a terceiro")).toBeInTheDocument()
    expect(screen.getByText(/1 fonte envia indicadores/i)).toBeInTheDocument()
  })

  it("mostra estado vazio na aba tabelas e abre o modal de criação", async () => {
    mockLoad()
    render(<EnrichmentPage />)
    await aguardaCarregar()

    fireEvent.click(screen.getByRole("tab", { name: /Tabelas/i }))

    expect(await screen.findByText("Nenhuma tabela ainda")).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole("button", { name: "Nova tabela" })[0])

    expect(await screen.findByRole("dialog", { name: "Nova tabela" })).toBeInTheDocument()
  })

  it("abre o modal de versões ao clicar em uma tabela", async () => {
    mockLoad({ tables: [table] })
    render(<EnrichmentPage />)
    await aguardaCarregar()

    fireEvent.click(screen.getByRole("tab", { name: /Tabelas/i }))
    fireEvent.click(await screen.findByTestId("table-card-rede-corp"))

    expect(await screen.findByRole("dialog", { name: "Versões de rede-corp" })).toBeInTheDocument()
  })

  it("apaga uma tabela via ConfirmDialog sem abrir o modal de versões", async () => {
    mockLoad({ tables: [table] })
    mockedApi.deleteEnrichmentTable.mockResolvedValue(undefined)
    render(<EnrichmentPage />)
    await aguardaCarregar()

    fireEvent.click(screen.getByRole("tab", { name: /Tabelas/i }))
    const card = await screen.findByTestId("table-card-rede-corp")
    fireEvent.click(within(card).getByRole("button", { name: "Apagar tabela" }))

    expect(screen.queryByRole("dialog", { name: "Versões de rede-corp" })).not.toBeInTheDocument()
    const confirmDialog = await screen.findByRole("dialog", { name: "Apagar tabela" })
    fireEvent.click(within(confirmDialog).getByRole("button", { name: "Excluir" }))

    await waitFor(() => expect(mockedApi.deleteEnrichmentTable).toHaveBeenCalledWith("t1"))
  })

  it("mostra erro do backend ao apagar sem fechar o dialog", async () => {
    mockLoad({ tables: [table] })
    mockedApi.deleteEnrichmentTable.mockRejectedValue(
      new Error("tabela em uso pela política 'contexto-de-ativo'"),
    )
    render(<EnrichmentPage />)
    await aguardaCarregar()

    fireEvent.click(screen.getByRole("tab", { name: /Tabelas/i }))
    const card = await screen.findByTestId("table-card-rede-corp")
    fireEvent.click(within(card).getByRole("button", { name: "Apagar tabela" }))
    const confirmDialog = await screen.findByRole("dialog", { name: "Apagar tabela" })
    fireEvent.click(within(confirmDialog).getByRole("button", { name: "Excluir" }))

    expect(await screen.findByText(/tabela em uso pela política/i)).toBeInTheDocument()
    expect(screen.getByRole("dialog", { name: "Apagar tabela" })).toBeInTheDocument()
  })

  it("mostra estado vazio na aba políticas e abre o modal de criação", async () => {
    mockLoad()
    render(<EnrichmentPage />)
    await aguardaCarregar()

    fireEvent.click(screen.getByRole("tab", { name: /Políticas/i }))

    expect(await screen.findByText("Nenhuma política ainda")).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole("button", { name: "Nova política" })[0])

    expect(await screen.findByRole("dialog", { name: "Nova política" })).toBeInTheDocument()
  })

  it("abre o modal de versões ao clicar em uma política", async () => {
    mockLoad({ policies: [policy] })
    render(<EnrichmentPage />)
    await aguardaCarregar()

    fireEvent.click(screen.getByRole("tab", { name: /Políticas/i }))
    fireEvent.click(await screen.findByTestId("policy-card-contexto-de-ativo"))

    expect(await screen.findByRole("dialog", { name: "Versões de contexto-de-ativo" })).toBeInTheDocument()
  })

  it("mostra ErrorState com retry quando o carregamento falha", async () => {
    mockedApi.listEnrichers.mockRejectedValue(new Error("falha ao carregar enrichers"))
    mockedApi.listEnrichmentTables.mockResolvedValue([])
    mockedApi.listEnrichmentPolicies.mockResolvedValue([])
    mockedApi.listEnrichmentSources.mockResolvedValue([])
    render(<EnrichmentPage />)

    expect(await screen.findByText(/falha ao carregar enrichers/i)).toBeInTheDocument()

    mockLoad({ tables: [table] })
    fireEvent.click(screen.getByRole("button", { name: "Tentar novamente" }))

    // Depois do retry a página volta para a aba de entrada; o catálogo está a
    // um clique dali.
    expect(await abreCatalogo()).toBeInTheDocument()
  })
})

describe("EnrichmentPage — fontes configuradas", () => {
  const source: EnrichmentSource = {
    id: "s1",
    organization_id: 1,
    name: "vt-prod",
    enricher: "virustotal",
    description: "Reputação em produção",
    config: { max_keys_per_batch: 25 },
    secret_configured: true,
    enabled: true,
    shared_organization_ids: [],
  }

  it("mostra estado vazio e abre o formulário de criação", async () => {
    mockLoad()
    render(<EnrichmentPage />)
    await aguardaCarregar()

    fireEvent.click(screen.getByRole("tab", { name: /Fontes/i }))

    expect(await screen.findByText("Nenhuma fonte configurada")).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole("button", { name: "Nova fonte" })[0])

    expect(await screen.findByRole("dialog", { name: "Nova fonte" })).toBeInTheDocument()
  })

  it("nunca renderiza a referência do segredo, só o indicador booleano", async () => {
    mockLoad({ sources: [source] })
    const { container } = render(<EnrichmentPage />)
    await aguardaCarregar()

    fireEvent.click(screen.getByRole("tab", { name: /Fontes/i }))
    await screen.findByTestId("source-row-vt-prod")

    expect(screen.getByText("credencial cadastrada")).toBeInTheDocument()
    // O cofre decifra qualquer ciphertext sem olhar org: a referência não pode
    // chegar ao cliente, senão é copiável para outra organização.
    expect(container.innerHTML).not.toContain("secret_ref")
  })

  it("fonte é alcançável por teclado", async () => {
    // A lista deixou de ser um grid de cards com `tabindex` e `onKeyDown`
    // manuais e passou a ser uma tabela cujo nome é um <button> nativo. A
    // propriedade testada é a mesma — dá para chegar e acionar sem mouse —, só
    // que agora vem do elemento certo, em vez de ser reconstruída à mão.
    mockLoad({ sources: [source] })
    render(<EnrichmentPage />)
    await aguardaCarregar()

    fireEvent.click(screen.getByRole("tab", { name: /Fontes/i }))
    const trigger = await screen.findByTestId("source-row-vt-prod")
    expect(trigger.tagName).toBe("BUTTON")

    fireEvent.click(trigger)
    expect(
      await screen.findByRole("dialog", { name: /Editar fonte/i }),
    ).toBeInTheDocument()
  })
})
