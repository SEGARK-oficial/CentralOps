import type React from "react"
import { useEffect, useMemo, useState } from "react"
import { Trans, useTranslation } from "react-i18next"
import {
  ActivityIcon,
  AlertTriangleIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  ClockIcon,
  DatabaseIcon,
  PlayIcon,
  RefreshCcwIcon,
  RotateCcwIcon,
  SearchIcon,
  XIcon,
  ZapIcon,
} from "lucide-react"
import * as api from "@/services/api"
import type {
  CollectionState,
  CollectorSummary,
  CollectorVendor,
} from "@/types"
import { formatLag } from "@/components/health/MetricsGrid"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { useAuth } from "@/contexts/AuthContext"
import { formatDate } from "@/lib/utils"
import { currentLocale, formatNumber } from "@/lib/intl"
import type { TFunction } from "i18next"

/**
 * Espelha `_BACKLOG_LAG_THRESHOLD_SECONDS` de `backend/app/routers/pipeline_health.py`.
 * Repetido aqui de propósito e não inventado: se esta tela usasse um limiar
 * próprio, o mesmo fluxo apareceria "com backlog" em Coletores e "saudável" na
 * Saúde do Pipeline, e o operador pararia de acreditar nos dois.
 */
const BACKLOG_LAG_THRESHOLD_SECONDS = 30 * 60

/**
 * Atraso REAL do dado: `agora − watermark_at`, o instante do fornecedor até onde
 * o cursor consumiu.
 *
 * `null` quando não medível — cursor não temporal, nada coletado ainda, ou API
 * anterior a esta versão (rolling upgrade). Quem chama tem de tratar `null` como
 * "não dá para afirmar", nunca como zero.
 */
function dataLagSeconds(row: CollectionState): number | null {
  if (!row.watermark_at) return null
  const parsed = new Date(row.watermark_at).getTime()
  if (!Number.isFinite(parsed)) return null
  return Math.max(0, Math.floor((Date.now() - parsed) / 1000))
}

/**
 * Backlog CONFIRMADO exige as duas condições, igual ao backend: o ciclo parou no
 * teto de páginas E o dado está muito atrás. Só o teto é um pico sendo absorvido;
 * só o atraso é um stream sem eventos com watermark legitimamente parado.
 */
function hasBacklog(row: CollectionState): boolean {
  const lag = dataLagSeconds(row)
  return Boolean(row.last_run_capped) && lag !== null && lag > BACKLOG_LAG_THRESHOLD_SECONDS
}

/** Decide a cor do badge conforme "idade" da última coleta bem-sucedida. */
function healthBadge(
  row: CollectionState,
  t: TFunction,
): { variant: "success" | "warning" | "danger" | "outline"; label: string } {
  if ((row.consecutive_failures ?? 0) > 0 || row.last_error) {
    return { variant: "danger", label: t("collectorsPage.health.failures", { count: row.consecutive_failures ?? 0 }) }
  }
  if (!row.last_success_at) {
    return { variant: "outline", label: t("collectorsPage.health.noCollection") }
  }
  const minutes = Math.max(
    0,
    Math.floor((Date.now() - new Date(row.last_success_at).getTime()) / 60_000),
  )
  if (minutes <= 10) return { variant: "success", label: t("collectorsPage.health.active") }
  if (minutes <= 60) return { variant: "warning", label: t("collectorsPage.health.minutesAgo", { minutes }) }
  return { variant: "danger", label: t("collectorsPage.health.minutesAgo", { minutes }) }
}

// Acima deste número de vendors, a lista colapsa (contagem + busca sob demanda) —
// progressive disclosure que escala p/ 200+ streams sem floodar a página.
const VENDOR_INLINE_THRESHOLD = 24

