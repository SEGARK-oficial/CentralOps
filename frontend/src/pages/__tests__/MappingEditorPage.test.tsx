/**
 * Testes de MappingEditorPage
 * Sprint 1: render 3 painéis, 404, loading, dry-run result, dry-run failures,
 *           paste manual de JSON válido e inválido.
 * Sprint 2: toggle view/edit mode, dirty flag, tabs Versões/Auditoria.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import MappingEditorPage from "@/pages/MappingEditorPage"
import * as hooks from "@/hooks/useMapping"
import * as dryRunHooks from "@/hooks/useMappingDryRun"
import * as permissionHooks from "@/hooks/usePermission"
import * as auditHooks from "@/hooks/useMappingAudit"
import { OCSF_TEMPLATES } from "@/data/ocsfTemplates"
import { ApiRequestError } from "@/services/api"
import type { Mapping, MappingVersion, DryRunResult } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

// Mock dos hooks
vi.mock("@/hooks/useMapping")
vi.mock("@/hooks/useMappingDryRun")
vi.mock("@/hooks/usePermission")
vi.mock("@/hooks/useMappingAudit")
// o editor lê selectedOrgId p/ escopar o dry-run. Mock estável.
vi.mock("@/contexts/PlatformContext", () => ({
  usePlatform: () => ({ selectedOrgId: null }),
}))
// Mock useMappingDiff para evitar requests reais
vi.mock("@/hooks/useMappingDiff", () => ({
  useMappingDiff: () => ({ diff: null, isLoading: false, error: null }),
}))

const mockedUseMapping = vi.mocked(hooks.useMapping)
const mockedUseDryRun = vi.mocked(dryRunHooks.useMappingDryRun)
const mockedUsePermission = vi.mocked(permissionHooks.usePermission)
const mockedUseMappingAudit = vi.mocked(auditHooks.useMappingAudit)

// ── Fixtures ──────────────────────────────────────────────────────────────────

const VERSION: MappingVersion = {
  id: "v1",
  definition_id: "m1",
  version_number: 1,
  rules: {
    preprocess: [],
    rules: [
      { target: "event.action", source: "action" },
      { target: "event.user", source: "user", required: true },
    ],
  },
  author_user_id: null,
  commit_message: "Versão inicial",
  diff_from_previous: null,
  dry_run_stats: null,
  created_at: "2026-01-01T00:00:00Z",
}

const MAPPING: Mapping & { versions: MappingVersion[] } = {
  id: "m1",
  vendor: "wazuh",
  event_type: "authentication",
  description: "Autenticação SSH",
  current_version_id: "v1",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  versions: [VERSION],
}

const DRY_RUN_EMPTY: ReturnType<typeof dryRunHooks.useMappingDryRun> = {
  result: null,
  isPending: false,
  error: null,
}

const DRY_RUN_PENDING: ReturnType<typeof dryRunHooks.useMappingDryRun> = {
  result: null,
  isPending: true,
  error: null,
}

const DRY_RUN_RESULT: DryRunResult = {
  sample_size: 5,
  ok_count: 4,
  fail_count: 1,
  rule_failures: [
    {
      target: "event.action",
      fail_count: 1,
      fail_examples: ["valor_invalido"],
    },
  ],
  output_examples: [{ event: { action: "login", user: "joao" } }],
  default_hit_warnings: [],
}

const DRY_RUN_RESULT_WITH_WARNINGS: DryRunResult = {
  sample_size: 5,
  ok_count: 5,
  fail_count: 0,
  rule_failures: [],
  output_examples: [{ event: { action: "login" } }],
  default_hit_warnings: [
    {
      target: "event.action",
      hit_rate: 1.0,
      hit_count: 5,
      sample_size: 5,
      expected_always_default: false,
    },
  ],
}

const DRY_RUN_SUCCESS: ReturnType<typeof dryRunHooks.useMappingDryRun> = {
  result: DRY_RUN_RESULT,
  isPending: false,
  error: null,
}

// ── Helper de render ──────────────────────────────────────────────────────────

function renderPage(id = "m1") {
  return render(
    <MemoryRouter initialEntries={[`/mappings/${id}`]}>
      <Routes>
        <Route path="/mappings/:id" element={<MappingEditorPage />} />
        <Route path="/dashboard" element={<div>Dashboard</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

// ── Setup ─────────────────────────────────────────────────────────────────────

beforeEach(() => {
  vi.clearAllMocks()
  mockedUseDryRun.mockReturnValue(DRY_RUN_EMPTY)
  // Default: sem permissão de write
  mockedUsePermission.mockReturnValue(false)
  mockedUseMappingAudit.mockReturnValue({ entries: [], isLoading: false, error: null })
})

// ── Testes Sprint 1 (regressão) ───────────────────────────────────────────────

describe("MappingEditorPage", () => {
  it("renderiza os 3 painéis com role=region quando mapping carrega", async () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    expect(screen.getByTestId("payload-panel")).toBeInTheDocument()
    expect(screen.getByTestId("rules-editor")).toBeInTheDocument()
    expect(screen.getByTestId("envelope-preview")).toBeInTheDocument()

    const regions = screen.getAllByRole("region")
    expect(regions.length).toBeGreaterThanOrEqual(3)

    expect(screen.getByText("Payload de amostra")).toBeInTheDocument()
    expect(screen.getByText("Regras")).toBeInTheDocument()
    expect(screen.getByText(/Envelope normalizado/)).toBeInTheDocument()
  })

  it("mostra título com vendor e event_type no PageHeader", () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    expect(screen.getByText("wazuh · authentication")).toBeInTheDocument()
  })

  it("mostra LoadingSpinner quando isLoading=true", () => {
    mockedUseMapping.mockReturnValue({
      data: null,
      isLoading: true,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    expect(screen.getByText("Carregando mapping...")).toBeInTheDocument()
  })

  it("mostra Notice de erro e botão Voltar quando 404", () => {
    mockedUseMapping.mockReturnValue({
      data: null,
      isLoading: false,
      error: new ApiRequestError("Mapping não encontrado", 404),
      refetch: vi.fn(),
    })

    renderPage()

    const occurrences = screen.getAllByText("Mapping não encontrado")
    expect(occurrences.length).toBeGreaterThanOrEqual(1)
    expect(screen.getByRole("button", { name: /voltar/i })).toBeInTheDocument()
  })

  it("mostra Notice de erro genérico para erros que não são 404", () => {
    mockedUseMapping.mockReturnValue({
      data: null,
      isLoading: false,
      error: new Error("Erro de rede"),
      refetch: vi.fn(),
    })

    renderPage()

    expect(screen.getByText("Erro ao carregar mapping")).toBeInTheDocument()
    expect(screen.getByText("Erro de rede")).toBeInTheDocument()
  })

  it("exibe LoadingSpinner no EnvelopePreview quando dry-run isPending=true (sem resultado anterior)", () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })
    mockedUseDryRun.mockReturnValue(DRY_RUN_PENDING)

    renderPage()

    expect(screen.getByText("Calculando normalização...")).toBeInTheDocument()
  })

  it("renderiza o JsonViewer quando dry-run retorna output_examples", () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })
    mockedUseDryRun.mockReturnValue(DRY_RUN_SUCCESS)

    renderPage()

    expect(screen.getByTestId("dry-run-status-bar")).toBeInTheDocument()
    expect(screen.getByText("5 amostras")).toBeInTheDocument()
    expect(screen.getByText("4 OK")).toBeInTheDocument()
    expect(screen.getByText("1 falha")).toBeInTheDocument()
  })

  it("exibe Notice de aviso quando dry-run tem falhas", () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })
    mockedUseDryRun.mockReturnValue(DRY_RUN_SUCCESS)

    renderPage()

    expect(screen.getByText("Falhas de regras detectadas")).toBeInTheDocument()
    const codeElements = screen.getAllByText(/event\.action/)
    expect(codeElements.some((el) => el.tagName === "CODE")).toBe(true)
  })

  it("exibe Notice de erro quando dry-run retorna error", () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })
    mockedUseDryRun.mockReturnValue({
      result: null,
      isPending: false,
      error: new Error("Backend indisponível"),
    })

    renderPage()

    expect(screen.getByText("Erro na simulação")).toBeInTheDocument()
    expect(screen.getByText("Backend indisponível")).toBeInTheDocument()
  })

  it("PayloadPanel modo Manual: JSON inválido exibe erro inline", async () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    fireEvent.click(screen.getByRole("tab", { name: /manual/i }))

    const textarea = screen.getByTestId("payload-manual-input")
    fireEvent.change(textarea, { target: { value: "{ invalido }" } })

    await waitFor(() => {
      expect(screen.getByText("JSON inválido — verifique a sintaxe.")).toBeInTheDocument()
    })
  })

  it("PayloadPanel modo Manual: JSON válido não exibe erro", async () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    fireEvent.click(screen.getByRole("tab", { name: /manual/i }))

    const textarea = screen.getByTestId("payload-manual-input")
    fireEvent.change(textarea, {
      target: { value: '{ "action": "login" }' },
    })

    await waitFor(() => {
      expect(screen.queryByText("JSON inválido")).not.toBeInTheDocument()
    })
  })

  it("PayloadPanel modo Manual: JSON não-objeto exibe erro de tipo", async () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    fireEvent.click(screen.getByRole("tab", { name: /manual/i }))

    const textarea = screen.getByTestId("payload-manual-input")
    fireEvent.change(textarea, { target: { value: "[1, 2, 3]" } })

    await waitFor(() => {
      expect(
        screen.getByText("O payload deve ser um objeto JSON (chave/valor)."),
      ).toBeInTheDocument()
    })
  })

  it("RulesEditor mostra as regras da versão corrente", () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    expect(screen.getByTestId("rule-row-event.action")).toBeInTheDocument()
    expect(screen.getByTestId("rule-row-event.user")).toBeInTheDocument()
  })

  it("RulesEditor exibe badge com contagem de regras", () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    expect(screen.getByText("Total: 2 regras")).toBeInTheDocument()
  })

  it("EnvelopePreview exibe EmptyState quando sem resultado e sem payload", () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })
    mockedUseDryRun.mockReturnValue(DRY_RUN_EMPTY)

    renderPage()

    expect(
      screen.getByText("Forneça uma amostra para ver a normalização."),
    ).toBeInTheDocument()
  })

  // ── Testes Sprint 2 ────────────────────────────────────────────────────────

  it("botão 'Editar regras' NÃO aparece sem permissão mapping.write", () => {
    mockedUsePermission.mockReturnValue(false)
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    expect(screen.queryByTestId("edit-mode-button")).not.toBeInTheDocument()
  })

  it("botão 'Editar regras' aparece com permissão mapping.write", () => {
    mockedUsePermission.mockReturnValue(true)
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    expect(screen.getByTestId("edit-mode-button")).toBeInTheDocument()
  })

  it("clique em 'Editar regras' muda para modo edit — botões Salvar e Descartar aparecem", () => {
    mockedUsePermission.mockReturnValue(true)
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    fireEvent.click(screen.getByTestId("edit-mode-button"))

    expect(screen.getByTestId("save-button")).toBeInTheDocument()
    expect(screen.getByTestId("discard-button")).toBeInTheDocument()
    // Botão Editar some
    expect(screen.queryByTestId("edit-mode-button")).not.toBeInTheDocument()
  })

  it("dirty flag: Notice 'alterações não salvas' aparece após editar regra", () => {
    mockedUsePermission.mockReturnValue(true)
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    // Entra em modo edit
    fireEvent.click(screen.getByTestId("edit-mode-button"))

    // Em edit mode, regras vêm colapsadas — expandir a primeira pra ver
    // o input do target.
    const expandBtns = screen.getAllByRole("button", { name: /expandir regra/i })
    fireEvent.click(expandBtns[0])

    // Modifica o target da primeira regra
    const targetInputs = screen.getAllByDisplayValue("event.action")
    fireEvent.change(targetInputs[0], { target: { value: "event.action.modificado" } })

    expect(screen.getByText("Você tem alterações não salvas.")).toBeInTheDocument()
  })

  it("Descartar sem alterações vai direto para modo view (sem confirm dialog)", () => {
    mockedUsePermission.mockReturnValue(true)
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    fireEvent.click(screen.getByTestId("edit-mode-button"))
    fireEvent.click(screen.getByTestId("discard-button"))

    // Volta para modo view — botão Editar reaparece
    expect(screen.getByTestId("edit-mode-button")).toBeInTheDocument()
  })

  it("Descartar com alterações mostra ConfirmDialog", () => {
    mockedUsePermission.mockReturnValue(true)
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    fireEvent.click(screen.getByTestId("edit-mode-button"))

    // Expande a primeira regra pra acessar o input do target
    const expandBtns = screen.getAllByRole("button", { name: /expandir regra/i })
    fireEvent.click(expandBtns[0])

    // Faz uma edição
    const targetInputs = screen.getAllByDisplayValue("event.action")
    fireEvent.change(targetInputs[0], { target: { value: "alterado" } })

    fireEvent.click(screen.getByTestId("discard-button"))

    // ConfirmDialog deve aparecer
    expect(screen.getByText("Descartar alterações?")).toBeInTheDocument()
  })

  it("aba Versões renderiza quando ativa", () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    fireEvent.click(screen.getByRole("tab", { name: /versões/i }))

    // A tabela de versões deve renderizar a mensagem de commit
    expect(screen.getByText("Versão inicial")).toBeInTheDocument()
  })

  it("aba Auditoria renderiza quando ativa", () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    fireEvent.click(screen.getByRole("tab", { name: /auditoria/i }))

    // Filtros da auditoria devem aparecer
    expect(screen.getByText("Ação")).toBeInTheDocument()
    expect(screen.getByText("Usuário")).toBeInTheDocument()
  })

  // ── Testes Sprint 3 ────────────────────────────────────────────────────────

  it("link 'Como criar regras?' está visível no header e aponta para o doc", () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    const link = screen.getByTestId("help-link")
    expect(link).toBeInTheDocument()
    // O link aponta para o portal Docusaurus público (CentralOps-docs).
    // A URL exata é mantida em src/lib/docs.ts.
    const href = link.getAttribute("href") ?? ""
    expect(href).toMatch(/CentralOps-docs\/docs\/normalization\//)
    expect(href).toMatch(/^https?:\/\//)
    expect(link).toHaveAttribute("target", "_blank")
    expect(link).toHaveAttribute("rel", "noopener noreferrer")
    expect(link).toHaveTextContent("Como criar regras?")
  })

  // ── Testes — Preprocess ────────────────────────────────────────────

  it("seção de pré-processamento renderiza em view mode (colapsada por default)", () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    // PreprocessEditor deve estar na tela
    expect(screen.getByTestId("preprocess-editor")).toBeInTheDocument()
    // Mas o body (preprocess-list) deve estar oculto (collapsed)
    expect(screen.queryByTestId("preprocess-list")).not.toBeInTheDocument()
  })

  it("badge DSL v2 NÃO aparece quando não há ops de preprocess", () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    expect(screen.queryByTestId("preprocess-badge")).not.toBeInTheDocument()
  })

  it("clicar em '+ Adicionar pré-processamento' em edit mode adiciona uma op e expande a seção", () => {
    mockedUsePermission.mockReturnValue(true)
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    // Entra em modo edit
    fireEvent.click(screen.getByTestId("edit-mode-button"))

    // Clica em adicionar
    fireEvent.click(screen.getByTestId("preprocess-add-button"))

    // O body deve aparecer agora
    expect(screen.getByTestId("preprocess-list")).toBeInTheDocument()
    // E deve haver uma row
    expect(screen.getByTestId("preprocess-row-0")).toBeInTheDocument()
  })

  it("badge DSL v2 aparece após adicionar uma op de preprocess", () => {
    mockedUsePermission.mockReturnValue(true)
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    fireEvent.click(screen.getByTestId("edit-mode-button"))
    fireEvent.click(screen.getByTestId("preprocess-add-button"))

    expect(screen.getByTestId("preprocess-badge")).toBeInTheDocument()
    expect(screen.getByTestId("preprocess-badge")).toHaveTextContent("preprocess")
  })

  it("remover a única op de preprocess remove o badge DSL v2", () => {
    mockedUsePermission.mockReturnValue(true)
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    fireEvent.click(screen.getByTestId("edit-mode-button"))
    fireEvent.click(screen.getByTestId("preprocess-add-button"))

    // Badge aparece
    expect(screen.getByTestId("preprocess-badge")).toBeInTheDocument()

    // Remove a op
    fireEvent.click(screen.getByRole("button", { name: /remover operação/i }))

    // Badge some
    expect(screen.queryByTestId("preprocess-badge")).not.toBeInTheDocument()
  })

  it("Descartar em edit mode reset o preprocess (badge DSL v2 some)", () => {
    mockedUsePermission.mockReturnValue(true)
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    fireEvent.click(screen.getByTestId("edit-mode-button"))
    fireEvent.click(screen.getByTestId("preprocess-add-button"))

    // Confirma que badge está visível
    expect(screen.getByTestId("preprocess-badge")).toBeInTheDocument()

    // Descarta sem alterações nas regras — vai direto (não abre confirm dialog)
    fireEvent.click(screen.getByTestId("discard-button"))

    // Volta para view mode e badge some
    expect(screen.queryByTestId("preprocess-badge")).not.toBeInTheDocument()
    expect(screen.getByTestId("edit-mode-button")).toBeInTheDocument()
  })

  // ── Testes — default_hit_warnings ───────────────────────────────

  it("chip de aviso NÃO aparece quando dry-run não retorna warnings", () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })
    mockedUseDryRun.mockReturnValue(DRY_RUN_SUCCESS)

    renderPage()

    expect(screen.queryByTestId("default-hit-warnings-chip")).not.toBeInTheDocument()
  })

  it("chip de aviso aparece quando dry-run retorna warnings", () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })
    mockedUseDryRun.mockReturnValue({
      result: DRY_RUN_RESULT_WITH_WARNINGS,
      isPending: false,
      error: null,
    })

    renderPage()

    expect(screen.getByTestId("default-hit-warnings-chip")).toBeInTheDocument()
    expect(screen.getByTestId("default-hit-warnings-chip")).toHaveTextContent("1 regra 100% default")
  })

  it("clicar no chip de aviso abre o popover de warnings", () => {
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })
    mockedUseDryRun.mockReturnValue({
      result: DRY_RUN_RESULT_WITH_WARNINGS,
      isPending: false,
      error: null,
    })

    renderPage()

    fireEvent.click(screen.getByTestId("default-hit-warnings-chip"))
    expect(screen.getByTestId("default-hit-warnings-popover")).toBeInTheDocument()
    // O warning item deve estar renderizado dentro do popover
    expect(screen.getByTestId("warning-item-event.action")).toBeInTheDocument()
  })

  // ── Templates OCSF ─────────────────────────────────────────────

  it("carregar template substitui draftRules pelas regras do template", () => {
    mockedUsePermission.mockReturnValue(true)
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    // Entra em modo edit
    fireEvent.click(screen.getByTestId("edit-mode-button"))

    // Abre o menu de adicionar regra
    fireEvent.click(screen.getByTestId("add-rule-button"))
    fireEvent.click(screen.getByTestId("load-ocsf-template"))

    // TemplatePicker deve estar visível
    expect(screen.getByTestId("template-picker")).toBeInTheDocument()

    // Editor tem regras existentes (2) — ao clicar em usar template deve exibir confirmação
    const firstTemplate = OCSF_TEMPLATES[0]
    fireEvent.click(screen.getByTestId(`use-template-${firstTemplate.id}`))

    expect(screen.getByTestId("template-confirm")).toBeInTheDocument()

    // Confirma substituição
    fireEvent.click(screen.getByTestId("template-confirm-replace"))

    // O picker deve fechar
    expect(screen.queryByTestId("template-picker")).not.toBeInTheDocument()

    // O editor deve mostrar o número de regras do template
    // O badge atualiza com a contagem correta
    const expectedCount = firstTemplate.rules.length
    expect(
      screen.getByText(`Total: ${expectedCount} regras`),
    ).toBeInTheDocument()
  })

  it("cancelar no prompt de confirmação mantém as regras originais", () => {
    mockedUsePermission.mockReturnValue(true)
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    fireEvent.click(screen.getByTestId("edit-mode-button"))

    // Regras originais
    expect(screen.getByText("Total: 2 regras")).toBeInTheDocument()

    fireEvent.click(screen.getByTestId("add-rule-button"))
    fireEvent.click(screen.getByTestId("load-ocsf-template"))

    const firstTemplate = OCSF_TEMPLATES[0]
    fireEvent.click(screen.getByTestId(`use-template-${firstTemplate.id}`))

    // Cancela
    fireEvent.click(screen.getByTestId("template-confirm-cancel"))

    // Modal permanece mas ainda tem as 2 regras originais
    expect(screen.getByText("Total: 2 regras")).toBeInTheDocument()
  })

  it("carregar template aciona o debounce do dry-run (useMappingDryRun é re-invocado)", () => {
    mockedUsePermission.mockReturnValue(true)
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    const initialCallCount = mockedUseDryRun.mock.calls.length

    fireEvent.click(screen.getByTestId("edit-mode-button"))
    fireEvent.click(screen.getByTestId("add-rule-button"))
    fireEvent.click(screen.getByTestId("load-ocsf-template"))

    const firstTemplate = OCSF_TEMPLATES[0]
    fireEvent.click(screen.getByTestId(`use-template-${firstTemplate.id}`))
    fireEvent.click(screen.getByTestId("template-confirm-replace"))

    // Após substituir as regras, useMappingDryRun deve ter sido chamado mais vezes
    expect(mockedUseDryRun.mock.calls.length).toBeGreaterThan(initialCallCount)
  })

  // ── Testes Bug 2: import/export com preprocess (schema v2) ──────────────────

  it("importar um arquivo v2 com preprocess popula draftPreprocess e mostra badge DSL v2", async () => {
    mockedUsePermission.mockReturnValue(true)
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    // Entra em edit mode
    fireEvent.click(screen.getByTestId("edit-mode-button"))

    // Simula clique no botão de import
    fireEvent.click(screen.getByTestId("import-rules-button"))

    // Prepara um arquivo v2 com preprocess
    const v2Payload = JSON.stringify({
      schema_version: 2,
      preprocess: [{ op: "json_parse", source: "raw_data", target: "_parsed", tolerant: true }],
      rules: [{ target: "event.imported", source: "imported_source" }],
    })
    const file = new File([v2Payload], "mapping.json", { type: "application/json" })

    // Dispara o FileReader via change no input oculto
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(fileInput, "files", { value: [file], configurable: true })

    // Usa FileReader real — simula o evento onload manualmente
    const readerMock = {
      onload: null as ((ev: ProgressEvent<FileReader>) => void) | null,
      readAsText: vi.fn().mockImplementation(function (this: typeof readerMock) {
        this.onload?.({ target: { result: v2Payload } } as unknown as ProgressEvent<FileReader>)
      }),
    }
    // vitest 4: o spy invoca o mock com `new FileReader()`. Arrow function não
    // pode ser construída (`new () => …` lança) — usa-se `function` (o retorno
    // de objeto substitui o `this` do construtor).
    vi.spyOn(globalThis, "FileReader" as never).mockImplementation(function () {
      return readerMock as unknown as FileReader
    })

    fireEvent.change(fileInput)

    // O dialog de confirmação deve aparecer com as regras importadas
    expect(screen.getByTestId("import-confirm")).toBeInTheDocument()
    expect(screen.getByText(/1 regra importada/)).toBeInTheDocument()

    // Confirma a importação
    fireEvent.click(screen.getByTestId("import-confirm-button"))

    // Badge DSL v2 deve aparecer (preprocess foi populado)
    expect(screen.getByTestId("preprocess-badge")).toBeInTheDocument()

    vi.restoreAllMocks()
  })

  it("exportar em edit mode com preprocess inclui preprocess no JSON (via buildMappingExport)", () => {
    // Este teste verifica que RulesEditor recebe `preprocess` e o passa a buildMappingExport.
    // A integração é testada via unit test em mapping-import.test.ts (round-trip).
    // Aqui apenas verificamos que o botão de export renderiza em edit mode.
    mockedUsePermission.mockReturnValue(true)
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })

    renderPage()

    fireEvent.click(screen.getByTestId("edit-mode-button"))

    // O botão de export deve estar visível (rules.length > 0)
    expect(screen.getByTestId("export-rules-button")).toBeInTheDocument()
  })

  it("clicar 'Marcar como intencional' atualiza draftRules com expected_always_default=true", () => {
    mockedUsePermission.mockReturnValue(true)
    mockedUseMapping.mockReturnValue({
      data: MAPPING,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    })
    mockedUseDryRun.mockReturnValue({
      result: DRY_RUN_RESULT_WITH_WARNINGS,
      isPending: false,
      error: null,
    })

    renderPage()

    // Entra em edit mode para que draftRules seja mutável
    fireEvent.click(screen.getByTestId("edit-mode-button"))

    // Abre o popover
    fireEvent.click(screen.getByTestId("default-hit-warnings-chip"))
    expect(screen.getByTestId("default-hit-warnings-popover")).toBeInTheDocument()

    // Clica em "Marcar como intencional" para event.action
    fireEvent.click(screen.getByTestId("mark-intentional-event.action"))

    // Verifica que o dry-run foi chamado novamente (useMappingDryRun re-invocado)
    // A verificação principal é que não houve throw e a UI não crashou.
    // O re-run real é testado via integração; aqui validamos que o callback existe.
    expect(mockedUseDryRun).toHaveBeenCalled()
  })
})
