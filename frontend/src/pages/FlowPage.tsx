/**
 * FlowPage (/flow) — visão de fluxo de dados da plataforma (estilo Cribl/Axoflow).
 *
 * Fontes → Roteamento → Destinos com:
 * - Zoom/pan via FlowCanvas
 * - Sankey ribbons + partículas fluindo
 * - Drill-down lateral (FlowNodeDetail)
 * - Feed ao vivo colapsável (FlowLiveFeed)
 * - Poll 15s do grafo
 */
import type React from "react"
import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { RefreshCwIcon, ActivityIcon, NetworkIcon, TrashIcon, SendIcon } from "lucide-react"
import * as api from "@/services/api"
import { Card } from "@/components/ui/Card/Card"
import { Button } from "@/components/ui/Button/Button"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { Notice } from "@/components/ui/Notice/Notice"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import LoadingSpinner from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { FlowCanvas } from "@/components/flow/FlowCanvas"
import { FlowNodeDetail } from "@/components/flow/FlowNodeDetail"
import { FlowLiveFeed } from "@/components/flow/FlowLiveFeed"
import { CostSavingsCard } from "@/components/observability/CostSavingsCard"
import { fmtRate } from "@/lib/fmt"
import type { FlowGraph as FlowGraphData } from "@/types"
import type { FlowNodeId } from "@/components/flow/FlowCanvas"

const POLL_MS = 15000

interface StatCardProps {
  label: string
  value: string
  icon: React.ReactNode
  tone?: "default" | "primary" | "danger"
}

const StatCard: React.FC<StatCardProps> = ({ label, value, icon, tone = "default" }) => {
  const toneCls =
    tone === "primary"
      ? "text-primary-600"
      : tone === "danger"
        ? "text-danger-600"
        : "text-text-secondary"
  return (
    <Card padding="sm" className="shadow-sm">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">
          {label}
        </span>
        <span className={toneCls}>{icon}</span>
      </div>
      <div className="mt-2 text-2xl font-semibold tabular-nums text-text">{value}</div>
    </Card>
  )
}

const FlowPage: React.FC = () => {
  const { t } = useTranslation("dashboard")
  const [data, setData] = useState<FlowGraphData | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const [selectedNode, setSelectedNode] = useState<FlowNodeId | null>(null)
  const [feedOpen, setFeedOpen] = useState(false)

  const load = useCallback(async (silent: boolean) => {
    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl
    if (silent) setRefreshing(true)
    else setLoading(true)
    try {
      const res = await api.getFlowGraph({ signal: ctrl.signal })
      setData(res)
      setError(null)
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") return
      setError(err instanceof Error ? err.message : t("flowPage.loadError"))
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [t])

  useEffect(() => {
    void load(false)
    const id = window.setInterval(() => void load(true), POLL_MS)
    return () => {
      window.clearInterval(id)
      abortRef.current?.abort()
    }
  }, [load])

  const totals = data?.totals
  const isEmpty =
    data &&
    data.sources.length === 0 &&
    data.routes.length === 0 &&
    data.destinations.length === 0

  return (
    <div className="space-y-5">
      <PageHeader
        title={t("flowPage.title")}
        description={t("flowPage.description")}
        actions={
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setFeedOpen((v) => !v)}
              leftIcon={<ActivityIcon size={14} />}
            >
              {t("flowPage.liveFeed")}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void load(true)}
              disabled={refreshing}
              leftIcon={
                <RefreshCwIcon
                  size={14}
                  className={refreshing ? "animate-spin" : undefined}
                />
              }
            >
              {t("common:actions.refresh")}
            </Button>
          </div>
        }
      />

      {error && (
        <Notice variant="danger" title={t("flowPage.notLoaded")}>
          {error}
        </Notice>
      )}

      {loading && !data ? (
        <div className="flex justify-center py-20">
          <LoadingSpinner />
        </div>
      ) : isEmpty ? (
        <EmptyState
          title={t("flowPage.empty.title")}
          description={t("flowPage.empty.description")}
        />
      ) : data ? (
        <>
          {/* KPI strip */}
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <StatCard
              label={t("flowPage.stats.ingest")}
              // Unidade unificada do funil: tudo em eventos/min (ingest/delivered
              // vêm em EPS do backend → ×60). Mantém os 4 cards comparáveis.
              value={t("flowPage.stats.perMinute", { value: fmtRate((totals?.ingest_eps ?? 0) * 60) })}
              icon={<ActivityIcon size={18} />}
              tone="primary"
            />
            <StatCard
              label={t("flowPage.stats.routed")}
              value={t("flowPage.stats.perMinute", { value: fmtRate(totals?.routed_per_min ?? 0) })}
              icon={<NetworkIcon size={18} />}
            />
            <StatCard
              label={t("flowPage.stats.dropped")}
              value={t("flowPage.stats.perMinute", { value: fmtRate(totals?.drop_per_min ?? 0) })}
              icon={<TrashIcon size={18} />}
              tone={totals && totals.drop_per_min > 0 ? "danger" : "default"}
            />
            <StatCard
              label={t("flowPage.stats.delivered")}
              value={t("flowPage.stats.perMinute", { value: fmtRate((totals?.delivered_eps ?? 0) * 60) })}
              icon={<SendIcon size={18} />}
              tone="primary"
            />
          </div>

          {/* Redução de volume & custo — auto-oculta se o metering está off */}
          <CostSavingsCard />

          {/* Live feed (colapsável) */}
          <FlowLiveFeed
            destinations={data.destinations}
            open={feedOpen}
            onToggle={() => setFeedOpen((v) => !v)}
          />

          {/* Flow canvas */}
          <Card padding="md" className="space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-text">{t("flowPage.topology.title")}</h3>
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-text-tertiary">
                <span className="flex items-center gap-1">
                  <span
                    className="inline-block h-2.5 w-2.5 rounded-full"
                    style={{ backgroundColor: "var(--color-success-500)" }}
                    aria-hidden="true"
                  />
                  {t("flowPage.topology.healthy")}
                </span>
                <span className="flex items-center gap-1">
                  <span
                    className="inline-block h-2.5 w-2.5 rounded-full"
                    style={{ backgroundColor: "var(--color-warning-500)" }}
                    aria-hidden="true"
                  />
                  {t("flowPage.topology.degraded")}
                </span>
                <span className="flex items-center gap-1">
                  <span
                    className="inline-block h-2.5 w-2.5 rounded-full"
                    style={{ backgroundColor: "var(--color-danger-500)" }}
                    aria-hidden="true"
                  />
                  {t("flowPage.topology.unavailable")}
                </span>
                <span>{t("flowPage.topology.legend")}</span>
              </div>
            </div>
            <FlowCanvas
              data={data}
              onSelectNode={setSelectedNode}
              className="min-h-[300px] rounded-md"
            />
          </Card>
        </>
      ) : null}

      {/* Drill-down side panel */}
      <FlowNodeDetail
        node={selectedNode}
        onClose={() => setSelectedNode(null)}
      />
    </div>
  )
}

export default FlowPage
