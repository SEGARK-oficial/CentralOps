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

import { render, screen, fireEvent, waitFor, within } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest"
import EnrichmentPage from "@/pages/EnrichmentPage"
import * as api from "@/services/api"
import type { EnricherCatalogItem, EnrichmentPolicy, EnrichmentTable } from "@/services/api"
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
}: { tables?: EnrichmentTable[]; policies?: EnrichmentPolicy[] } = {}) {
  mockedApi.listEnrichers.mockResolvedValue(enrichers)
  mockedApi.listEnrichmentTables.mockResolvedValue(tables)
  mockedApi.listEnrichmentPolicies.mockResolvedValue(policies)
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedApi.listEnrichmentTableVersions.mockResolvedValue([])
  mockedApi.listEnrichmentPolicyVersions.mockResolvedValue([])
})

describe("EnrichmentPage", () => {
  it("carrega catálogo, tabelas e políticas ao montar", async () => {
    mockLoad({ tables: [table], policies: [policy] })
    render(<EnrichmentPage />)

    await waitFor(() => expect(mockedApi.listEnrichers).toHaveBeenCalled())
    expect(mockedApi.listEnrichmentTables).toHaveBeenCalled()
    expect(mockedApi.listEnrichmentPolicies).toHaveBeenCalled()
    expect(await screen.findByText("Tabela CIDR")).toBeInTheDocument()
  })

  it("mostra o catálogo agrupado por categoria com selo de egresso", async () => {
    mockLoad()
    render(<EnrichmentPage />)

    expect(await screen.findByText("Tabela CIDR")).toBeInTheDocument()
    expect(screen.getByText("VirusTotal")).toBeInTheDocument()
    expect(screen.getByText("sem egresso")).toBeInTheDocument()
    expect(screen.getByText("envia a terceiro")).toBeInTheDocument()
    expect(screen.getByText(/1 fonte envia indicadores/i)).toBeInTheDocument()
  })

  it("mostra estado vazio na aba tabelas e abre o modal de criação", async () => {
    mockLoad()
    render(<EnrichmentPage />)
    await screen.findByText("Tabela CIDR")

    fireEvent.click(screen.getByRole("tab", { name: /Tabelas/i }))

    expect(await screen.findByText("Nenhuma tabela ainda")).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole("button", { name: "Nova tabela" })[0])

    expect(await screen.findByRole("dialog", { name: "Nova tabela" })).toBeInTheDocument()
  })

  it("abre o modal de versões ao clicar em uma tabela", async () => {
    mockLoad({ tables: [table] })
    render(<EnrichmentPage />)
    await screen.findByText("Tabela CIDR")

    fireEvent.click(screen.getByRole("tab", { name: /Tabelas/i }))
    fireEvent.click(await screen.findByTestId("table-card-rede-corp"))

    expect(await screen.findByRole("dialog", { name: "Versões — rede-corp" })).toBeInTheDocument()
  })

  it("apaga uma tabela via ConfirmDialog sem abrir o modal de versões", async () => {
    mockLoad({ tables: [table] })
    mockedApi.deleteEnrichmentTable.mockResolvedValue(undefined)
    render(<EnrichmentPage />)
    await screen.findByText("Tabela CIDR")

    fireEvent.click(screen.getByRole("tab", { name: /Tabelas/i }))
    const card = await screen.findByTestId("table-card-rede-corp")
    fireEvent.click(within(card).getByRole("button", { name: "Apagar tabela" }))

    expect(screen.queryByRole("dialog", { name: "Versões — rede-corp" })).not.toBeInTheDocument()
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
    await screen.findByText("Tabela CIDR")

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
    await screen.findByText("Tabela CIDR")

    fireEvent.click(screen.getByRole("tab", { name: /Políticas/i }))

    expect(await screen.findByText("Nenhuma política ainda")).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole("button", { name: "Nova política" })[0])

    expect(await screen.findByRole("dialog", { name: "Nova política" })).toBeInTheDocument()
  })

  it("abre o modal de versões ao clicar em uma política", async () => {
    mockLoad({ policies: [policy] })
    render(<EnrichmentPage />)
    await screen.findByText("Tabela CIDR")

    fireEvent.click(screen.getByRole("tab", { name: /Políticas/i }))
    fireEvent.click(await screen.findByTestId("policy-card-contexto-de-ativo"))

    expect(await screen.findByRole("dialog", { name: "Versões — contexto-de-ativo" })).toBeInTheDocument()
  })

  it("mostra ErrorState com retry quando o carregamento falha", async () => {
    mockedApi.listEnrichers.mockRejectedValue(new Error("falha ao carregar enrichers"))
    mockedApi.listEnrichmentTables.mockResolvedValue([])
    mockedApi.listEnrichmentPolicies.mockResolvedValue([])
    render(<EnrichmentPage />)

    expect(await screen.findByText(/falha ao carregar enrichers/i)).toBeInTheDocument()

    mockLoad({ tables: [table] })
    fireEvent.click(screen.getByRole("button", { name: "Tentar novamente" }))

    expect(await screen.findByText("Tabela CIDR")).toBeInTheDocument()
  })
})
