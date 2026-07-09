/**
 * MappingEditorPage
 * Página /mappings/:id — editor de mapping com edição, save/diff/rollback e auditoria.
 */

import { useState, useCallback, useMemo, useRef, useEffect } from "react"
import type React from "react"
import { useParams, useNavigate, useSearchParams } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { ArrowLeftIcon, GitBranchIcon, HistoryIcon, ClipboardListIcon, PencilIcon, BookOpenIcon } from "lucide-react"
import { useMapping } from "@/hooks/useMapping"
import { useMappingDryRun } from "@/hooks/useMappingDryRun"
import { usePlatform } from "@/contexts/PlatformContext"
import { useDiscoveredFields } from "@/hooks/useDiscoveredFields"
import { usePermission } from "@/hooks/usePermission"
import { ApiRequestError } from "@/services/api"
import { DOCS } from "@/lib/docs"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { Button } from "@/components/ui/Button/Button"
import { Badge } from "@/components/ui/Badge/Badge"
import { Notice } from "@/components/ui/Notice/Notice"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Tabs, TabsList, TabsTrigger, TabsPanel } from "@/components/ui/Tabs/Tabs"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { MappingEditorLayout } from "@/components/mappings/MappingEditorLayout"
import { PayloadPanel } from "@/components/mappings/PayloadPanel"
import { RulesEditor } from "@/components/mappings/RulesEditor"
import { PreprocessEditor } from "@/components/mappings/PreprocessEditor"
import { EnvelopePreview } from "@/components/mappings/EnvelopePreview"
import { SaveModal } from "@/components/mappings/SaveModal"
import { MappingVersionsTable } from "@/components/mappings/MappingVersionsTable"
import { MappingAuditTable } from "@/components/mappings/MappingAuditTable"
import type { MappingPayload, MappingRule, MappingVersion, PreprocessOp } from "@/types"

// ── Helpers ───────────────────────────────────────────────────────────────────

function resolveCurrentVersion(
  versions: MappingVersion[],
  currentVersionId: string | null,
): MappingVersion | null {
  if (!currentVersionId) return versions[0] ?? null
  return versions.find((v) => v.id === currentVersionId) ?? versions[0] ?? null
}

function deepEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

// ── Componente ────────────────────────────────────────────────────────────────

type EditorMode = "view" | "edit"
type ActiveTab = "editor" | "versions" | "audit"