const CollectorsPage: React.FC = () => {
  const { t } = useTranslation("config")
  // `formatLag` vive na MetricsGrid e lê as chaves do namespace `dashboard`.
  // Reaproveitado em vez de reimplementado: "há 15h" tem de ler igual aqui e na
  // Saúde do Pipeline, senão o operador acha que são duas medidas diferentes.
  const { t: tLag } = useTranslation("dashboard")
  const { user } = useAuth()
  const isAdmin = user?.role === "admin"

  const [states, setStates] = useState<CollectionState[]>([])
  const [vendors, setVendors] = useState<CollectorVendor[]>([])
  const [summary, setSummary] = useState<CollectorSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [feedback, setFeedback] = useState<
    { type: "success" | "error"; message: string } | null
  >(null)
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [resetTarget, setResetTarget] = useState<CollectionState | null>(null)
  // Vendors registrados: colapsado + busca quando a lista é grande (200+ vendors).
  const [vendorsOpen, setVendorsOpen] = useState(false)
  const [vendorQuery, setVendorQuery] = useState("")
  const filteredVendors = useMemo(() => {
    const q = vendorQuery.trim().toLowerCase()
    if (!q) return vendors
    return vendors.filter((v) =>
      `${v.platform} ${v.stream} ${v.task_name ?? ""}`.toLowerCase().includes(q),
    )
  }, [vendors, vendorQuery])
  const [resetting, setResetting] = useState(false)

  const loadAll = async () => {
    try {
      setLoading(true)
      const [stateData, vendorData, summaryData] = await Promise.all([
        api.listCollectionState(),
        api.listCollectorVendors(),
        api.getCollectorSummary(),
      ])
      setStates(stateData)
      setVendors(vendorData)
      setSummary(summaryData)
    } catch (err) {
      const message =
        err instanceof Error ? err.message : t("collectorsPage.loadError")
      setFeedback({ type: "error", message })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void loadAll()
  }, [])

  // Feedback de sucesso some sozinho após ~5s; erros persistem até nova ação.
  useEffect(() => {
    if (feedback?.type !== "success") return
    const timer = setTimeout(() => setFeedback(null), 5000)
    return () => clearTimeout(timer)
  }, [feedback])

  const rowKey = (row: CollectionState) => `${row.integration_id}:${row.stream}`

  const sortedStates = useMemo(
    () =>
      [...states].sort((a, b) => {
        const orgA = a.organization_name ?? ""
        const orgB = b.organization_name ?? ""
        if (orgA !== orgB) return orgA.localeCompare(orgB, currentLocale())
        const nameA = a.integration_name ?? ""
        const nameB = b.integration_name ?? ""
        if (nameA !== nameB) return nameA.localeCompare(nameB, currentLocale())
        return a.stream.localeCompare(b.stream)
      }),
    [states],
  )

  /**
   * O fluxo mais atrasado, COM NOME. A Saúde do Pipeline agrega N streams pelo
   * pior deles e mostra só o número; esta é a única visão por (integração,
   * fluxo) do produto, então é aqui que o operador descobre em qual fluxo mexer.
   */
  const worstDataLag = useMemo(() => {
    let worst: { row: CollectionState; seconds: number } | null = null
    for (const row of states) {
      const seconds = dataLagSeconds(row)
      if (seconds === null) continue
      if (!worst || seconds > worst.seconds) worst = { row, seconds }
    }
    return worst
  }, [states])

  const handleTrigger = async (row: CollectionState) => {
    const key = rowKey(row)
    try {
      setBusyKey(key)
      setFeedback(null)
      const result = await api.triggerCollection(row.integration_id, row.stream)
      setFeedback({
        type: "success",
        message: t("collectorsPage.triggerSuccess", { queue: result.queue, taskId: result.task_id.slice(0, 8) }),
      })
      // Pequena espera para o worker executar; recarrega no fim.
      setTimeout(() => void loadAll(), 2500)
    } catch (err) {
      const message =
        err instanceof Error ? err.message : t("collectorsPage.triggerError")
      setFeedback({ type: "error", message })
    } finally {
      setBusyKey(null)
    }
  }

  const handleConfirmReset = async () => {
    if (!resetTarget) return
    try {
      setResetting(true)
      setFeedback(null)
      await api.resetCollectorCursor(resetTarget.integration_id, resetTarget.stream)
      setFeedback({
        type: "success",
        message: t("collectorsPage.resetSuccess", {
          name: resetTarget.integration_name ?? t("collectorsPage.defaultIntegration"),
          stream: resetTarget.stream,
        }),
      })
      setResetTarget(null)
      await loadAll()
    } catch (err) {
      const message =
        err instanceof Error ? err.message : t("collectorsPage.resetError")
      setFeedback({ type: "error", message })
    } finally {
      setResetting(false)
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t("collectorsPage.eyebrow")}
        title={t("collectorsPage.title")}
        icon={<ZapIcon size={22} />}
        description={t("collectorsPage.description")}
        actions={
          <Button
            variant="outline"
            size="sm"
            leftIcon={<RefreshCcwIcon size={16} />}
            onClick={() => void loadAll()}
            loading={loading}
          >
            {t("collectorsPage.refresh")}
          </Button>
        }
      />

      {feedback && (
        <Notice
          variant={feedback.type === "success" ? "success" : "danger"}
          action={
            <button
              type="button"
              onClick={() => setFeedback(null)}
              aria-label={t("collectorsPage.closeNotice")}
              className="rounded p-1 text-current opacity-70 transition hover:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-current"
            >
              <XIcon size={16} aria-hidden="true" />
            </button>
          }
        >
          {feedback.message}
        </Notice>
      )}

      {/* KPI cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
        <KpiCard
          icon={<DatabaseIcon size={18} />}
          label={t("collectorsPage.kpi.integrationsTracked")}
          value={summary?.integrations_tracked ?? 0}
          hint={
            summary
              ? t("collectorsPage.kpi.vendorsRegisteredHint", { count: summary.vendors_registered })
              : undefined
          }
        />
        <KpiCard
          icon={<ActivityIcon size={18} />}
          label={t("collectorsPage.kpi.eventsCollected")}
          value={formatNumber(summary?.events_collected_total ?? 0)}
          hint={t("collectorsPage.kpi.eventsCollectedHint")}
        />
        <KpiCard
          icon={<AlertTriangleIcon size={18} />}
          label={t("collectorsPage.kpi.integrationsWithErrors")}
          value={summary?.integrations_with_errors ?? 0}
          intent={(summary?.integrations_with_errors ?? 0) > 0 ? "warning" : "ok"}
        />
        {/* "Quando rodou". Continua útil para achar coletor PARADO, mas o rótulo
            não pode mais dizer "lag": este número é reescrito a cada ciclo que
            termina sem erro, inclusive quando o ciclo processou o dia anterior. */}
        <KpiCard
          icon={<ClockIcon size={18} />}
          label={t("collectorsPage.kpi.maxLag")}
          value={summary?.stale_minutes_max ?? "—"}
          intent={
            summary?.stale_minutes_max != null && summary.stale_minutes_max > 15
              ? "warning"
              : "ok"
          }
          hint={t("collectorsPage.kpi.maxLagHint")}
        />
        {/* "De quando é o dado", e de qual fluxo. Sem o nome do fluxo o operador
            vê o número e não tem onde agir — a agregação da Saúde do Pipeline
            escolhe o pior stream e depois o esconde. */}
        <KpiCard
          icon={<ClockIcon size={18} />}
          label={t("collectorsPage.kpi.worstDataLag")}
          value={worstDataLag ? formatLag(worstDataLag.seconds, tLag) : "—"}
          intent={worstDataLag && hasBacklog(worstDataLag.row) ? "warning" : "ok"}
          hint={
            worstDataLag
              ? t("collectorsPage.kpi.worstDataLagHint", {
                  name: worstDataLag.row.integration_name ?? `#${worstDataLag.row.integration_id}`,
                  stream: worstDataLag.row.stream,
                })
              : t("collectorsPage.kpi.worstDataLagNone")
          }
        />
      </div>

      {/* Vendors registrados — progressive disclosure: inline p/ listas pequenas,
          colapsado + busca p/ 200+ vendors (não floodar a página). */}
      {vendors.length > 0 && (
        <Card className="p-4">
          <div className="mb-3 flex items-center justify-between gap-2">
            <span className="text-xs font-semibold uppercase tracking-[0.2em] text-text-tertiary">
              {t("collectorsPage.vendors.registered", { count: vendors.length })}
            </span>
            {vendors.length > VENDOR_INLINE_THRESHOLD && (
              <Button
                variant="ghost"
                size="xs"
                aria-expanded={vendorsOpen}
                aria-controls="vendor-registry-panel"
                leftIcon={
                  vendorsOpen ? (
                    <ChevronDownIcon size={14} />
                  ) : (
                    <ChevronRightIcon size={14} />
                  )
                }
                onClick={() =>
                  setVendorsOpen((o) => {
                    if (o) setVendorQuery("") // limpa o filtro ao colapsar
                    return !o
                  })
                }
              >
                {vendorsOpen ? t("collectorsPage.vendors.hide") : t("collectorsPage.vendors.showAll")}
              </Button>
            )}
          </div>

          {vendors.length <= VENDOR_INLINE_THRESHOLD ? (
            <div className="flex flex-wrap gap-2">
              {vendors.map((v) => (
                <Badge
                  key={`${v.platform}:${v.stream}:${v.queue}`}
                  variant="primary"
                  title={`Task: ${v.task_name} • Queue: ${v.queue} • ${v.schedule_seconds}s`}
                >
                  {v.platform} · {v.stream}{" "}
                  <span className="ml-1 opacity-60">
                    ({Math.round(v.schedule_seconds / 60)}m)
                  </span>
                </Badge>
              ))}
            </div>
          ) : vendorsOpen ? (
            <div className="space-y-3" id="vendor-registry-panel">
              <div className="relative">
                <SearchIcon
                  size={14}
                  className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-tertiary"
                />
                <input
                  type="search"
                  value={vendorQuery}
                  onChange={(e) => setVendorQuery(e.target.value)}
                  placeholder={t("collectorsPage.vendors.filterPlaceholder")}
                  aria-label={t("collectorsPage.vendors.filterAriaLabel")}
                  className="w-full rounded-md border border-border bg-surface py-1.5 pl-9 pr-3 text-sm text-text placeholder:text-text-tertiary focus:border-primary focus:outline-none"
                />
              </div>
              <div
                className="flex max-h-64 flex-wrap gap-2 overflow-auto"
                tabIndex={0}
                role="region"
                aria-label={t("collectorsPage.vendors.listAriaLabel")}
              >
                {filteredVendors.map((v) => (
                  <Badge
                    key={`${v.platform}:${v.stream}:${v.queue}`}
                    variant="primary"
                    title={t("collectorsPage.vendors.taskTooltip", { task: v.task_name, queue: v.queue, seconds: v.schedule_seconds })}
                  >
                    {v.platform} · {v.stream}{" "}
                    <span className="ml-1 opacity-60">
                      ({Math.round(v.schedule_seconds / 60)}m)
                    </span>
                  </Badge>
                ))}
              </div>
              <p className="text-[11px] text-text-tertiary">
                {t("collectorsPage.vendors.filteredCount", { filtered: filteredVendors.length, total: vendors.length })}
              </p>
            </div>
          ) : (
            <p className="text-xs text-text-secondary">
              {t("collectorsPage.vendors.collapsedHint", { count: vendors.length })}
            </p>
          )}
        </Card>
      )}

      {/* Tabela de estado */}
      <Card>
        <div className="border-b border-border p-4">
          <h2 className="text-sm font-semibold text-text">{t("collectorsPage.table.title")}</h2>
          <p className="text-xs text-text-secondary">
            <Trans
              i18nKey="collectorsPage.table.sourceHint"
              t={t}
              values={{ table: "collection_state" }}
              components={{
                code: <code className="rounded bg-surface-tertiary px-1 py-0.5 text-[11px]" />,
              }}
            />
          </p>
        </div>

        {loading && states.length === 0 ? (
          <div className="flex items-center justify-center p-10">
            <LoadingSpinner size="md" text={t("collectorsPage.table.loading")} />
          </div>
        ) : sortedStates.length === 0 ? (
          <EmptyState
            icon={<ZapIcon size={32} />}
            title={t("collectorsPage.table.emptyTitle")}
            description={
              vendors.length === 0
                ? t("collectorsPage.table.emptyNoVendors")
                : t("collectorsPage.table.emptyNoIntegration")
            }
          />
        ) : (
          <div className="overflow-x-auto">
            <table
              className="w-full min-w-[1080px] text-sm"
              role="table"
              aria-label={t("collectorsPage.table.ariaLabel")}
            >
              <thead className="bg-surface-tertiary text-xs uppercase tracking-wider text-text-secondary">
                <tr>
                  <th scope="col" className="px-4 py-3 text-left">{t("collectorsPage.table.columns.orgIntegration")}</th>
                  <th scope="col" className="px-4 py-3 text-left">{t("collectorsPage.table.columns.platformStream")}</th>
                  <th scope="col" className="whitespace-nowrap px-4 py-3 text-right">{t("collectorsPage.table.columns.events")}</th>
                  {/* Duas colunas de tempo lado a lado, cada uma com a pergunta
                      que responde escrita embaixo. É a distinção que faltava: um
                      coletor pode ter acabado de rodar E estar processando ontem. */}
                  <th scope="col" className="whitespace-nowrap px-4 py-3 text-left">
                    {t("collectorsPage.table.columns.lastSuccess")}
                    <div className="mt-0.5 text-[10px] font-normal normal-case tracking-normal text-text-tertiary">
                      {t("collectorsPage.table.columns.lastSuccessHint")}
                    </div>
                  </th>
                  <th scope="col" className="whitespace-nowrap px-4 py-3 text-left">
                    {t("collectorsPage.table.columns.dataLag")}
                    <div className="mt-0.5 text-[10px] font-normal normal-case tracking-normal text-text-tertiary">
                      {t("collectorsPage.table.columns.dataLagHint")}
                    </div>
                  </th>
                  <th scope="col" className="whitespace-nowrap px-4 py-3 text-left">{t("collectorsPage.table.columns.status")}</th>
                  <th scope="col" className="px-4 py-3 text-right">{t("collectorsPage.table.columns.actions")}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {sortedStates.map((row) => {
                  const key = rowKey(row)
                  const health = healthBadge(row, t)
                  const dataLag = dataLagSeconds(row)
                  // Org e integração são tipicamente 1:1 (cada tenant aprovado
                  // cria 1 org + 1 integração de nome derivado), então as duas
                  // colunas repetiam. Fundimos numa só: org como principal e a
                  // integração como subtítulo APENAS quando difere (evita
                  // mostrar o mesmo nome duas vezes).
                  const orgName = row.organization_name
                  const intgName = row.integration_name ?? `#${row.integration_id}`
                  const primaryName = orgName ?? intgName
                  const secondaryName = orgName && intgName !== orgName ? intgName : null
                  return (
                    <tr key={key} className="hover:bg-surface-hover">
                      <td className="px-4 py-3 text-text">
                        <div className="flex max-w-[220px] flex-col gap-0.5">
                          <span className="truncate" title={primaryName}>
                            {primaryName}
                          </span>
                          {secondaryName && (
                            <span
                              className="truncate text-xs text-text-secondary"
                              title={secondaryName}
                            >
                              {secondaryName}
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex max-w-[180px] flex-col gap-0.5">
                          <span className="truncate text-text" title={row.platform ?? undefined}>
                            {row.platform ?? "—"}
                          </span>
                          <span className="truncate text-xs text-text-secondary" title={row.stream}>
                            {row.stream}
                          </span>
                        </div>
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-right font-mono tabular-nums text-text">
                        {formatNumber(row.events_collected_total)}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-text-secondary">
                        {row.last_success_at ? formatDate(row.last_success_at) : "—"}
                      </td>
                      <td
                        className="whitespace-nowrap px-4 py-3"
                        data-testid={`collector-data-lag-${row.integration_id}-${row.stream}`}
                      >
                        <div className="flex flex-col items-start gap-1">
                          {/* O `!row.watermark_at` é redundante em runtime (é a
                              primeira coisa que `dataLagSeconds` checa) e existe
                              só para o TS estreitar o tipo sem cast. */}
                          {dataLag === null || !row.watermark_at ? (
                            // Sem watermark não dá para AFIRMAR atraso — e também
                            // não dá para afirmar que está em dia. O tooltip
                            // impede que este traço seja lido como "zero".
                            <span
                              className="text-text-tertiary"
                              title={t("collectorsPage.table.dataLagUnavailable")}
                            >
                              —
                            </span>
                          ) : (
                            <span
                              className="text-text"
                              title={t("collectorsPage.table.dataLagTooltip", {
                                date: formatDate(row.watermark_at),
                              })}
                            >
                              {formatLag(dataLag, tLag)}
                            </span>
                          )}
                          {/* Teto sem atraso confirmado é pico absorvido: não
                              acende nada, senão o badge apareceria em toda rajada
                              normal e o operador aprenderia a ignorá-lo. */}
                          {hasBacklog(row) && (
                            <Badge
                              variant="warning"
                              size="sm"
                              title={t("collectorsPage.table.backlogTooltip")}
                              data-testid={`collector-backlog-${row.integration_id}-${row.stream}`}
                            >
                              {t("collectorsPage.table.backlog")}
                            </Badge>
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <Badge variant={health.variant}>{health.label}</Badge>
                        {row.last_error && (
                          <div
                            className="mt-1 max-w-xs truncate text-xs text-danger-600"
                            title={row.last_error}
                          >
                            {row.last_error}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex justify-end gap-1">
                          <Button
                            size="xs"
                            variant="outline"
                            leftIcon={<PlayIcon size={14} />}
                            loading={busyKey === key}
                            onClick={() => void handleTrigger(row)}
                            title={t("collectorsPage.table.triggerTooltip")}
                          >
                            {t("collectorsPage.table.trigger")}
                          </Button>
                          {isAdmin && (
                            <Button
                              size="xs"
                              variant="ghost"
                              leftIcon={<RotateCcwIcon size={14} />}
                              onClick={() => setResetTarget(row)}
                              title={t("collectorsPage.table.resetTooltip")}
                            >
                              {t("collectorsPage.table.reset")}
                            </Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Modal de confirmação do reset */}
      <Modal
        open={resetTarget !== null}
        title={t("collectorsPage.resetModal.title")}
        onClose={() => !resetting && setResetTarget(null)}
      >
        <div className="space-y-3">
          <p className="text-sm text-text">
            {t("collectorsPage.resetModal.body", {
              name: resetTarget?.integration_name ?? `#${resetTarget?.integration_id}`,
              stream: resetTarget?.stream,
            })}
          </p>
          <p className="text-sm text-text-secondary">
            {t("collectorsPage.resetModal.hint")}
          </p>
          <div className="flex justify-end gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setResetTarget(null)}
              disabled={resetting}
            >
              {t("common:actions.cancel")}
            </Button>
            <Button
              variant="danger"
              size="sm"
              loading={resetting}
              onClick={() => void handleConfirmReset()}
            >
              {t("collectorsPage.resetModal.confirm")}
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}

// ── Componente auxiliar ──────────────────────────────────────────────

interface KpiCardProps {
  icon: React.ReactNode
  label: string
  value: string | number
  hint?: string
  intent?: "ok" | "warning"
}

const KpiCard: React.FC<KpiCardProps> = ({ icon, label, value, hint, intent = "ok" }) => (
  <Card className="p-4">
    <div className="flex items-start justify-between">
      <div className="space-y-1">
        <div className="text-xs font-medium uppercase tracking-wide text-text-tertiary">
          {label}
        </div>
        <div
          className={
            intent === "warning"
              ? "text-2xl font-semibold text-warning-700"
              : "text-2xl font-semibold text-text"
          }
        >
          {value}
        </div>
        {hint && <div className="text-xs text-text-secondary">{hint}</div>}
      </div>
      <div
        className={
          intent === "warning"
            ? "flex h-9 w-9 items-center justify-center rounded-xl bg-warning-50 text-warning-700"
            : "flex h-9 w-9 items-center justify-center rounded-xl bg-primary-50 text-primary-700"
        }
      >
        {icon}
      </div>
    </div>
  </Card>
)

export default CollectorsPage
