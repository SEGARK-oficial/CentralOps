/**
 * PolicyVersionsModal tests.
 *
 * Cobre:
 * - Estado habilitado/desabilitado e o botão de alternar (desabilitado sem versão publicada).
 * - Dry-run: exige ao menos uma regra; roda e mostra o resultado.
 * - Publicação: exige regra e mensagem de commit; sucesso MANTÉM as regras no editor.
 * - Hidratação: o editor abre com as regras da versão vigente (publicar substitui tudo).
 * - Histórico: lista versões, rollback funciona, versão vigente não mostra reverter.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest"
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

/** Regras da v2, o que a hidratação deve trazer para o editor. */
const v2Rules = [
  {
    id: "regra-rede",
    enricher: "table_cidr",
    table: "redes",
    key: { source: "normalized.src_endpoint.ip", kind: "ip" },
    outputs: [{ from: "site", target: "_centralops.enrichment.src.site" }],
    // Campo que o editor estruturado NÃO renderiza. Está aqui de propósito:
    // se a hidratação perdê-lo, republicar apaga a condição em silêncio.
    when: { exists: "normalized.src_endpoint.ip" },
  },
]

beforeEach(() => {
  // O arquivo não tem `clearMocks` global: sem isto, `mock.calls[0]` de um
  // teste é a chamada de OUTRO, e as asserções passam ou falham por acidente.
  vi.clearAllMocks()
  mockedApi.getEnrichmentPolicyVersion.mockResolvedValue({
    id: "v2",
    version_number: 2,
    rules: v2Rules as never,
  })
})

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

    expect(await screen.findByText(/Política ativa/i)).toBeInTheDocument()
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

  it("publica com sucesso, mantém as regras no editor e chama onChanged", async () => {
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
    // As regras FICAM. Limpar aqui reintroduziria o bug: publicar duas vezes
    // seguidas gravaria uma versão vazia por cima da que acabou de sair.
    expect(screen.queryByTestId("rules-empty")).not.toBeInTheDocument()
    expect(screen.getByTestId("rule-card-0")).toBeInTheDocument()
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
  it("abre com as regras da versão vigente, não em branco", async () => {
    // O bug original: o editor abria vazio e publicar SUBSTITUI a versão
    // inteira. Adicionar uma regra a uma política de duas publicava uma versão
    // com uma só, e a outra sumia sem erro nem aviso.
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

    await waitFor(() =>
      expect(mockedApi.getEnrichmentPolicyVersion).toHaveBeenCalledWith("p1", "v2"),
    )
    expect(await screen.findByTestId("rule-card-0")).toBeInTheDocument()
    expect(screen.queryByTestId("rules-empty")).not.toBeInTheDocument()
    expect(screen.getByTestId("editing-from-version")).toHaveTextContent("v2")
  })

  it("preserva campos que o editor estruturado não renderiza", async () => {
    // `when` não tem widget ainda. Se a publicação o perdesse, editar qualquer
    // outro campo apagaria a condição que decide se a regra roda.
    mockedApi.listEnrichmentPolicyVersions.mockResolvedValue(versions)
    mockedApi.commitEnrichmentPolicyVersion.mockResolvedValue({
      ...versions[0],
      id: "v3",
      version_number: 3,
    })
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
    await screen.findByTestId("rule-card-0")

    fireEvent.change(screen.getByLabelText("Id da regra"), { target: { value: "renomeada" } })
    fireEvent.change(screen.getByLabelText("Mensagem do commit"), { target: { value: "renomeia" } })
    fireEvent.click(screen.getByRole("button", { name: "Publicar versão" }))

    await waitFor(() => expect(mockedApi.commitEnrichmentPolicyVersion).toHaveBeenCalled())
    const [, payload] = mockedApi.commitEnrichmentPolicyVersion.mock.calls[0]
    expect(payload.rules[0].id).toBe("renomeada")
    expect(payload.rules[0].when).toEqual({ exists: "normalized.src_endpoint.ip" })
  })

  it("carrega uma versão antiga no editor sem reverter", async () => {
    // Carregar é diferente de reverter: reverter troca a vigente na hora,
    // carregar abre para revisar e publicar uma nova por cima.
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
    await screen.findByText("versão inicial")

    mockedApi.getEnrichmentPolicyVersion.mockResolvedValue({
      id: "v1",
      version_number: 1,
      rules: [v2Rules[0]] as never,
    })
    fireEvent.click(screen.getByTestId("load-version-1"))

    await waitFor(() =>
      expect(mockedApi.getEnrichmentPolicyVersion).toHaveBeenCalledWith("p1", "v1"),
    )
    expect(mockedApi.rollbackEnrichmentPolicy).not.toHaveBeenCalled()
    expect(await screen.findByTestId("editing-from-version")).toHaveTextContent("v1")
  })

  it("política sem versão publicada abre em branco", async () => {
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

    expect(await screen.findByTestId("rules-empty")).toBeInTheDocument()
    expect(mockedApi.getEnrichmentPolicyVersion).not.toHaveBeenCalled()
  })
  it("não re-hidrata quando a página devolve a política recarregada", async () => {
    // Publicar chama `onChanged`, que recarrega a lista de políticas na página
    // e entrega um OBJETO NOVO com o mesmo id. Se o efeito dependesse da
    // referência, ele re-hidrataria por cima do que o operador começou a
    // editar logo depois de publicar.
    mockedApi.listEnrichmentPolicyVersions.mockResolvedValue(versions)
    const { rerender } = render(
      <PolicyVersionsModal
        open
        policy={enabledPolicy}
        enrichers={enrichers}
        tables={tables}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    )
    await screen.findByTestId("rule-card-0")
    expect(mockedApi.getEnrichmentPolicyVersion).toHaveBeenCalledTimes(1)

    // Mesma política, objeto novo: é exatamente o que `onChanged` produz.
    rerender(
      <PolicyVersionsModal
        open
        policy={{ ...enabledPolicy }}
        enrichers={enrichers}
        tables={tables}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    )

    await waitFor(() => expect(mockedApi.getEnrichmentPolicyVersion).toHaveBeenCalledTimes(1))
  })
  it("descarta a resposta de uma política que não está mais aberta", async () => {
    // O modal NUNCA desmonta: a página o renderiza sempre, com `open`. Um GET
    // atrasado da política A chegava e sobrescrevia o editor da política B.
    // Publicar dali gravaria as regras de A como versão de B, e publicar
    // SUBSTITUI a lista inteira.
    mockedApi.listEnrichmentPolicyVersions.mockResolvedValue(versions)
    let resolveA: (v: unknown) => void = () => {}
    mockedApi.getEnrichmentPolicyVersion.mockImplementationOnce(
      () => new Promise((r) => { resolveA = r }) as never,
    )

    const { rerender } = render(
      <PolicyVersionsModal
        open
        policy={enabledPolicy}
        enrichers={enrichers}
        tables={tables}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    )

    // Troca para uma política SEM versão antes de A responder.
    rerender(
      <PolicyVersionsModal
        open
        policy={disabledNoVersionPolicy}
        enrichers={enrichers}
        tables={tables}
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    )
    await screen.findByTestId("rules-empty")

    resolveA({ id: "v2", version_number: 77, rules: v2Rules as never })

    await waitFor(() => {
      expect(screen.getByTestId("rules-empty")).toBeInTheDocument()
    })
    expect(screen.queryByTestId("editing-from-version")).not.toBeInTheDocument()
  })

  it("não deixa as regras da política anterior na tela quando a carga falha", async () => {
    mockedApi.listEnrichmentPolicyVersions.mockResolvedValue(versions)
    mockedApi.getEnrichmentPolicyVersion.mockRejectedValue(new Error("500 boom"))

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

    expect(await screen.findByText(/500 boom/)).toBeInTheDocument()
    // Sem regras na tela: publicar aqui substituiria a política pelo que sobrou.
    expect(screen.getByTestId("rules-empty")).toBeInTheDocument()
  })
})
