/**
 * PolicyVersionsModal tests.
 *
 * Cobre:
 * - Estado habilitado/desabilitado e o botão de alternar (desabilitado sem versão publicada).
 * - Dry-run: exige ao menos uma regra; roda e mostra o resultado.
 * - Publicação: exige regra e mensagem de commit; sucesso limpa o editor e mostra Notice.
 * - Histórico: lista versões, rollback funciona, versão vigente não mostra reverter.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll } from "vitest"
import { PolicyVersionsModal } from "../PolicyVersionsModal"
import * as api from "@/services/api"
import type {
  EnricherCatalogItem,
  EnrichmentDryRunResponse,
  EnrichmentPolicy,
  EnrichmentPolicyVersion,
  EnrichmentTable,
} from "@/services/api"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

const enrichers: EnricherCatalogItem[] = [
  {
    name: "table_cidr",
    label: "Tabela CIDR",
    category: "table",
    description: "",
    icon_id: null,
    docs_url: null,
    tier: "core",
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
]

const tables: EnrichmentTable[] = []

const enabledPolicy: EnrichmentPolicy = {
  id: "p1",
  organization_id: 1,
  name: "contexto-de-ativo",
  description: null,
  enabled: true,
  current_version_id: "v2",
  rule_count: 2,
}

const disabledNoVersionPolicy: EnrichmentPolicy = {
  id: "p2",
  organization_id: 1,
  name: "sem-versao",
  description: null,
  enabled: false,
  current_version_id: null,
  rule_count: 0,
}

const versions: EnrichmentPolicyVersion[] = [
  {
    id: "v2",
    version_number: 2,
    commit_message: "adiciona regra de rede",
    author_user_id: 1,
    created_at: "2026-08-01T00:00:00Z",
    is_current: true,
    summary: { version: 2, rule_count: 2, has_local: true, has_remote: false, rules: [] },
  },
  {
    id: "v1",
    version_number: 1,
    commit_message: "versão inicial",
    author_user_id: 1,
    created_at: "2026-07-01T00:00:00Z",
    is_current: false,
    summary: { version: 1, rule_count: 1, has_local: true, has_remote: false, rules: [] },
  },
]

const dryRunResponse: EnrichmentDryRunResponse = {
  ok: true,
  summary: { version: 0, rule_count: 1, has_local: true, has_remote: false, rules: [] },
  enriched: { normalized: { src_endpoint: { ip: "10.0.5.7" } } },
  hits: { "regra-1": 1 },
  misses: {},
  skipped: {},
  errors: {},
  bytes_added: 42,
}

describe("PolicyVersionsModal", () => {
  it("mostra estado ativo e permite desabilitar", async () => {
    mockedApi.listEnrichmentPolicyVersions.mockResolvedValue(versions)
    render(
      <PolicyVersionsModal
        open
        policy={enabledPolicy}
        enrichers={enrichers}
        tables={tables}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    )

    expect(await screen.findByText(/está ATIVA/i)).toBeInTheDocument()
    const toggle = screen.getByRole("button", { name: "Desabilitar" })
    expect(toggle).not.toBeDisabled()
  })

  it("desabilita o botão de habilitar quando não há versão publicada", async () => {
    mockedApi.listEnrichmentPolicyVersions.mockResolvedValue([])
    render(
      <PolicyVersionsModal
        open
        policy={disabledNoVersionPolicy}
        enrichers={enrichers}
        tables={tables}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    )

    expect(await screen.findByText("Publique uma versão antes de habilitar.")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Habilitar" })).toBeDisabled()
  })

  it("alterna habilitado/desabilitado e chama onChanged", async () => {
    mockedApi.listEnrichmentPolicyVersions.mockResolvedValue(versions)
    mockedApi.setEnrichmentPolicyEnabled.mockResolvedValue({ ...enabledPolicy, enabled: false })
    const onChanged = vi.fn()
    render(
      <PolicyVersionsModal
        open
        policy={enabledPolicy}
        enrichers={enrichers}
        tables={tables}
        onClose={vi.fn()}
        onChanged={onChanged}
      />,
    )

    fireEvent.click(await screen.findByRole("button", { name: "Desabilitar" }))

    await waitFor(() => expect(mockedApi.setEnrichmentPolicyEnabled).toHaveBeenCalledWith("p1", false))
    expect(onChanged).toHaveBeenCalled()
  })

  it("exige ao menos uma regra antes de testar", async () => {
    mockedApi.listEnrichmentPolicyVersions.mockResolvedValue([])
    render(
      <PolicyVersionsModal
        open
        policy={disabledNoVersionPolicy}
        enrichers={enrichers}
        tables={tables}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    )
    await screen.findByText("Nenhuma versão publicada ainda.")

    fireEvent.click(screen.getByRole("button", { name: "Testar" }))

    expect(await screen.findByText("Adicione ao menos uma regra antes de testar.")).toBeInTheDocument()
    expect(mockedApi.dryRunEnrichment).not.toHaveBeenCalled()
  })

  it("roda o dry-run e mostra o resultado", async () => {
    mockedApi.listEnrichmentPolicyVersions.mockResolvedValue([])
    mockedApi.dryRunEnrichment.mockResolvedValue(dryRunResponse)
    render(
      <PolicyVersionsModal
        open
        policy={disabledNoVersionPolicy}
        enrichers={enrichers}
        tables={tables}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    )
    await screen.findByText("Nenhuma versão publicada ainda.")

    fireEvent.click(screen.getByTestId("add-rule"))
    fireEvent.click(screen.getByRole("button", { name: "Testar" }))

    expect(await screen.findByTestId("dry-run-result")).toBeInTheDocument()
    expect(screen.getByText("+42 bytes")).toBeInTheDocument()
    expect(mockedApi.dryRunEnrichment).toHaveBeenCalledWith(
      expect.objectContaining({ rules: expect.arrayContaining([expect.objectContaining({ enricher: "table_cidr" })]) }),
    )
  })

  it("exige regra e mensagem de commit antes de publicar", async () => {
    mockedApi.listEnrichmentPolicyVersions.mockResolvedValue([])
    render(
      <PolicyVersionsModal
        open
        policy={disabledNoVersionPolicy}
        enrichers={enrichers}
        tables={tables}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    )
    await screen.findByText("Nenhuma versão publicada ainda.")

    fireEvent.click(screen.getByRole("button", { name: "Publicar versão" }))
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/Adicione ao menos uma regra/i))

    fireEvent.click(screen.getByTestId("add-rule"))
    fireEvent.click(screen.getByRole("button", { name: "Publicar versão" }))
    expect(await screen.findByText(/Descreva a mudança antes de publicar/i)).toBeInTheDocument()
    expect(mockedApi.commitEnrichmentPolicyVersion).not.toHaveBeenCalled()
  })

  it("publica com sucesso, limpa o editor e chama onChanged", async () => {
    mockedApi.listEnrichmentPolicyVersions.mockResolvedValue([])
    mockedApi.commitEnrichmentPolicyVersion.mockResolvedValue({
      id: "v3",
      version_number: 3,
      commit_message: "teste",
      author_user_id: 1,
      created_at: "2026-08-09T00:00:00Z",
      is_current: true,
      summary: { version: 3, rule_count: 1, has_local: true, has_remote: false, rules: [] },
    })
    const onChanged = vi.fn()
    render(
      <PolicyVersionsModal
        open
        policy={disabledNoVersionPolicy}
        enrichers={enrichers}
        tables={tables}
        onClose={vi.fn()}
        onChanged={onChanged}
      />,
    )
    await screen.findByText("Nenhuma versão publicada ainda.")

    fireEvent.click(screen.getByTestId("add-rule"))
    fireEvent.change(screen.getByLabelText("Mensagem do commit"), { target: { value: "teste" } })
    fireEvent.click(screen.getByRole("button", { name: "Publicar versão" }))

    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(mockedApi.commitEnrichmentPolicyVersion).toHaveBeenCalledWith(
      "p2",
      expect.objectContaining({ commit_message: "teste" }),
    )
    expect(await screen.findByText("Versão publicada")).toBeInTheDocument()
    expect(screen.getByTestId("rules-empty")).toBeInTheDocument()
  })

  it("lista o histórico e reverte para uma versão antiga", async () => {
    mockedApi.listEnrichmentPolicyVersions.mockResolvedValue(versions)
    mockedApi.rollbackEnrichmentPolicy.mockResolvedValue(enabledPolicy)
    const onChanged = vi.fn()
    render(
      <PolicyVersionsModal
        open
        policy={enabledPolicy}
        enrichers={enrichers}
        tables={tables}
        onClose={vi.fn()}
        onChanged={onChanged}
      />,
    )

    expect(await screen.findByText("versão inicial")).toBeInTheDocument()
    expect(screen.getByText("adiciona regra de rede")).toBeInTheDocument()

    const rollbackButtons = screen.getAllByRole("button", { name: "Reverter para esta" })
    expect(rollbackButtons).toHaveLength(1)
    fireEvent.click(rollbackButtons[0])

    await waitFor(() => expect(mockedApi.rollbackEnrichmentPolicy).toHaveBeenCalledWith("p1", "v1"))
    expect(onChanged).toHaveBeenCalled()
  })
})
