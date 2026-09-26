/**
 * RulesEditor
 * Painel central do editor de mappings.
 * Sprint 1: read-only.
 * Sprint 2: modo edit com inputs por linha, reorder por botões ↑↓, add/remove.
 * Sprint 3: busca, filtros chip, agrupamento por prefixo, collapse-all, contador.
 */

import type React from "react"
import { useCallback, useId, useRef, useState, useMemo, useEffect } from "react"
import { ChevronDownIcon, DownloadIcon, SearchIcon, ChevronsUpDownIcon, UploadIcon, LayoutTemplateIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"
import type { MappingRule, ArrayBuilderRule, PreprocessOp } from "@/types"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { RuleRow } from "@/components/mappings/RuleRow"
import { TemplatePicker } from "@/components/mappings/TemplatePicker"
import {
  parseMappingExport,
  buildMappingExport,
  buildExportFilename,
  MappingImportError,
} from "@/lib/mapping-import"

interface RulesEditorBaseProps {
  rules: MappingRule[]
  className?: string
  /**
   * Metadados do mapping — usados no nome do arquivo de exportação.
   * Disponível em view e edit (export funciona nos dois modos).
   */
  vendor?: string
  eventType?: string
  /**
   * Ops de pré-processamento atuais — incluídas no JSON exportado (schema v2).
   * Disponível em view (versão atual) e edit (draft). Se ausente/vazio, o
   * export omite o campo `preprocess`.
   */
  preprocess?: PreprocessOp[]
}

interface RulesEditorViewProps extends RulesEditorBaseProps {
  mode?: "view"
  onChange?: never
}

interface RulesEditorEditProps extends RulesEditorBaseProps {
  mode: "edit"
  onChange: (rules: MappingRule[]) => void
  /** Sugestões JMESPath repassadas a cada RuleRow para autocomplete. */
  jmespathSuggestions?: string[]
  /**
   * Callback chamado após o usuário confirmar um import bem-sucedido.
   * Recebe o payload completo (preprocess + rules) para que o pai possa
   * atualizar ambos os estados em sincronia.
   */
  onImportPayload?: (payload: { preprocess?: PreprocessOp[]; rules: MappingRule[] }) => void
}

type RulesEditorProps = RulesEditorViewProps | RulesEditorEditProps

/**
 * BUG-03: a key por POSIÇÃO (`row-${index}`) resolvia o problema de
 * remount-por-target (ver histórico abaixo), mas trocou um bug por outro: ao
 * REMOVER uma regra do meio da lista, todo mundo depois dela desliza um
 * índice — a key "row-3" passa a apontar pra outra regra, e o estado de
 * `expansionMap` (que também é chaveado por essa key) "escorrega" pra
 * regra vizinha: ela aparece expandida sem o usuário ter pedido.
 *
 * Fix: id ESTÁVEL por regra, independente de posição E de conteúdo, via
 * WeakMap referência-do-objeto → id. Funciona de graça para reorder/remove
 * (o objeto da regra sobrevivente mantém a MESMA referência, então mantém o
 * mesmo id não importa o índice); para EDIT, que troca a referência do
 * objeto (`{ ...rule, ...partial }`), o id é propagado explicitamente do
 * objeto antigo pro novo em `handleChange` — ver `linkStableId`.
 *
 * (Histórico: a key por `target` quebrava foco/debounce a cada keystroke —
 * cada letra digitada remontava o RuleRow. Position-based key resolvia isso,
 * mas criava o bug de remoção acima.)
 */
const ruleStableIds = new WeakMap<MappingRule, string>()
let stableIdCounter = 0

function stableIdFor(rule: MappingRule): string {
  let id = ruleStableIds.get(rule)
  if (id === undefined) {
    stableIdCounter += 1
    id = `rule-${stableIdCounter}`
    ruleStableIds.set(rule, id)
  }
  return id
}

/** Propaga o id estável de `source` (objeto antigo) para `target` (objeto novo). */
function linkStableId(source: MappingRule, target: MappingRule): void {
  ruleStableIds.set(target, stableIdFor(source))
}

// Contador monotônico pra gerar targets default únicos quando o usuário
// clica "Adicionar regra" várias vezes em sequência sem editar — evita
// colisão de `name` em radio groups (o browser trataria como um único
// grupo). A key de linha em si já é estável por objeto (`stableIdFor`), não
// depende mais do target.
let newRuleCounter = 0

function newRule(): MappingRule {
  newRuleCounter += 1
  return { target: `novo.campo.${newRuleCounter}`, source: "" }
}

function newArrayBuilderRule(): ArrayBuilderRule {
  return {
    target: "normalized.observables",
    kind: "array_builder",
    items: [],
    skip_null: true,
  }
}

// First path segment: "normalized.foo.bar" → "normalized"
function prefixOf(target: string): string {
  const dot = target.indexOf(".")
  return dot === -1 ? target : target.slice(0, dot)
}

// ── ChipButton — filter toggle pill ──────────────────────────────────────────

interface ChipButtonProps {
  active: boolean
  onClick: () => void
  children: React.ReactNode
  "data-testid"?: string
}

const ChipButton: React.FC<ChipButtonProps> = ({ active, onClick, children, "data-testid": testId }) => (
  <button
    type="button"
    onClick={onClick}
    data-testid={testId}
    aria-pressed={active}
    className={cn(
      "inline-flex items-center gap-1 h-7 px-2.5 text-xs font-medium rounded-full border transition-colors",
      active
        ? "bg-primary-100 border-primary-300 text-primary-700"
        : "bg-surface border-border text-text-secondary hover:bg-surface-tertiary hover:text-text",
    )}
  >
    {children}
  </button>
)

export const RulesEditor: React.FC<RulesEditorProps> = ({
  rules,
  mode = "view",
  onChange,
  className,
  vendor,
  eventType,
  preprocess,
  ...rest
}) => {
  const { t } = useTranslation("mappings")
  const jmespathSuggestions = mode === "edit" ? (rest as RulesEditorEditProps).jmespathSuggestions ?? [] : []
  const onImportPayload = mode === "edit" ? (rest as RulesEditorEditProps).onImportPayload : undefined
  const headingId = useId()

  // ── Import / Export state ────────────────────────────────────────────────
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [importError, setImportError] = useState<string | null>(null)
  // Payload completo do import pendente de confirmação (preprocess + rules)
  const [importConfirm, setImportConfirm] = useState<{ preprocess?: PreprocessOp[]; rules: MappingRule[] } | null>(null)

  // ── Template picker state ────────────────────────────────────────────────
  const [templatePickerOpen, setTemplatePickerOpen] = useState(false)

  // ── Search & filter state ────────────────────────────────────────────────
  const [search, setSearch] = useState("")
  const [filterRequired, setFilterRequired] = useState(false)
  const [filterValueMap, setFilterValueMap] = useState(false)
  const [filterTypeCast, setFilterTypeCast] = useState(false)
  const [groupByPrefix, setGroupByPrefix] = useState(false)

  // ── Controlled expansion map (key → expanded) ────────────────────────────
  const [expansionMap, setExpansionMap] = useState<Map<string, boolean>>(new Map())

  // PERF-06: estável por toda a vida do componente (updater funcional, sem
  // ler `expansionMap`/`rules` do closure) — pré-requisito para poder cachear
  // um callback por linha em `getToggleCallback` sem invalidar o cache a
  // cada render.
  const toggleExpansion = useCallback((key: string) => {
    setExpansionMap((prev) => {
      const next = new Map(prev)
      next.set(key, !prev.get(key))
      return next
    })
  }, [])

  // PERF-06: RuleRow é `memo()`, mas `onToggleExpand={() => toggleExpansion(key)}`
  // inline no render criava uma closure NOVA por linha a CADA render do
  // RulesEditor (mesmo por causas alheias à linha, tipo digitar na busca) —
  // isso sozinho já invalidava o memo de toda linha visível. Cacheamos uma
  // closure estável por id de linha, criada uma única vez.
  const toggleCallbacksRef = useRef(new Map<string, () => void>())
  const getToggleCallback = useCallback(
    (key: string): (() => void) => {
      let cb = toggleCallbacksRef.current.get(key)
      if (!cb) {
        cb = () => toggleExpansion(key)
        toggleCallbacksRef.current.set(key, cb)
      }
      return cb
    },
    [toggleExpansion],
  )

  function collapseAll() {
    setExpansionMap(new Map())
  }

  function expandAll() {
    setExpansionMap(() => {
      const next = new Map<string, boolean>()
      rules.forEach((rule) => next.set(stableIdFor(rule), true))
      return next
    })
  }

  // ── Filtered rules (retaining original index for keying) ─────────────────
  const filteredWithIndex = useMemo<Array<{ rule: MappingRule; index: number }>>(() => {
    const q = search.trim().toLowerCase()

    return rules
      .map((rule, index) => ({ rule, index }))
      .filter(({ rule }) => {
        const isArrayBuilder = rule.kind === "array_builder"
        if (q) {
          const inTarget = rule.target.toLowerCase().includes(q)
          const inSource = (!isArrayBuilder && rule.source != null)
            ? rule.source.toLowerCase().includes(q)
            : false
          const inConst =
            !isArrayBuilder && rule.const !== undefined
              ? JSON.stringify(rule.const).toLowerCase().includes(q)
              : false
          if (!inTarget && !inSource && !inConst) return false
        }
        if (filterRequired && (isArrayBuilder || !rule.required)) return false
        if (filterValueMap && (isArrayBuilder || rule.value_map == null)) return false
        if (filterTypeCast && (isArrayBuilder || rule.type_cast == null)) return false
        return true
      })
  }, [rules, search, filterRequired, filterValueMap, filterTypeCast])

  // ── Group by prefix (optional) ───────────────────────────────────────────
  const groups = useMemo<Array<{ prefix: string; items: Array<{ rule: MappingRule; index: number }> }>>(() => {
    if (!groupByPrefix) return [{ prefix: "", items: filteredWithIndex }]

    const map = new Map<string, Array<{ rule: MappingRule; index: number }>>()
    for (const item of filteredWithIndex) {
      const prefix = prefixOf(item.rule.target)
      if (!map.has(prefix)) map.set(prefix, [])
      map.get(prefix)!.push(item)
    }
    return Array.from(map.entries()).map(([prefix, items]) => ({ prefix, items }))
  }, [filteredWithIndex, groupByPrefix])

  // ── Edit handlers ─────────────────────────────────────────────────────────
  // PERF-06: antes dependiam de `[onChange, rules]` — `rules` é uma array NOVA
  // a cada edição (o pai faz `onChange([...])`), então esses 4 handlers
  // ganhavam identidade nova a cada keystroke, o que por si só invalidava o
  // memo de TODA linha (elas são props do RuleRow memoizado). `rulesRef`
  // mantém a versão mais recente sem entrar nas deps — os handlers agora só
  // mudam de identidade se `onChange` (prop do pai) mudar.
  const rulesRef = useRef(rules)
  rulesRef.current = rules

  const handleChange = useCallback(
    (index: number, updated: MappingRule) => {
      if (!onChange) return
      const current = rulesRef.current
      // BUG-03: propaga o id estável da regra ANTIGA (nesse índice) pro
      // objeto NOVO que `update()` acabou de criar — sem isso, cada edição
      // "esqueceria" o id e a linha reapareceria como se fosse outra.
      linkStableId(current[index], updated)
      const next = [...current]
      next[index] = updated
      onChange(next)
    },
    [onChange],
  )

  const handleRemove = useCallback(
    (index: number) => {
      if (!onChange) return
      onChange(rulesRef.current.filter((_, i) => i !== index))
    },
    [onChange],
  )

  // BUG-03: reorder troca a POSIÇÃO das regras, não a referência dos objetos —
  // como `stableIdFor` chaveia por referência, o id de cada regra viaja com
  // ela automaticamente. Não precisa mais rekeyar `expansionMap` manualmente.
  const handleMoveUp = useCallback(
    (index: number) => {
      if (!onChange || index === 0) return
      const current = rulesRef.current
      const next = [...current]
      ;[next[index - 1], next[index]] = [next[index], next[index - 1]]
      onChange(next)
    },
    [onChange],
  )

  const handleMoveDown = useCallback(
    (index: number) => {
      if (!onChange) return
      const current = rulesRef.current
      if (index === current.length - 1) return
      const next = [...current]
      ;[next[index], next[index + 1]] = [next[index + 1], next[index]]
      onChange(next)
    },
    [onChange],
  )

  // ── Add rule dropdown state ──────────────────────────────────────────────
  const [addMenuOpen, setAddMenuOpen] = useState(false)
  const addMenuRef = useRef<HTMLDivElement>(null)
  // Botão que abre o menu — recebe o foco de volta ao fechar via teclado.
  const addMenuTriggerRef = useRef<HTMLButtonElement>(null)
  // Container do menu (role=menu) — usado para localizar os menuitems e mover
  // o foco entre eles (padrão ARIA menu / roving focus).
  const addMenuListRef = useRef<HTMLDivElement>(null)

  // Fecha o dropdown ao clicar fora
  useEffect(() => {
    if (!addMenuOpen) return
    function handleClickOutside(e: MouseEvent) {
      if (addMenuRef.current && !addMenuRef.current.contains(e.target as Node)) {
        setAddMenuOpen(false)
      }
    }
    document.addEventListener("mousedown", handleClickOutside)
    return () => document.removeEventListener("mousedown", handleClickOutside)
  }, [addMenuOpen])

  // ── Navegação por teclado no menu (padrão ARIA menu) ─────────────────────
  // Coleta os menuitems habilitados na ordem do DOM.
  const getMenuItems = useCallback((): HTMLElement[] => {
    const root = addMenuListRef.current
    if (!root) return []
    return Array.from(
      root.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])'),
    )
  }, [])

  const focusMenuItemAt = useCallback(
    (index: number) => {
      const items = getMenuItems()
      if (items.length === 0) return
      // wrap-around: ArrowDown no último volta ao primeiro e vice-versa
      const next = (index + items.length) % items.length
      items[next]?.focus()
    },
    [getMenuItems],
  )

  // Ao abrir, move o foco para o primeiro item (comportamento esperado ao
  // ativar via Enter/Espaço/clique no trigger). Setado em microtask para
  // garantir que o menu já foi montado no DOM.
  useEffect(() => {
    if (!addMenuOpen) return
    const raf = requestAnimationFrame(() => focusMenuItemAt(0))
    return () => cancelAnimationFrame(raf)
  }, [addMenuOpen, focusMenuItemAt])

  // Fecha o menu e devolve o foco ao botão que o abriu.
  const closeAddMenuAndRefocus = useCallback(() => {
    setAddMenuOpen(false)
    addMenuTriggerRef.current?.focus()
  }, [])

  function handleMenuKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    const items = getMenuItems()
    if (items.length === 0) return
    const currentIndex = items.findIndex((el) => el === document.activeElement)
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault()
        focusMenuItemAt((currentIndex < 0 ? -1 : currentIndex) + 1)
        break
      case "ArrowUp":
        e.preventDefault()
        focusMenuItemAt((currentIndex < 0 ? items.length : currentIndex) - 1)
        break
      case "Home":
        e.preventDefault()
        focusMenuItemAt(0)
        break
      case "End":
        e.preventDefault()
        focusMenuItemAt(items.length - 1)
        break
      case "Escape":
        e.preventDefault()
        closeAddMenuAndRefocus()
        break
      case "Tab":
        // Tab fora do menu fecha (sem prevenir o comportamento padrão de Tab,
        // para que o foco siga o fluxo natural da página).
        setAddMenuOpen(false)
        break
      default:
        break
    }
  }

  function handleAdd(rule: MappingRule) {
    if (!onChange) return
    const newKey = stableIdFor(rule)
    // Auto-expandir a regra recém-adicionada — o usuário acabou de criar
    // e quer editar imediatamente. As outras permanecem como estavam.
    setExpansionMap((prev) => {
      const next = new Map(prev)
      next.set(newKey, true)
      return next
    })
    onChange([...rules, rule])
    setAddMenuOpen(false)
  }

  function handleAddScalar() {
    handleAdd(newRule())
  }

  function handleAddArrayBuilder() {
    handleAdd(newArrayBuilderRule())
  }

  function handleOpenTemplatePicker() {
    setAddMenuOpen(false)
    setTemplatePickerOpen(true)
  }

  function handleTemplatePick(template: import("@/data/ocsfTemplates").OcsfTemplate) {
    if (!onChange) return
    onChange(template.rules)
    setTemplatePickerOpen(false)
    // Colapsa tudo após carregar template para apresentação limpa
    setExpansionMap(new Map())
  }

  // ── Export ───────────────────────────────────────────────────────────────

  function handleExport() {
    const exported = buildMappingExport(rules, { vendor, event_type: eventType }, preprocess)
    const blob = new Blob([JSON.stringify(exported, null, 2)], { type: "application/json" })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = buildExportFilename(vendor, eventType)
    a.click()
    URL.revokeObjectURL(url)
  }

  // ── Import ───────────────────────────────────────────────────────────────

  function handleImportClick() {
    setImportError(null)
    fileInputRef.current?.click()
  }

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    // Reset para permitir reimportar o mesmo arquivo
    e.target.value = ""

    const reader = new FileReader()
    reader.onload = (ev) => {
      const text = ev.target?.result
      if (typeof text !== "string") return
      try {
        const parsed = parseMappingExport(text)
        setImportError(null)
        setImportConfirm({ preprocess: parsed.preprocess, rules: parsed.rules })
      } catch (err) {
        setImportError(err instanceof MappingImportError ? err.message : t("rulesEditor.importFileError"))
      }
    }
    reader.readAsText(file)
  }

  function handleImportConfirm() {
    if (!importConfirm) return
    if (onImportPayload) {
      // Propaga preprocess + rules ao pai para atualizar ambos em sincronia
      onImportPayload({ preprocess: importConfirm.preprocess, rules: importConfirm.rules })
    } else if (onChange) {
      // Fallback: apenas rules (sem onImportPayload configurado)
      onChange(importConfirm.rules)
    }
    setImportConfirm(null)
    collapseAll()
  }

  function handleImportCancel() {
    setImportConfirm(null)
    setImportError(null)
  }

  const filteredCount = filteredWithIndex.length
  const hasFilter = search.trim() !== "" || filterRequired || filterValueMap || filterTypeCast

  return (
    <section
      role="region"
      aria-labelledby={headingId}
      data-testid="rules-editor"
      className={cn(
        "flex flex-col gap-3 rounded-lg border border-border bg-surface p-4 min-h-0",
        className,
      )}
    >
      {/* ── Header row ─────────────────────────────────────────────────── */}
      <div className="flex items-center gap-2">
        <h2
          id={headingId}
          className="text-sm font-semibold text-text flex-1"
        >
          {t("rulesEditor.heading")}
        </h2>
        {!hasFilter && (
          <Badge variant="default" size="sm">
            {t("rulesEditor.total", { count: rules.length })}
          </Badge>
        )}

        {/* Export — visível sempre (view e edit) */}
        {rules.length > 0 && (
          <Button
            variant="outline"
            size="xs"
            onClick={handleExport}
            type="button"
            leftIcon={<DownloadIcon size={12} aria-hidden="true" />}
            data-testid="export-rules-button"
            aria-label={t("rulesEditor.exportAriaLabel")}
          >
            {t("common:actions.export")}
          </Button>
        )}

        {/* Import — visível apenas em edit mode */}
        {mode === "edit" && (
          <>
            <Button
              variant="outline"
              size="xs"
              onClick={handleImportClick}
              type="button"
              leftIcon={<UploadIcon size={12} aria-hidden="true" />}
              data-testid="import-rules-button"
              aria-label={t("rulesEditor.importAriaLabel")}
            >
              {t("rulesEditor.import")}
            </Button>
            {/* File input oculto — acionado pelo botão acima */}
            <input
              ref={fileInputRef}
              type="file"
              accept=".json,application/json"
              onChange={handleFileChange}
              className="sr-only"
              aria-hidden="true"
              tabIndex={-1}
            />
          </>
        )}
      </div>

      {/* ── Import error ───────────────────────────────────────────────── */}
      {importError && (
        <div
          role="alert"
          className="rounded-md border border-danger-300 bg-danger-50 px-3 py-2 text-xs text-danger-700"
          data-testid="import-error"
        >
          <strong>{t("rulesEditor.importErrorPrefix")}</strong> {importError}
        </div>
      )}

      {/* ── Import confirm dialog ──────────────────────────────────────── */}
      {importConfirm && (
        <div
          role="alertdialog"
          aria-labelledby="import-confirm-title"
          className="rounded-md border border-warning-300 bg-warning-50 px-3 py-2 flex flex-col gap-2"
          data-testid="import-confirm"
        >
          <p id="import-confirm-title" className="text-sm font-medium text-warning-800">
            {t("rulesEditor.importConfirm.title")}
          </p>
          <p className="text-xs text-warning-700">
            {t("rulesEditor.importConfirm.replaceCurrent", { count: rules.length })}{" "}
            {t("rulesEditor.importConfirm.withImported", { count: importConfirm.rules.length })}
          </p>
          <div className="flex gap-2">
            <Button
              variant="primary"
              size="xs"
              type="button"
              onClick={handleImportConfirm}
              data-testid="import-confirm-button"
            >
              {t("rulesEditor.importConfirm.confirm")}
            </Button>
            <Button
              variant="outline"
              size="xs"
              type="button"
              onClick={handleImportCancel}
              data-testid="import-cancel-button"
            >
              {t("common:actions.cancel")}
            </Button>
          </div>
        </div>
      )}

      {/* ── Control bar ────────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-2" aria-label={t("rulesEditor.filterControlsAriaLabel")}>
        {/* Search */}
        <div className="flex-1 min-w-[180px]">
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("rulesEditor.searchPlaceholder")}
            leftIcon={<SearchIcon size={14} />}
            aria-label={t("rulesEditor.searchAriaLabel")}
            data-testid="rules-search"
          />
        </div>

        {/* Filter chips */}
        <ChipButton
          active={filterRequired}
          onClick={() => setFilterRequired((p) => !p)}
          data-testid="filter-required"
        >
          {t("rulesEditor.filters.required")}
        </ChipButton>
        <ChipButton
          active={filterValueMap}
          onClick={() => setFilterValueMap((p) => !p)}
          data-testid="filter-value-map"
        >
          value_map
        </ChipButton>
        <ChipButton
          active={filterTypeCast}
          onClick={() => setFilterTypeCast((p) => !p)}
          data-testid="filter-type-cast"
        >
          type_cast
        </ChipButton>

        {/* Group toggle */}
        <ChipButton
          active={groupByPrefix}
          onClick={() => setGroupByPrefix((p) => !p)}
          data-testid="toggle-group"
        >
          <ChevronsUpDownIcon size={12} aria-hidden="true" />
          {t("rulesEditor.filters.group")}
        </ChipButton>

        {/* Expand all (apenas em edit mode — em view a UX é começar tudo
            colapsado e o user expande individualmente) */}
        {mode === "edit" && (
          <Button
            variant="outline"
            size="xs"
            onClick={expandAll}
            type="button"
            data-testid="expand-all"
            aria-label={t("rulesEditor.expandAllAriaLabel")}
          >
            {t("rulesEditor.expandAll")}
          </Button>
        )}

        {/* Collapse all */}
        <Button
          variant="outline"
          size="xs"
          onClick={collapseAll}
          type="button"
          data-testid="collapse-all"
          aria-label={t("rulesEditor.collapseAllAriaLabel")}
        >
          {t("rulesEditor.collapseAll")}
        </Button>
      </div>

      {/* ── Counter ────────────────────────────────────────────────────── */}
      {hasFilter && (
        <p className="text-xs text-text-secondary" data-testid="rules-counter" aria-live="polite">
          {filteredCount < rules.length
            ? t("rulesEditor.showingCount", { filtered: filteredCount, total: rules.length, count: rules.length })
            : t("rulesEditor.total", { count: rules.length })}
        </p>
      )}

      {/* ── Rule list ──────────────────────────────────────────────────── */}
      {rules.length === 0 ? (
        mode === "edit" ? (
          <div className="text-sm text-text-secondary text-center py-4">
            {t("rulesEditor.emptyEditHint")}
          </div>
        ) : (
          <EmptyState
            title={t("rulesEditor.emptyView.title")}
            description={t("rulesEditor.emptyView.description")}
          />
        )
      ) : filteredCount === 0 ? (
        <div className="flex flex-col items-center gap-2 py-6" data-testid="no-results">
          <p className="text-sm text-text-secondary">
            {t("rulesEditor.noFilterResults")}
          </p>
          <Button
            variant="outline"
            size="xs"
            type="button"
            onClick={() => {
              setSearch("")
              setFilterRequired(false)
              setFilterValueMap(false)
              setFilterTypeCast(false)
            }}
            data-testid="clear-filters"
          >
            {t("rulesEditor.clearFilters")}
          </Button>
        </div>
      ) : (
        <div className="flex flex-col gap-2 overflow-auto">
          {groups.map(({ prefix, items }) =>
            groupByPrefix && prefix ? (
              <details key={prefix} open className="group">
                <summary className="flex items-center gap-2 cursor-pointer list-none select-none py-1 text-xs font-semibold text-text-secondary uppercase tracking-wide">
                  <ChevronDownIcon
                    size={12}
                    className="transition-transform group-open:rotate-0 -rotate-90"
                    aria-hidden="true"
                  />
                  {prefix}
                  <Badge variant="default" size="sm">{items.length}</Badge>
                </summary>
                <div className="flex flex-col gap-2 mt-1">
                  {items.map(({ rule, index }) =>
                    renderRow(rule, index),
                  )}
                </div>
              </details>
            ) : (
              items.map(({ rule, index }) =>
                renderRow(rule, index),
              )
            ),
          )}
        </div>
      )}

      {mode === "edit" && (
        <div ref={addMenuRef} className="relative mt-2 self-start">
          <Button
            ref={addMenuTriggerRef}
            variant="outline"
            size="sm"
            onClick={() => setAddMenuOpen((prev) => !prev)}
            onKeyDown={(e) => {
              // ArrowDown/ArrowUp abre o menu já posicionando o foco no
              // primeiro item (o useEffect de abertura cuida do foco).
              if ((e.key === "ArrowDown" || e.key === "ArrowUp") && !addMenuOpen) {
                e.preventDefault()
                setAddMenuOpen(true)
              } else if (e.key === "Escape" && addMenuOpen) {
                e.preventDefault()
                setAddMenuOpen(false)
              }
            }}
            type="button"
            aria-haspopup="menu"
            aria-expanded={addMenuOpen}
            data-testid="add-rule-button"
            rightIcon={
              <ChevronDownIcon
                size={12}
                className={cn("transition-transform", addMenuOpen && "rotate-180")}
                aria-hidden="true"
              />
            }
          >
            {t("rulesEditor.addRule")}
          </Button>

          {addMenuOpen && (
            <div
              ref={addMenuListRef}
              role="menu"
              aria-label={t("rulesEditor.addMenuAriaLabel")}
              onKeyDown={handleMenuKeyDown}
              className={cn(
                // LAY-39: token de z-index (não o número mágico `z-20`).
                "absolute left-0 top-full mt-1 z-dropdown min-w-[220px]",
                "rounded-md border border-border bg-surface shadow-md py-1",
              )}
              data-testid="add-rule-menu"
            >
              <button
                type="button"
                role="menuitem"
                tabIndex={-1}
                onClick={handleAddScalar}
                data-testid="add-scalar-rule"
                className={cn(
                  "w-full text-left px-3 py-2 text-sm text-text",
                  "hover:bg-surface-tertiary focus:bg-surface-tertiary focus-ring",
                )}
              >
                <span className="font-medium">{t("rulesEditor.addMenu.scalarRule")}</span>
                <span className="block text-xs text-text-tertiary mt-0.5">
                  {t("rulesEditor.addMenu.scalarRuleHint")}
                </span>
              </button>
              <button
                type="button"
                role="menuitem"
                tabIndex={-1}
                onClick={handleAddArrayBuilder}
                data-testid="add-array-builder-rule"
                className={cn(
                  "w-full text-left px-3 py-2 text-sm text-text",
                  "hover:bg-surface-tertiary focus:bg-surface-tertiary focus-ring",
                )}
              >
                <span className="font-medium">{t("rulesEditor.addMenu.arrayBuilder")} <span className="text-success-700">({t("rulesEditor.addMenu.observables")})</span></span>
                <span className="block text-xs text-text-tertiary mt-0.5">
                  {t("rulesEditor.addMenu.arrayBuilderHint")}
                </span>
              </button>
              <div className="my-1 border-t border-border" role="separator" />
              <button
                type="button"
                role="menuitem"
                tabIndex={-1}
                onClick={handleOpenTemplatePicker}
                data-testid="load-ocsf-template"
                className={cn(
                  "w-full text-left px-3 py-2 text-sm text-text",
                  "hover:bg-surface-tertiary focus:bg-surface-tertiary focus-ring",
                  "flex items-start gap-2",
                )}
              >
                <LayoutTemplateIcon size={14} className="mt-0.5 shrink-0 text-primary-600" aria-hidden="true" />
                <span>
                  <span className="font-medium">{t("rulesEditor.addMenu.loadTemplate")}</span>
                  <span className="block text-xs text-text-tertiary mt-0.5">
                    {t("rulesEditor.addMenu.loadTemplateHint")}
                  </span>
                </span>
              </button>
            </div>
          )}
        </div>
      )}

      {/* ── Template picker modal ───────────────────────────────────────── */}
      {mode === "edit" && (
        <TemplatePicker
          open={templatePickerOpen}
          onClose={() => setTemplatePickerOpen(false)}
          onPick={handleTemplatePick}
          existingRulesCount={rules.length}
        />
      )}
    </section>
  )

  // ── renderRow ────────────────────────────────────────────────────────────

  function renderRow(rule: MappingRule, index: number) {
    const key = stableIdFor(rule)

    if (mode === "edit") {
      return (
        <RuleRow
          key={key}
          rule={rule}
          index={index}
          mode="edit"
          onChange={handleChange}
          onRemove={handleRemove}
          onMoveUp={handleMoveUp}
          onMoveDown={handleMoveDown}
          canMoveUp={index > 0}
          canMoveDown={index < rules.length - 1}
          expanded={expansionMap.get(key) ?? false}
          onToggleExpand={getToggleCallback(key)}
          jmespathSuggestions={jmespathSuggestions}
        />
      )
    }

    return (
      <RuleRow
        key={key}
        rule={rule}
        mode="view"
        expanded={expansionMap.get(key) ?? false}
        onToggleExpand={getToggleCallback(key)}
      />
    )
  }
}

export default RulesEditor
