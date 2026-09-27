import type React from "react"
import { Fragment, useCallback, useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  SearchIcon,
  DownloadIcon,
  ClockIcon,
  UserIcon,
  ActivityIcon,
  ShieldCheckIcon,
  MapPinIcon,
  FilterIcon,
  RefreshCwIcon,
  ChevronDownIcon,
  ChevronRightIcon,
} from "lucide-react"
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "@/components/ui/Card/Card"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import Select from "@/components/ui/Select/Select"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { ErrorState } from "@/components/ui/ErrorState"
import { Notice } from "@/components/ui/Notice/Notice"
import { Badge } from "@/components/ui/Badge/Badge"
import { Tabs, TabsList, TabsTrigger, TabsPanel } from "@/components/ui/Tabs/Tabs"
import { DataTable } from "@/components/ui/DataTable/DataTable"
import { useAuth } from "@/contexts/AuthContext"
import { useHistory } from "@/hooks/useHistory"
import { useClients } from "@/hooks/useClients"
import { cn } from "@/lib/utils"
import { formatDateTime } from "@/lib/intl"
import type { AuditFilters, AuditHistoryItem, HistoryItem, SearchHistoryItem, TableColumn } from "@/types"

type TabType = "operations" | "searches" | "audit"

// R2-5.3: acima disto, a tabela desktop (buscas/operações) virtualiza — a
// virtualização em si (achatar resumo+detalhe, medir altura real) agora mora
// no DataTable, não mais duplicada aqui.
const VIRTUALIZE_THRESHOLD = 40
const VIRTUALIZED_MAX_HEIGHT_PX = 560

interface SearchRow {
  h: SearchHistoryItem
  clientName: string
  storedCount: number | null
  expired: boolean
  canDownload: boolean
}

interface OperationRow {
  h: HistoryItem
  clientName: string
}