export const MappingEditorPage: React.FC = () => {
  const { t } = useTranslation("mappings")
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  // o reservoir é por-org. Admin global nomeia o tenant cujas
  // amostras o dry-run deve usar (o filtro global de org). Sem isto, o dry-run
  // do admin (org=None) lê reservoir vazio e o painel não mostra o envelope.
  const { selectedOrgId } = usePlatform()

  const { data: mapping, isLoading, error: mappingError, refetch } = useMapping(id ?? "")

  const canWrite = usePermission("mapping.write")

  // ── prefill_path ─────────────────────────────────────────────────────────────
  // Lido do query param ?prefill_path= gerado pelo DriftTable quando o usuário
  // clica em "criar regra para este campo". Ao ser consumido, o param é removido
  // da URL para evitar re-disparo em re-renders.
  const prefillPath = searchParams.get("prefill_path")

  // Tabs
  const [activeTab, setActiveTab] = useState<ActiveTab>("editor")

  // Editor mode
  const [editorMode, setEditorMode] = useState<EditorMode>("view")
  const [draftRules, setDraftRules] = useState<MappingRule[] | null>(null)
  const [showDiscardConfirm, setShowDiscardConfirm] = useState(false)
  const [showSaveModal, setShowSaveModal] = useState(false)

  // ── Preprocess state ─────────────────────────────────────────────────────────
  // Armazena a lista de ops de pré-processamento do draft.
  // Separado de draftRules para manter a mesma estrutura pattern que o backend.
  const [draftPreprocess, setDraftPreprocess] = useState<PreprocessOp[]>([])
  const [preprocessExpanded, setPreprocessExpanded] = useState(false)

  // Raw event for dry-run
  const [rawEvent, setRawEvent] = useState<Record<string, unknown> | null>(null)
  const handleRawEventChange = useCallback(
    (event: Record<string, unknown> | null) => setRawEvent(event),
    [],
  )

  // Resolve versão corrente
  const currentVersion = mapping
    ? resolveCurrentVersion(mapping.versions, mapping.current_version_id)
    : null

  // currentVersion.rules vem do backend no shape v2 (dict).
  const currentRules: MappingRule[] = currentVersion?.rules?.rules ?? []
  const currentPreprocess: PreprocessOp[] = currentVersion?.rules?.preprocess ?? []

  // Auto-expand a seção de preprocess em view mode quando a versão atual
  // tem ops persistidas — caso contrário o usuário acha que "preprocess não
  // foi salvo" porque a seção colapsada esconde os ops.
  useEffect(() => {
    if (editorMode === "view" && currentPreprocess.length > 0) {
      setPreprocessExpanded(true)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editorMode, currentVersion?.id, currentPreprocess.length])

  // draft: usa draftRules quando em edit mode, senão currentRules
  const activeRules = editorMode === "edit" && draftRules !== null ? draftRules : currentRules

  const isDirty = useMemo(
    () => editorMode === "edit" && draftRules !== null && !deepEqual(draftRules, currentRules),
    [editorMode, draftRules, currentRules],
  )

  // ── Proteção contra perda de alterações ───────────────────────────────────
  // beforeunload cobre fechar a aba, recarregar a página e navegação para fora
  // do SPA (links externos / barra de endereço). O browser exibe seu próprio
  // diálogo nativo de confirmação quando preventDefault é chamado.
  //
  // Observação: a interceptação de navegação *interna* do SPA (react-router)
  // via useBlocker NÃO está disponível neste app porque o roteador é montado
  // com <BrowserRouter> (App.tsx), e useBlocker exige um data router
  // (createBrowserRouter + RouterProvider). Migrar o roteador foge ao escopo
  // desta página. Enquanto isso, o usuário ainda é
  // protegido pelo Notice de "alterações não salvas" e pelo fluxo de Descartar.
  useEffect(() => {
    if (!isDirty) return
    function handleBeforeUnload(e: BeforeUnloadEvent) {
      e.preventDefault()
      // Compat: alguns browsers legados exigem returnValue setado.
      e.returnValue = ""
    }
    window.addEventListener("beforeunload", handleBeforeUnload)
    return () => window.removeEventListener("beforeunload", handleBeforeUnload)
  }, [isDirty])

  // savePayload: payload v2 (dict) enviado ao backend. Memoizado para
  // não trocar de identidade enquanto o usuário digita na textarea de
  // commit do SaveModal — evita cascata de re-renders no MappingDiffModal
  // aninhado e a perda de foco resultante.
  const savePayload: MappingPayload = useMemo(
    () => ({ preprocess: draftPreprocess, rules: draftRules ?? currentRules }),
    [draftPreprocess, draftRules, currentRules],
  )

  // ── Debounce de regras para o dry-run ─────────────────────────────────────
  //
  // `draftRules` é atualizado imediatamente a cada keystroke para que a UI
  // (inputs do RulesEditor) reflita o estado sem delay. Porém, passar
  // `activeRules` diretamente ao useMappingDryRun causaria um dry-run por
  // keystroke — o array muda a cada render por referência, sobrepondo o
  // debounce interno do hook.
  //
  // Solução: mantemos `effectiveRules` (estado separado) que só é
  // sincronizado com `activeRules` após DRY_RUN_DEBOUNCE_MS de inatividade.
  // O dry-run consome `effectiveRules` em vez de `activeRules`.
  //
  // RulesEditor.onChange recebe `handleRulesChange` via useCallback estável
  // → não re-monta os inputs de cada RuleRow a cada render do pai.
  const DRY_RUN_DEBOUNCE_MS = 400
  const [effectiveRules, setEffectiveRules] = useState<MappingRule[]>(activeRules)
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Sincroniza effectiveRules quando activeRules muda (ex: ao entrar em edit
  // mode, ao sair, ou ao resetar o draft). Em view mode ou quando draftRules
  // é null, a mudança é pontual (não veio de keystroke) — aplica sem delay.
  useEffect(() => {
    if (editorMode !== "edit" || draftRules === null) {
      // Mudança não originada por keystroke — aplica imediatamente
      if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current)
      setEffectiveRules(activeRules)
    }
    // Caso edit mode com draftRules: a atualização debounced é feita em
    // handleRulesChange para garantir que cada keystroke cancele o timer
    // anterior. Não fazer nada aqui nesse branch para evitar dobrar timers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editorMode, draftRules === null])

  // Callback estável para RulesEditor: atualiza draftRules imediatamente (UI
  // responsiva) e agenda atualização de effectiveRules com debounce (dry-run).
  const handleRulesChange = useCallback(
    (rules: MappingRule[]) => {
      setDraftRules(rules)
      if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current)
      debounceTimerRef.current = setTimeout(() => {
        setEffectiveRules(rules)
      }, DRY_RUN_DEBOUNCE_MS)
    },
    [],
  )

  // Limpar timer pendente ao desmontar
  useEffect(() => {
    return () => {
      if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current)
    }
  }, [])

  // ── Consume prefill_path ──────────────────────────────────────────────────
  // Entra em edit mode, adiciona rule com source=prefill_path e remove o param.
  // Executa apenas quando: mapping carregado + escrita permitida + view mode.
  // O efeito roda uma vez (prefillPath muda de valor para null após consumo).
  const prefillConsumedRef = useRef(false)
  useEffect(() => {
    if (
      !prefillPath ||
      prefillConsumedRef.current ||
      !mapping ||
      !canWrite ||
      editorMode !== "view"
    ) return

    prefillConsumedRef.current = true

    // Remove o param da URL (replace para não poluir o histórico)
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete("prefill_path")
      return next
    }, { replace: true })

    // Entra em edit mode com a rule pré-preenchida
    const rules = [...currentRules]
    const prefilled = { target: "", source: prefillPath }
    const nextRules = [...rules, prefilled]
    setDraftRules(nextRules)
    setEffectiveRules(nextRules)
    setEditorMode("edit")
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefillPath, mapping, canWrite, editorMode])

  // Toast/banner para informar o usuário sobre o prefill
  const [prefillBanner, setPrefillBanner] = useState<string | null>(null)
  useEffect(() => {
    if (prefillPath && mapping && canWrite) {
      setPrefillBanner(prefillPath)
    }
  }, [prefillPath, mapping, canWrite])

  function dismissPrefillBanner() {
    setPrefillBanner(null)
  }

  // dry-run — usa effectiveRules (debounced) em vez de activeRules (imediato).
  // rawEvents é MEMOIZADO: sem isto, `[rawEvent]` era um array novo a cada render,
  // mudando a dependência do useEffect do useMappingDryRun e disparando um dry-run
  // por render (o resultado atualiza estado → re-render → novo array → novo dry-run).
  const rawEvents = useMemo(() => (rawEvent !== null ? [rawEvent] : null), [rawEvent])
  const {
    result: dryRunResult,
    isPending: dryRunPending,
    error: dryRunError,
  } = useMappingDryRun(effectiveRules, rawEvents, {
    vendor: mapping?.vendor,
    eventType: mapping?.event_type,
    organizationId: selectedOrgId,
    // debounceMs=0 aqui pois o debounce já foi aplicado no effectiveRules
    debounceMs: 0,
    limit: 1,
    preprocess: draftPreprocess,
  })

  // JMESPath suggestions para autocomplete no editor de regras
  const { fields: jmespathSuggestions } = useDiscoveredFields(id)

  // dry-run fail ratio para SaveModal warning
  const dryRunFailRatio =
    dryRunResult && dryRunResult.sample_size > 0
      ? dryRunResult.fail_count / dryRunResult.sample_size
      : undefined

  // ── Handlers de modo ────────────────────────────────────────────────────────

  function handleEnterEditMode() {
    const rules = [...currentRules]
    setDraftRules(rules)
    setEffectiveRules(rules)
    // Seed o draft de preprocess com o que já está persistido na versão atual,
    // para não perder ops ao salvar uma nova versão.
    const seedPreprocess = [...currentPreprocess]
    setDraftPreprocess(seedPreprocess)
    setPreprocessExpanded(seedPreprocess.length > 0)
    setEditorMode("edit")
  }

  function handleDiscard() {
    if (isDirty) {
      setShowDiscardConfirm(true)
    } else {
      if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current)
      setEditorMode("view")
      setDraftRules(null)
      setEffectiveRules(currentRules)
      setDraftPreprocess([])
      setPreprocessExpanded(false)
    }
  }

  function handleDiscardConfirm() {
    if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current)
    setShowDiscardConfirm(false)
    setEditorMode("view")
    setDraftRules(null)
    setEffectiveRules(currentRules)
    setDraftPreprocess([])
    setPreprocessExpanded(false)
  }

  function handleSaveSuccess() {
    if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current)
    setShowSaveModal(false)
    setEditorMode("view")
    setDraftRules(null)
    setDraftPreprocess([])
    setPreprocessExpanded(false)
    refetch()
  }

  // ── Mark intentional handler ─────────────────────────────────────
  // Flipa expected_always_default=true na regra com o target correspondente.
  // O dry-run re-executa automaticamente via debounce em effectiveRules.
  const handleMarkIntentional = useCallback(
    (target: string) => {
      const rules = draftRules ?? currentRules
      const updated = rules.map((r) => {
        if (r.target !== target || r.kind === "array_builder") return r
        return { ...r, expected_always_default: true }
      })
      handleRulesChange(updated)
    },
    [draftRules, currentRules, handleRulesChange],
  )

  // ── Preprocess handlers ──────────────────────────────────────────────────────

  const handlePreprocessChange = useCallback((ops: PreprocessOp[]) => {
    setDraftPreprocess(ops)
    // Auto-expandir quando o usuário adiciona a primeira op
    if (ops.length > 0) {
      setPreprocessExpanded(true)
    }
  }, [])

  function handlePreprocessToggle() {
    setPreprocessExpanded((prev) => !prev)
  }

  // ── Loading / error states ───────────────────────────────────────────────

  if (isLoading) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center">
        <LoadingSpinner size="lg" text={t("editor.loading")} />
      </div>
    )
  }

  const is404 = mappingError instanceof ApiRequestError && mappingError.statusCode === 404

  if (mappingError) {
    return (
      <div className="flex flex-col gap-4 max-w-xl mx-auto mt-12 px-4">
        <Notice
          variant="danger"
          title={is404 ? t("editor.notFound") : t("editor.loadError")}
          action={
            <Button
              variant="ghost"
              size="sm"
              onClick={() => navigate(-1)}
              leftIcon={<ArrowLeftIcon size={14} />}
            >
              {t("common:actions.back")}
            </Button>
          }
        >
          {mappingError.message}
        </Notice>
      </div>
    )
  }

  if (!mapping) return null

  // ── Layout principal ────────────────────────────────────────────────────────

  return (
    <div data-testid="mapping-editor-page" className="flex flex-col gap-6 px-1">
      <PageHeader
        eyebrow={t("editor.eyebrow")}
        title={`${mapping.vendor} · ${mapping.event_type}`}
        description={mapping.description ?? undefined}
        icon={<GitBranchIcon size={20} />}
        actions={
          <div className="flex items-center gap-2">
            {currentVersion && (
              <Badge variant="primary">
                v{currentVersion.version_number}
              </Badge>
            )}

            {/* Indicador discreto de presença de preprocess ops.
                Em view mode reflete a versão persistida; em edit, o draft. */}
            {(editorMode === "edit" ? draftPreprocess : currentPreprocess).length > 0 && (
              <Badge
                variant="warning"
                size="sm"
                data-testid="preprocess-badge"
                title={t("editor.preprocessBadgeTitle")}
              >
                {t("editor.preprocessBadge")}
              </Badge>
            )}

            {/* Link de ajuda — abre o guia no GitHub (renderiza markdown
                com anchors). Usuários sem acesso ao repo veem o login do
                GitHub; nesse caso, copie o conteúdo de
                docs/normalization/mapping-editor-guide.md no checkout
                local. */}
            <a
              href={DOCS.mappingEditorGuide}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sm text-primary-600 hover:underline inline-flex items-center gap-1"
              data-testid="help-link"
            >
              <BookOpenIcon size={14} aria-hidden="true" /> {t("editor.helpLink")}
            </a>

            {/* Ações de edição — só na aba Editor */}
            {activeTab === "editor" && (
              <>
                {editorMode === "view" && canWrite && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleEnterEditMode}
                    leftIcon={<PencilIcon size={14} />}
                    data-testid="edit-mode-button"
                    type="button"
                  >
                    {t("editor.editRules")}
                  </Button>
                )}
                {editorMode === "edit" && (
                  <>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={handleDiscard}
                      data-testid="discard-button"
                      type="button"
                    >
                      {t("editor.discard")}
                    </Button>
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={() => setShowSaveModal(true)}
                      data-testid="save-button"
                      type="button"
                    >
                      {t("common:actions.save")}
                    </Button>
                  </>
                )}
              </>
            )}

            <Button
              variant="outline"
              size="sm"
              onClick={() => navigate(-1)}
              leftIcon={<ArrowLeftIcon size={14} />}
            >
              {t("common:actions.back")}
            </Button>
          </div>
        }
      />

      {/* Dirty flag notice */}
      {isDirty && (
        <Notice variant="warning">
          {t("editor.unsavedChanges")}
        </Notice>
      )}

      {/* Prefill banner — informa que o editor foi aberto com campo do drift */}
      {prefillBanner && (
        <Notice
          variant="info"
          data-testid="prefill-banner"
          action={
            <Button
              variant="ghost"
              size="xs"
              type="button"
              onClick={dismissPrefillBanner}
              aria-label={t("editor.dismissBannerAriaLabel")}
            >
              ✕
            </Button>
          }
        >
          {t("editor.prefillBanner.before")} <code className="font-mono text-xs">{prefillBanner}</code> {t("editor.prefillBanner.after")} <strong>{t("editor.prefillBanner.targetField")}</strong> {t("editor.prefillBanner.suffix")}
        </Notice>
      )}

      {/* Tabs */}
      <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as ActiveTab)}>
        <TabsList ariaLabel={t("editor.tabsAriaLabel")}>
          <TabsTrigger value="editor" icon={<GitBranchIcon size={16} />}>
            {t("editor.tabs.editor")}
          </TabsTrigger>
          <TabsTrigger
            value="versions"
            icon={<HistoryIcon size={16} />}
            data-testid="versions-tab"
          >
            {t("editor.tabs.versions")}
          </TabsTrigger>
          <TabsTrigger
            value="audit"
            icon={<ClipboardListIcon size={16} />}
            data-testid="audit-tab"
          >
            {t("editor.tabs.audit")}
          </TabsTrigger>
        </TabsList>

        <TabsPanel value="editor">
          <MappingEditorLayout
            payload={
              <PayloadPanel onRawEventChange={handleRawEventChange} />
            }
            rules={
              <>
                {/* Seção de pré-processamento — acima das regras.
                    Em view mode, exibe as ops da versão persistida
                    (currentPreprocess); em edit, exibe o draft. */}
                <PreprocessEditor
                  ops={editorMode === "edit" ? draftPreprocess : currentPreprocess}
                  expanded={preprocessExpanded}
                  onToggleExpand={handlePreprocessToggle}
                  onChange={handlePreprocessChange}
                  readOnly={editorMode !== "edit"}
                />

                {editorMode === "edit" ? (
                  <RulesEditor
                    rules={draftRules ?? currentRules}
                    mode="edit"
                    onChange={handleRulesChange}
                    jmespathSuggestions={jmespathSuggestions}
                    vendor={mapping.vendor}
                    eventType={mapping.event_type}
                    preprocess={draftPreprocess}
                    onImportPayload={(payload) => {
                      setDraftPreprocess(payload.preprocess ?? [])
                      handleRulesChange(payload.rules)
                    }}
                  />
                ) : (
                  <RulesEditor
                    rules={currentRules}
                    mode="view"
                    vendor={mapping.vendor}
                    eventType={mapping.event_type}
                    preprocess={currentPreprocess}
                  />
                )}
              </>
            }
            envelope={
              <EnvelopePreview
                result={dryRunResult}
                isPending={dryRunPending}
                error={dryRunError}
                onMarkIntentional={handleMarkIntentional}
              />
            }
          />
        </TabsPanel>

        <TabsPanel value="versions" data-testid="versions-tab-panel">
          <MappingVersionsTable
            mappingId={mapping.id}
            versions={mapping.versions}
            currentVersionId={mapping.current_version_id}
            onRefetch={refetch}
          />
        </TabsPanel>

        <TabsPanel value="audit" data-testid="audit-tab-panel">
          <MappingAuditTable mappingId={mapping.id} />
        </TabsPanel>
      </Tabs>

      {/* Discard confirm */}
      <ConfirmDialog
        open={showDiscardConfirm}
        title={t("editor.discardConfirm.title")}
        description={t("editor.discardConfirm.description")}
        confirmLabel={t("editor.discard")}
        cancelLabel={t("editor.discardConfirm.cancelLabel")}
        confirmVariant="danger"
        onConfirm={handleDiscardConfirm}
        onClose={() => setShowDiscardConfirm(false)}
      />

      {/* Save modal */}
      {showSaveModal && currentVersion && (
        <SaveModal
          open={showSaveModal}
          onClose={() => setShowSaveModal(false)}
          mappingId={mapping.id}
          currentRules={currentRules}
          draftRules={draftRules ?? currentRules}
          draftPayload={savePayload}
          currentVersionNumber={currentVersion.version_number}
          onSuccess={handleSaveSuccess}
          dryRunFailRatio={dryRunFailRatio}
        />
      )}
    </div>
  )
}

export default MappingEditorPage