const HistoryPage: React.FC = () => {
  const { t } = useTranslation("alerts")
  const { user } = useAuth()
  // TS-06: `user` é `AuthUser | null` — HistoryPage é acessível a qualquer
  // papel autenticado, então o `null` (sessão ainda resolvendo) é um caso
  // real, não um erro de tipo para silenciar.
  const isAdmin = user?.role === "admin"
  const { clients } = useClients()
  const { operationHistory, auditHistory, searchHistory, loading, error, fetchHistory, fetchAuditHistory, downloadAuditCSV, downloadCSV } = useHistory()

  const [selectedClient, setSelectedClient] = useState<number | null>(null)
  const [activeTab, setActiveTab] = useState<TabType>("searches")
  const emptyAuditFilters: AuditFilters = { username: "", ip_address: "", date_from: "", date_to: "" }
  const [auditFilters, setAuditFilters] = useState<AuditFilters>(emptyAuditFilters)
  const [appliedAuditFilters, setAppliedAuditFilters] = useState<AuditFilters>(emptyAuditFilters)
  const [auditCurrentPage, setAuditCurrentPage] = useState(1)
  const [auditPageSize, setAuditPageSize] = useState(25)
  const [expandedRows, setExpandedRows] = useState<Record<string, boolean>>({})
  const [downloadError, setDownloadError] = useState<string | null>(null)
  const [isDownloading, setIsDownloading] = useState(false)
  // Congelado no mount (useMemo, sem deps): é um corte de RETENÇÃO de 7 dias,
  // não precisa de precisão de milissegundo. Recalcular `Date.now()` a cada
  // render (como antes) invalidava qualquer memo que dependesse dele.
  const csvRetentionCutoff = useMemo(() => Date.now() - 7 * 24 * 60 * 60 * 1000, [])

  const auditPageSizeOptions = [
    { value: "10", label: t("history.audit.pageSizeOption", { count: 10 }) },
    { value: "25", label: t("history.audit.pageSizeOption", { count: 25 }) },
    { value: "50", label: t("history.audit.pageSizeOption", { count: 50 }) },
    { value: "100", label: t("history.audit.pageSizeOption", { count: 100 }) },
  ]

  useEffect(() => { if (!isAdmin && activeTab === "audit") setActiveTab("searches") }, [activeTab, isAdmin])
  useEffect(() => { if (activeTab !== "audit") fetchHistory(selectedClient) }, [activeTab, selectedClient, fetchHistory])
  useEffect(() => { if (activeTab === "audit") fetchAuditHistory(appliedAuditFilters) }, [activeTab, appliedAuditFilters, fetchAuditHistory])
  useEffect(() => {
    if (activeTab !== "audit") return
    const totalPages = Math.max(1, Math.ceil(auditHistory.length / auditPageSize))
    if (auditCurrentPage > totalPages) setAuditCurrentPage(totalPages)
  }, [activeTab, auditCurrentPage, auditHistory.length, auditPageSize])

  const parseUtcDate = (ds: string) => {
    const normalized = /(?:[zZ]|[+-]\d{2}:\d{2})$/.test(ds) ? ds : `${ds}Z`
    return new Date(normalized)
  }
  const formatDate = useCallback((ds: string) => { const d = parseUtcDate(ds); return Number.isNaN(d.getTime()) ? ds : formatDateTime(d) }, [])
  const getCreatedAtTimestamp = (h: SearchHistoryItem) => h.created_at ? parseUtcDate(h.created_at).getTime() : Number.NaN
  const toggleExpandedRow = (key: string) => setExpandedRows((r) => ({ ...r, [key]: !r[key] }))

  const formatPayload = (payload?: string) => {
    if (!payload?.trim()) return t("history.noPayload")
    try { return JSON.stringify(JSON.parse(payload), null, 2) } catch { return payload }
  }

  const formatSearchPayload = (h: SearchHistoryItem) => JSON.stringify({ search_id: h.search_id, client_id: h.client_id ?? null, schedule_id: h.schedule_id ?? null, statement: h.statement, table: h.table, from: h.from_ts, to: h.to_ts, result_count: h.result_count ?? null, error_message: h.error_message ?? null }, null, 2)
  const formatOperationPayload = (h: HistoryItem) => h.payload ? formatPayload(h.payload) : JSON.stringify({ operation: h.operation, endpoint: h.endpoint, response_summary: h.response_summary || null }, null, 2)
  const formatAuditPayload = (h: AuditHistoryItem) => h.request_payload ? formatPayload(h.request_payload) : JSON.stringify({ action: h.action, endpoint: h.endpoint, detail: h.detail || null }, null, 2)

  const getStoredResultCount = (h: SearchHistoryItem): number | null => {
    if (typeof h.result_count === "number") return h.result_count
    if (!h.result_json?.trim()) return h.error_message ? 0 : null
    try { const p = JSON.parse(h.result_json); const items = p?.items || p?.results || []; return Array.isArray(items) ? items.length : 0 } catch { return null }
  }
  const canDownloadStoredResult = (h: SearchHistoryItem, storedCount: number | null) => {
    if (typeof storedCount !== "number" || storedCount <= 0) return false
    if (!h.created_at) return true
    const ts = getCreatedAtTimestamp(h)
    return Number.isNaN(ts) ? true : ts >= csvRetentionCutoff
  }
  const isStoredResultExpired = (h: SearchHistoryItem) => {
    if (!h.created_at) return false
    const ts = getCreatedAtTimestamp(h)
    return Number.isNaN(ts) ? false : ts < csvRetentionCutoff
  }

  const getStatusBadge = (status: string) => {
    const num = Number(status)
    if (!Number.isNaN(num)) {
      const variant = num >= 200 && num < 300 ? "success" : num >= 400 ? "danger" : num >= 300 ? "warning" : "default"
      return <Badge variant={variant} size="sm">{num}</Badge>
    }
    const map: Record<string, "success" | "warning" | "danger"> = { completed: "success", finished: "success", running: "warning", failed: "danger", cancelled: "danger" }
    return <Badge variant={map[status] || "default"} size="sm">{status}</Badge>
  }

  const filteredSearchHistory = selectedClient ? searchHistory.filter((h) => h.client_id === selectedClient) : searchHistory
  const filteredOperationHistory = selectedClient ? operationHistory.filter((h) => h.client_id === selectedClient) : operationHistory

  // PERF-03: pré-computado uma vez por mudança de dado/cliente/idioma, em vez
  // de recalculado (com `JSON.parse` embutido) a cada render.
  const searchRows: SearchRow[] = useMemo(
    () =>
      filteredSearchHistory.map((h) => {
        const storedCount = getStoredResultCount(h)
        const clientName = h.client_id == null
          ? t("history.federatedSearch")
          : clients.find((c) => c.id === h.client_id)?.name || t("history.clientRemoved")
        return {
          h,
          clientName,
          storedCount,
          expired: isStoredResultExpired(h),
          canDownload: canDownloadStoredResult(h, storedCount),
        }
      }),
    // getStoredResultCount/canDownloadStoredResult/isStoredResultExpired só
    // fecham sobre `csvRetentionCutoff`, já na lista.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [filteredSearchHistory, clients, csvRetentionCutoff, t],
  )

  const operationRows: OperationRow[] = useMemo(
    () =>
      filteredOperationHistory.map((h) => ({
        h,
        clientName: h.client_id ? clients.find((c) => c.id === h.client_id)?.name || t("history.clientLabel", { id: h.client_id }) : t("history.system"),
      })),
    [filteredOperationHistory, clients, t],
  )

  const handleRefresh = () => { activeTab === "audit" ? fetchAuditHistory(appliedAuditFilters) : fetchHistory(selectedClient) }
  const handleAuditFilterChange = (field: keyof AuditFilters, value: string) => setAuditFilters((p) => ({ ...p, [field]: value }))
  const handleApplyAuditFilters = () => { setAuditCurrentPage(1); setAppliedAuditFilters({ ...auditFilters }) }
  const handleClearAuditFilters = () => { setAuditCurrentPage(1); setAuditFilters(emptyAuditFilters); setAppliedAuditFilters(emptyAuditFilters) }

  const totalAuditPages = Math.max(1, Math.ceil(auditHistory.length / auditPageSize))
  const auditStartIndex = (auditCurrentPage - 1) * auditPageSize
  const paginatedAuditHistory = auditHistory.slice(auditStartIndex, auditStartIndex + auditPageSize)
  const auditRangeStart = auditHistory.length === 0 ? 0 : auditStartIndex + 1
  const auditRangeEnd = Math.min(auditStartIndex + auditPageSize, auditHistory.length)

  const handleAuditExport = async () => {
    try {
      setDownloadError(null)
      setIsDownloading(true)
      await downloadAuditCSV(appliedAuditFilters)
    } catch {
      // R3-5.3: sem console.error em produção — o erro já vai pra UI via
      // `downloadError` (Notice com "tentar de novo" logo abaixo).
      setDownloadError(t("history.errors.auditExportFailed"))
    } finally {
      setIsDownloading(false)
    }
  }

  const handleSearchCsvDownload = useCallback(
    async (searchId: string) => {
      try {
        setDownloadError(null)
        setIsDownloading(true)
        await downloadCSV(searchId)
      } catch {
        // R3-5.3: idem — o erro já vai pra UI via `downloadError`.
        setDownloadError(t("history.errors.searchCsvDownloadFailed"))
      } finally {
        setIsDownloading(false)
      }
    },
    [t, downloadCSV],
  )

  const thCls = "px-4 py-3 text-left text-xs font-semibold text-text-secondary uppercase tracking-wider"
  const tdCls = "px-4 py-3 text-sm"

  const ExpandButton = ({ rowKey, label }: { rowKey: string; label: string }) => (
    <button
      type="button"
      className="p-1 rounded hover:bg-surface-tertiary transition-colors"
      aria-expanded={!!expandedRows[rowKey]}
      aria-label={label}
      onClick={() => toggleExpandedRow(rowKey)}
    >
      {expandedRows[rowKey] ? <ChevronDownIcon size={14} /> : <ChevronRightIcon size={14} />}
    </button>
  )

  // ── Colunas da tabela de buscas (R2-5.3: migrado para o DataTable) ─────────
  const searchColumns: TableColumn<SearchRow>[] = useMemo(
    () => [
      {
        key: "client",
        title: t("history.searches.columns.client"),
        dataIndex: "clientName",
        render: (_v, r) => (
          <div className="flex items-center gap-1.5">
            <UserIcon size={14} className="shrink-0 text-text-tertiary" />
            <span className="truncate max-w-[160px]" title={r.clientName}>{r.clientName}</span>
          </div>
        ),
      },
      {
        key: "query",
        title: t("history.searches.columns.query"),
        dataIndex: "clientName",
        render: (_v, r) => (
          <code className="block truncate max-w-[280px] text-xs bg-surface-tertiary px-1.5 py-0.5 rounded font-mono" title={r.h.statement}>
            {r.h.statement}
          </code>
        ),
      },
      {
        key: "status",
        title: t("common:fields.status"),
        dataIndex: "clientName",
        className: "whitespace-nowrap",
        render: (_v, r) => (
          <div className="flex items-center gap-2">
            {getStatusBadge(r.h.status)}
            {r.h.error_message ? (
              <span className="text-xs text-danger-600 truncate max-w-[120px]" title={r.h.error_message}>{r.h.error_message}</span>
            ) : r.expired ? (
              <span className="text-xs text-warning-600 whitespace-nowrap">{t("history.searches.csvExpired")}</span>
            ) : typeof r.storedCount === "number" ? (
              <span className="text-xs text-text-secondary whitespace-nowrap">{t("history.searches.resultCount", { count: r.storedCount })}</span>
            ) : null}
          </div>
        ),
      },
      {
        key: "date",
        title: t("common:fields.date"),
        dataIndex: "clientName",
        className: "whitespace-nowrap",
        render: (_v, r) => (
          <div className="flex items-center gap-1.5 text-xs whitespace-nowrap">
            <ClockIcon size={14} className="shrink-0 text-text-tertiary" />{formatDate(r.h.created_at)}
          </div>
        ),
      },
      {
        key: "actions",
        title: t("common:fields.actions"),
        dataIndex: "clientName",
        render: (_v, r) =>
          r.canDownload && (
            <Button size="xs" variant="outline" onClick={() => handleSearchCsvDownload(r.h.search_id)} disabled={isDownloading} leftIcon={<DownloadIcon size={12} />}>
              CSV
            </Button>
          ),
      },
    ],
    [t, isDownloading, formatDate, handleSearchCsvDownload],
  )

  const operationColumns: TableColumn<OperationRow>[] = useMemo(
    () => [
      {
        key: "client",
        title: t("history.operations.columns.client"),
        dataIndex: "clientName",
        render: (_v, r) => (
          <div className="flex items-center gap-1.5">
            <UserIcon size={14} className="shrink-0 text-text-tertiary" />
            <span className="truncate max-w-[160px]" title={r.clientName}>{r.clientName}</span>
          </div>
        ),
      },
      {
        key: "operation",
        title: t("history.operations.columns.operation"),
        dataIndex: "clientName",
        render: (_v, r) => <Badge variant="default" size="sm">{r.h.operation}</Badge>,
      },
      {
        key: "endpoint",
        title: t("history.operations.columns.endpoint"),
        dataIndex: "clientName",
        render: (_v, r) => <code className="block truncate max-w-[240px] text-xs font-mono" title={r.h.endpoint}>{r.h.endpoint}</code>,
      },
      {
        key: "date",
        title: t("common:fields.date"),
        dataIndex: "clientName",
        className: "whitespace-nowrap",
        render: (_v, r) => (
          <div className="flex items-center gap-1.5 text-xs whitespace-nowrap">
            <ClockIcon size={14} className="shrink-0 text-text-tertiary" />{formatDate(r.h.timestamp)}
          </div>
        ),
      },
      {
        key: "result",
        title: t("history.operations.columns.result"),
        dataIndex: "clientName",
        render: (_v, r) => (
          <span className="block truncate max-w-[220px] text-xs text-text-secondary" title={r.h.response_summary || undefined}>
            {r.h.response_summary || t("history.notAvailable")}
          </span>
        ),
      },
    ],
    [t, formatDate],
  )

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-text">{t("history.title")}</h1>
          <p className="text-sm text-text-secondary mt-1">{t("history.subtitle")}</p>
        </div>
        <div className="flex items-center gap-2">
          {activeTab === "audit" && isAdmin && (
            <Button variant="outline" size="sm" onClick={handleAuditExport} loading={isDownloading} disabled={isDownloading} leftIcon={<DownloadIcon size={14} />}>{t("common:actions.export")} CSV</Button>
          )}
          <Button variant="outline" size="sm" onClick={handleRefresh} disabled={loading} leftIcon={<RefreshCwIcon size={14} />}>{t("common:actions.refresh")}</Button>
        </div>
      </div>

      {/* Filters */}
      {(activeTab !== "audit" || isAdmin) && (
        <Card padding="md">
          {activeTab !== "audit" && (
            <div>
              <label className="flex items-center gap-1.5 text-xs font-medium text-text-secondary mb-1">
                <FilterIcon size={14} /> {t("history.filters.byClient")}
              </label>
              <Select
                options={[{ value: "", label: t("history.filters.allClients") }, ...clients.map((c) => ({ value: c.id.toString(), label: `${c.name}${c.region ? ` (${c.region})` : ""}` }))]}
                value={selectedClient?.toString() || ""}
                onChange={(v) => { if (Array.isArray(v) || v === "") { setSelectedClient(null); return } setSelectedClient(Number(v)) }}
                placeholder={t("history.filters.selectClientPlaceholder")}
              />
            </div>
          )}
          {activeTab === "audit" && isAdmin && (
            <form
              className="space-y-3"
              aria-label={t("history.audit.filtersAria")}
              onSubmit={(e) => { e.preventDefault(); handleApplyAuditFilters() }}
            >
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3">
                <Input label={t("history.audit.filters.username")} placeholder={t("history.audit.filters.usernamePlaceholder")} value={auditFilters.username || ""} onChange={(e) => handleAuditFilterChange("username", e.target.value)} />
                <Input label={t("history.audit.filters.ipAddress")} placeholder={t("history.audit.filters.ipAddressPlaceholder")} value={auditFilters.ip_address || ""} onChange={(e) => handleAuditFilterChange("ip_address", e.target.value)} />
                <Input type="date" label={t("history.audit.filters.dateFrom")} value={auditFilters.date_from || ""} onChange={(e) => handleAuditFilterChange("date_from", e.target.value)} />
                <Input type="date" label={t("history.audit.filters.dateTo")} value={auditFilters.date_to || ""} onChange={(e) => handleAuditFilterChange("date_to", e.target.value)} />
                <div>
                  <Select
                    label={t("history.audit.filters.pageSize")}
                    options={auditPageSizeOptions}
                    value={auditPageSize.toString()}
                    onChange={(v) => { if (!Array.isArray(v)) { setAuditCurrentPage(1); setAuditPageSize(Number(v)) } }}
                    placeholder={t("history.audit.filters.selectPlaceholder")}
                  />
                </div>
              </div>
              <div className="flex gap-2">
                <Button type="button" variant="outline" size="sm" onClick={handleClearAuditFilters}>{t("common:actions.clear")}</Button>
                <Button type="submit" size="sm">{t("history.audit.applyFilters")}</Button>
              </div>
            </form>
          )}
        </Card>
      )}

      {downloadError && (
        <Notice
          variant="danger"
          title={t("history.errors.downloadFailedTitle")}
          action={
            <Button variant="ghost" size="xs" onClick={() => setDownloadError(null)}>{t("history.dismiss")}</Button>
          }
        >
          {downloadError}
        </Notice>
      )}

      <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as TabType)}>
        <TabsList ariaLabel={t("history.tabs.ariaLabel")}>
          <TabsTrigger
            value="searches"
            icon={<SearchIcon size={16} />}
            badge={<Badge variant="default" size="sm">{filteredSearchHistory.length}</Badge>}
          >
            {t("history.tabs.searches")}
          </TabsTrigger>
          <TabsTrigger
            value="operations"
            icon={<ActivityIcon size={16} />}
            badge={<Badge variant="default" size="sm">{filteredOperationHistory.length}</Badge>}
          >
            {t("history.tabs.operations")}
          </TabsTrigger>
          {isAdmin && (
            <TabsTrigger
              value="audit"
              icon={<ShieldCheckIcon size={16} />}
              badge={<Badge variant="default" size="sm">{auditHistory.length}</Badge>}
            >
              {t("history.tabs.audit")}
            </TabsTrigger>
          )}
        </TabsList>

        {/* R4-5.4: cada aba precisa de um <TabsPanel> de verdade — sem isto,
            o `aria-controls` que o TabsTrigger emite (ver Tabs.tsx) apontava
            pra um id que não existia em lugar nenhum do DOM (o conteúdo era
            só um `{activeTab === "x" && (...)}` solto, fora do <Tabs>). O
            `TabsPanel` também traz de graça o Suspense local por aba (R3-8.1)
            e o `role="tabpanel"`/`aria-labelledby` corretos. */}

        {/* Searches Tab */}
        <TabsPanel value="searches">
          {activeTab === "searches" && (
            <Card>
              <CardHeader>
                <CardTitle><SearchIcon size={20} className="inline mr-2" />{t("history.tabs.searches")}</CardTitle>
                <CardDescription>{t("history.searches.description")}</CardDescription>
              </CardHeader>
              <CardContent>
                {/* R2-5.4: erro num refresh NÃO apaga dado já visível — só
                    quando não sobra nada pra mostrar é que vira ErrorState.
                    Com dado na tela, o erro é um banner por cima (com retry),
                    e a tabela (stale, mas real) continua ali. */}
                {error && searchRows.length > 0 && (
                  <Notice
                    variant="danger"
                    title={t("history.errors.loadFailedTitle")}
                    action={<Button variant="ghost" size="xs" onClick={handleRefresh}>{t("history.tryAgain")}</Button>}
                    className="mb-3"
                  >
                    {error}
                  </Notice>
                )}
                {!loading && searchRows.length === 0 ? (
                  error ? (
                    <ErrorState title={t("history.errors.loadFailedTitle")} message={error} onRetry={handleRefresh} />
                  ) : (
                    <EmptyState icon={<SearchIcon size={48} />} title={t("history.searches.emptyTitle")} description={t("history.searches.emptyDescription")} />
                  )
                ) : (
                  <DataTable<SearchRow>
                    data={searchRows}
                    columns={searchColumns}
                    loading={loading}
                    rowKey={(r) => r.h.id}
                    virtualizeRows={searchRows.length > VIRTUALIZE_THRESHOLD}
                    maxHeight={VIRTUALIZED_MAX_HEIGHT_PX}
                    tableClassName="min-w-[760px]"
                    tableAriaLabel={t("history.tabs.searches")}
                    emptyMessage={t("history.searches.emptyTitle")}
                    expandableRow={{
                      isExpanded: (r) => !!expandedRows[`search-${r.h.id}`],
                      onToggle: (r) => toggleExpandedRow(`search-${r.h.id}`),
                      toggleLabel: (r) => t("history.searches.expandAria", { searchId: r.h.search_id }),
                      renderDetail: (r) => (
                        <div className="px-4 py-3 bg-surface-tertiary/50 border-t border-border">
                          <span className="text-xs font-medium text-text-secondary block mb-1">{t("history.requestPayload")}</span>
                          <pre className="text-xs bg-surface p-3 rounded-md overflow-auto max-h-64 font-mono border border-border">{formatSearchPayload(r.h)}</pre>
                        </div>
                      ),
                    }}
                    renderMobileCard={(r) => {
                      const rowKey = `search-mobile-${r.h.id}`
                      const isExpanded = !!expandedRows[rowKey]
                      return (
                        <div className="rounded-lg border border-border bg-surface p-3">
                          <div className="flex items-start justify-between gap-2">
                            <div className="flex min-w-0 items-center gap-1.5">
                              <UserIcon size={14} className="shrink-0 text-text-tertiary" />
                              <span className="truncate text-sm font-medium text-text" title={r.clientName}>{r.clientName}</span>
                            </div>
                            <div className="shrink-0">{getStatusBadge(r.h.status)}</div>
                          </div>
                          <code className="mt-2 block truncate rounded bg-surface-tertiary px-1.5 py-0.5 font-mono text-xs" title={r.h.statement}>{r.h.statement}</code>
                          <div className="mt-2 flex items-center gap-1.5 text-xs text-text-secondary">
                            <ClockIcon size={14} className="shrink-0 text-text-tertiary" />{formatDate(r.h.created_at)}
                          </div>
                          {(r.h.error_message || r.expired || typeof r.storedCount === "number") && (
                            <div className="mt-1 text-xs">
                              {r.h.error_message ? (
                                <span className="block truncate text-danger-600" title={r.h.error_message}>{r.h.error_message}</span>
                              ) : r.expired ? (
                                <span className="text-warning-600">{t("history.searches.csvExpired")}</span>
                              ) : (
                                <span className="text-text-secondary">{t("history.searches.resultCount", { count: r.storedCount ?? 0 })}</span>
                              )}
                            </div>
                          )}
                          <div className="mt-2 flex items-center gap-2">
                            {r.canDownload && (
                              <Button size="xs" variant="outline" onClick={() => handleSearchCsvDownload(r.h.search_id)} disabled={isDownloading} leftIcon={<DownloadIcon size={12} />}>CSV</Button>
                            )}
                            <ExpandButton rowKey={rowKey} label={t("history.searches.expandAria", { searchId: r.h.search_id })} />
                          </div>
                          {isExpanded && (
                            <div className="mt-2 border-t border-border pt-2">
                              <span className="mb-1 block text-xs font-medium text-text-secondary">{t("history.requestPayload")}</span>
                              <pre className="max-h-64 overflow-auto rounded-md border border-border bg-surface-tertiary/50 p-3 font-mono text-xs">{formatSearchPayload(r.h)}</pre>
                            </div>
                          )}
                        </div>
                      )
                    }}
                  />
                )}
              </CardContent>
            </Card>
          )}
        </TabsPanel>

        {/* Operations Tab */}
        <TabsPanel value="operations">
          {activeTab === "operations" && (
            <Card>
              <CardHeader>
                <CardTitle><ActivityIcon size={20} className="inline mr-2" />{t("history.tabs.operations")}</CardTitle>
                <CardDescription>{t("history.operations.description")}</CardDescription>
              </CardHeader>
              <CardContent>
                {error && operationRows.length > 0 && (
                  <Notice
                    variant="danger"
                    title={t("history.errors.loadFailedTitle")}
                    action={<Button variant="ghost" size="xs" onClick={handleRefresh}>{t("history.tryAgain")}</Button>}
                    className="mb-3"
                  >
                    {error}
                  </Notice>
                )}
                {!loading && operationRows.length === 0 ? (
                  error ? (
                    <ErrorState title={t("history.errors.loadFailedTitle")} message={error} onRetry={handleRefresh} />
                  ) : (
                    <EmptyState icon={<ActivityIcon size={48} />} title={t("history.operations.emptyTitle")} description={t("history.operations.emptyDescription")} />
                  )
                ) : (
                  <DataTable<OperationRow>
                    data={operationRows}
                    columns={operationColumns}
                    loading={loading}
                    rowKey={(r) => r.h.id}
                    virtualizeRows={operationRows.length > VIRTUALIZE_THRESHOLD}
                    maxHeight={VIRTUALIZED_MAX_HEIGHT_PX}
                    tableClassName="min-w-[760px]"
                    tableAriaLabel={t("history.tabs.operations")}
                    emptyMessage={t("history.operations.emptyTitle")}
                    expandableRow={{
                      isExpanded: (r) => !!expandedRows[`operation-${r.h.id}`],
                      onToggle: (r) => toggleExpandedRow(`operation-${r.h.id}`),
                      toggleLabel: (r) => t("history.operations.expandAria", { operation: r.h.operation }),
                      renderDetail: (r) => (
                        <div className="px-4 py-3 bg-surface-tertiary/50 border-t border-border">
                          <span className="text-xs font-medium text-text-secondary block mb-1">{t("history.requestPayload")}</span>
                          <pre className="text-xs bg-surface p-3 rounded-md overflow-auto max-h-64 font-mono border border-border">{formatOperationPayload(r.h)}</pre>
                        </div>
                      ),
                    }}
                    renderMobileCard={(r) => {
                      const rowKey = `operation-mobile-${r.h.id}`
                      const isExpanded = !!expandedRows[rowKey]
                      return (
                        <div className="rounded-lg border border-border bg-surface p-3">
                          <div className="flex items-start justify-between gap-2">
                            <div className="flex min-w-0 items-center gap-1.5">
                              <UserIcon size={14} className="shrink-0 text-text-tertiary" />
                              <span className="truncate text-sm font-medium text-text" title={r.clientName}>{r.clientName}</span>
                            </div>
                            <Badge variant="default" size="sm">{r.h.operation}</Badge>
                          </div>
                          <code className="mt-2 block truncate font-mono text-xs text-text-secondary" title={r.h.endpoint}>{r.h.endpoint}</code>
                          <div className="mt-2 flex items-center gap-1.5 text-xs text-text-secondary">
                            <ClockIcon size={14} className="shrink-0 text-text-tertiary" />{formatDate(r.h.timestamp)}
                          </div>
                          <p className="mt-1 line-clamp-2 text-xs text-text-secondary" title={r.h.response_summary || undefined}>{r.h.response_summary || t("history.notAvailable")}</p>
                          <div className="mt-2">
                            <ExpandButton rowKey={rowKey} label={t("history.operations.expandAria", { operation: r.h.operation })} />
                          </div>
                          {isExpanded && (
                            <div className="mt-2 border-t border-border pt-2">
                              <span className="mb-1 block text-xs font-medium text-text-secondary">{t("history.requestPayload")}</span>
                              <pre className="max-h-64 overflow-auto rounded-md border border-border bg-surface-tertiary/50 p-3 font-mono text-xs">{formatOperationPayload(r.h)}</pre>
                            </div>
                          )}
                        </div>
                      )
                    }}
                  />
                )}
              </CardContent>
            </Card>
          )}
        </TabsPanel>

        {/* Audit Tab */}
        {isAdmin && (
          <TabsPanel value="audit">
            {activeTab === "audit" && (
              <Card>
              <CardHeader>
                <CardTitle><ShieldCheckIcon size={20} className="inline mr-2" />{t("history.tabs.audit")}</CardTitle>
                <CardDescription>{t("history.audit.description")}</CardDescription>
              </CardHeader>
              <CardContent>
                {error && auditHistory.length > 0 && (
                  <Notice
                    variant="danger"
                    title={t("history.errors.loadFailedTitle")}
                    action={<Button variant="ghost" size="xs" onClick={handleRefresh}>{t("history.tryAgain")}</Button>}
                    className="mb-3"
                  >
                    {error}
                  </Notice>
                )}
                {!loading && auditHistory.length === 0 ? (
                  error ? (
                    <ErrorState title={t("history.errors.loadFailedTitle")} message={error} onRetry={handleRefresh} />
                  ) : (
                    <EmptyState icon={<ShieldCheckIcon size={48} />} title={t("history.audit.emptyTitle")} description={t("history.audit.emptyDescription")} />
                  )
                ) : loading ? (
                  <DataTable
                    data={[]}
                    columns={[
                      { key: "user", title: t("history.audit.columns.user"), dataIndex: "user" },
                      { key: "action", title: t("history.audit.columns.action"), dataIndex: "action" },
                      { key: "endpoint", title: t("history.audit.columns.endpoint"), dataIndex: "endpoint" },
                      { key: "origin", title: t("history.audit.columns.origin"), dataIndex: "origin" },
                      { key: "status", title: t("common:fields.status"), dataIndex: "status" },
                      { key: "date", title: t("common:fields.date"), dataIndex: "date" },
                    ]}
                    loading
                  />
                ) : (
                  <div className="space-y-4">
                    <div className="overflow-x-auto rounded-lg border border-border">
                      <table className="w-full text-sm">
                        <thead><tr className="border-b border-border bg-surface-tertiary">
                          <th className="w-10 px-2 py-3" aria-label={t("history.expand")}></th>
                          <th className={thCls}>{t("history.audit.columns.user")}</th><th className={thCls}>{t("history.audit.columns.action")}</th><th className={thCls}>{t("history.audit.columns.endpoint")}</th><th className={thCls}>{t("history.audit.columns.origin")}</th><th className={thCls}>{t("common:fields.status")}</th><th className={thCls}>{t("common:fields.date")}</th>
                        </tr></thead>
                        <tbody className="divide-y divide-border">
                          {paginatedAuditHistory.map((h) => {
                            const rowKey = `audit-${h.id}`
                            const isExpanded = !!expandedRows[rowKey]
                            return (
                              <Fragment key={h.id}>
                                <tr className={cn("hover:bg-surface-tertiary/50", isExpanded && "bg-surface-tertiary/30")}>
                                  <td className="px-2 py-3 text-center"><ExpandButton rowKey={rowKey} label={t("history.audit.expandAria", { action: h.action })} /></td>
                                  <td className={tdCls}>
                                    <div className="flex flex-col">
                                      <Badge variant="default" size="sm">{(h.username || t("history.audit.anonymous")).trim()}</Badge>
                                      <span className="text-xs text-text-tertiary mt-0.5">{h.user_role ? h.user_role.toUpperCase() : t("history.audit.noRole")}</span>
                                    </div>
                                  </td>
                                  <td className={tdCls}><Badge variant="default" size="sm">{h.action}</Badge></td>
                                  <td className={tdCls}>
                                    <code className="text-xs font-mono" title={h.endpoint}>{h.method ? `${h.method} ` : ""}{h.endpoint}</code>
                                    {h.detail && <span className="block text-xs text-text-tertiary mt-0.5 truncate max-w-[160px]" title={h.detail}>{h.detail}</span>}
                                  </td>
                                  <td className={tdCls}><div className="flex items-center gap-1.5 text-xs"><MapPinIcon size={14} className="text-text-tertiary" />{h.ip_address || t("history.audit.ipUnidentified")}</div></td>
                                  <td className={tdCls}>{getStatusBadge(String(h.status_code || "n/a"))}</td>
                                  <td className={tdCls}><div className="flex items-center gap-1.5 text-xs"><ClockIcon size={14} className="text-text-tertiary" />{formatDate(h.created_at)}</div></td>
                                </tr>
                                {isExpanded && (
                                  <tr><td colSpan={7} className="p-0">
                                    <div className="px-4 py-3 bg-surface-tertiary/50 border-t border-border">
                                      <span className="text-xs font-medium text-text-secondary block mb-1">{t("history.requestPayload")}</span>
                                      <pre className="text-xs bg-surface p-3 rounded-md overflow-auto max-h-64 font-mono border border-border">{formatAuditPayload(h)}</pre>
                                    </div>
                                  </td></tr>
                                )}
                              </Fragment>
                            )
                          })}
                        </tbody>
                      </table>
                    </div>

                    {/* Pagination */}
                    <div className="flex items-center justify-between text-sm">
                      <span className="text-text-secondary">{t("history.audit.paginationSummary", { start: auditRangeStart, end: auditRangeEnd, total: auditHistory.length })}</span>
                      <div className="flex items-center gap-2">
                        <Button size="sm" variant="outline" onClick={() => setAuditCurrentPage((c) => Math.max(1, c - 1))} disabled={auditCurrentPage <= 1}>{t("pagination.previous")}</Button>
                        <span className="text-text-secondary text-xs">{t("pagination.pageOf", { page: auditCurrentPage, totalPages: totalAuditPages })}</span>
                        <Button size="sm" variant="outline" onClick={() => setAuditCurrentPage((c) => Math.min(totalAuditPages, c + 1))} disabled={auditCurrentPage >= totalAuditPages}>{t("pagination.next")}</Button>
                      </div>
                    </div>
                  </div>
                )}
              </CardContent>
              </Card>
            )}
          </TabsPanel>
        )}
      </Tabs>
    </div>
  )
}

export default HistoryPage
