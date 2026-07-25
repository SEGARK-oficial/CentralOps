import type React from "react"
import { useCallback, useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { ActivityIcon, RefreshCwIcon, ServerIcon } from "lucide-react"
import * as api from "@/services/api"
import type { Integration, IntegrationPipelineHealth, PipelineHealthStatus } from "@/types"
import { DestinationHealthGrid } from "@/components/health/DestinationHealthGrid"
import { HealthBadge } from "@/components/health/HealthBadge"
import { formatLag } from "@/components/health/MetricsGrid"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import EmptyState from "@/components/ui/EmptyState/EmptyState"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Notice } from "@/components/ui/Notice/Notice"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { cn } from "@/lib/utils"
import { formatDateTime } from "@/lib/intl"

/**
 * Health sintético para integrações ainda sem dados de coleta: assim elas
 * permanecem visíveis na grade (status 'unknown') em vez de sumirem da tela.
 */
function buildUnknownHealth(integrationId: number): IntegrationPipelineHealth {
  return {
    integration_id: integrationId,
    status: "unknown",
    events_per_minute: null,
    lag_seconds: null,
    // Integração sem dado de coleta não tem atraso MEDÍVEL — `null` faz a linha
    // sumir do card, em vez de afirmar "em dia" com um zero.
    watermark_lag_seconds: null,
    backlog_detected: false,
    last_error: null,
    last_success_at: null,
    mapped_field_ratio: null,
    drift_count_24h: 0,
    quarantine_count_24h: 0,
    cached_at: "",
  }
}

type FilterTab = "all" | "healthy" | "problem"

interface HealthCardProps {
  integration: Integration
  health: IntegrationPipelineHealth
}

const HealthCard: React.FC<HealthCardProps> = ({ integration, health }) => {
  const { t } = useTranslation("dashboard")
  const navigate = useNavigate()
  return (
    <Card padding="md" className="flex flex-col gap-4">
      {/* Nome numa linha inteira, selos na de baixo: com dois selos ao lado, o
          nome truncava em "Firewall bo…" — e o card existe para identificar
          QUAL integração está com problema. */}
      <div>
        <h3 className="truncate font-semibold text-text">{integration.name}</h3>
        <p className="truncate font-mono text-xs text-text-tertiary">
          {integration.organization_name || "—"}
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-1">
          {/* Backlog não é o mesmo que "não saudável": o coletor está coletando,
              só não está dando conta. Badge própria, ao lado do status. */}
          {health.backlog_detected && (
            <Badge
              variant="warning"
              size="sm"
              title={t("pipelineHealthPage.card.backlogTitle")}
              data-testid={`health-backlog-${integration.id}`}
            >
              {t("pipelineHealthPage.card.backlog")}
            </Badge>
          )}
          <HealthBadge status={health.status} size="sm" />
        </div>
      </div>

      {/* O número é o conteúdo: a vazão em Archivo grande, a unidade em mono ao
          lado. O resto do card é contexto e fica um degrau abaixo. */}
      <div className="flex items-baseline gap-1.5">
        <span className="font-display text-3xl leading-none text-text">
          {health.events_per_minute !== null ? String(health.events_per_minute) : "—"}
        </span>
        <span className="font-mono text-[11px] uppercase tracking-[0.1em] text-text-tertiary">
          {t("pipelineHealthPage.card.eventsPerMinute")}
        </span>
      </div>

      <dl className="grid grid-cols-2 gap-x-3 gap-y-1 font-mono text-xs">
        {/* Dois atrasos, dois nomes. "Última coleta" responde quando o coletor
            rodou; "Atraso dos dados" responde de quando é o dado. Foi por só
            existir o primeiro que um coletor 15h atrasado passou por saudável. */}
        <dt className="text-text-tertiary" title={t("pipelineHealthPage.card.lastCollectionTitle")}>
          {t("pipelineHealthPage.card.lastCollection")}
        </dt>
        <dd className="text-right text-text" data-testid={`health-last-collection-${integration.id}`}>
          {formatLag(health.lag_seconds, t)}
        </dd>

        {/* Ausente (`null`, ou campo que a API antiga nem manda) = não medível ⇒
            a linha inteira some. Nunca "0" nem "—" aqui: os dois se leem como
            "em dia". */}
        {health.watermark_lag_seconds != null && (
          <>
            <dt className="text-text-tertiary" title={t("pipelineHealthPage.card.dataLagTitle")}>
              {t("pipelineHealthPage.card.dataLag")}
            </dt>
            <dd className="text-right text-text" data-testid={`health-data-lag-${integration.id}`}>
              {formatLag(health.watermark_lag_seconds, t)}
            </dd>
          </>
        )}

        <dt className="text-text-tertiary">{t("pipelineHealthPage.card.drift24h")}</dt>
        <dd className="text-right text-text">{String(health.drift_count_24h)}</dd>

        <dt className="text-text-tertiary">{t("pipelineHealthPage.card.quarantine24h")}</dt>
        <dd className="text-right text-text">{String(health.quarantine_count_24h)}</dd>
      </dl>

      <Button
        variant="outline"
        size="sm"
        className="mt-auto"
        onClick={() => navigate(`/integrations/${integration.id}`)}
      >
        {t("common:actions.details")}
      </Button>
    </Card>
  )
}

const PipelineHealthPage: React.FC = () => {
  const { t } = useTranslation("dashboard")
  const [integrations, setIntegrations] = useState<Integration[]>([])
  const [healthMap, setHealthMap] = useState<Map<number, IntegrationPipelineHealth>>(new Map())
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filterTab, setFilterTab] = useState<FilterTab>("all")
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const [showDestinations, setShowDestinations] = useState(false)

  const load = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const list = await api.listIntegrations()
      setIntegrations(list)

      // Attempt bulk endpoint first; fall back to parallel individual requests.
      let healthItems: IntegrationPipelineHealth[]
      try {
        healthItems = await api.listPipelineHealth()
      } catch {
        const results = await Promise.allSettled(
          list.map((i) => api.getIntegrationPipelineHealth(i.id)),
        )
        healthItems = results
          .filter((r): r is PromiseFulfilledResult<IntegrationPipelineHealth> => r.status === "fulfilled")
          .map((r) => r.value)
      }

      const map = new Map<number, IntegrationPipelineHealth>()
      for (const item of healthItems) {
        map.set(item.integration_id, item)
      }
      setHealthMap(map)
      setUpdatedAt(new Date())
    } catch (e) {
      setError(e instanceof Error ? e.message : t("pipelineHealthPage.loadHealthError"))
    } finally {
      setIsLoading(false)
    }
  }, [t])

  useEffect(() => {
    void load()
  }, [load])

  // Resolve o health de cada integração: usa o real quando existe, senão
  // 'unknown' sintético — garantindo que nenhuma integração desapareça.
  const resolveHealth = (integrationId: number): IntegrationPipelineHealth =>
    healthMap.get(integrationId) ?? buildUnknownHealth(integrationId)

  const statuses = integrations.map((i) => resolveHealth(i.id).status)
  const counts = {
    healthy: statuses.filter((s) => s === "healthy").length,
    degraded: statuses.filter((s) => s === "degraded").length,
    unhealthy: statuses.filter((s) => s === "unhealthy").length,
    unknown: statuses.filter((s) => s === "unknown").length,
  }

  const problemStatuses: PipelineHealthStatus[] = ["degraded", "unhealthy", "unknown"]
  const problemCount = counts.degraded + counts.unhealthy + counts.unknown

  const filtered = integrations.filter((i) => {
    const status = resolveHealth(i.id).status
    if (filterTab === "healthy") return status === "healthy"
    if (filterTab === "problem") return problemStatuses.includes(status)
    return true
  })

  return (
    <div className="space-y-6">
      <PageHeader
        icon={<ActivityIcon size={24} />}
        title={t("pipelineHealthPage.title")}
        actions={
          <div className="flex items-center gap-3">
            {updatedAt && !isLoading && (
              <span className="font-mono text-xs text-text-tertiary">
                {t("pipelineHealthPage.updatedAt", {
                  time: formatDateTime(updatedAt, { hour: "2-digit", minute: "2-digit" }),
                })}
              </span>
            )}
            <Button
              variant="outline"
              leftIcon={<RefreshCwIcon size={14} />}
              onClick={() => void load()}
              disabled={isLoading}
              aria-busy={isLoading}
            >
              {t("common:actions.refresh")}
            </Button>
          </div>
        }
      />

      {error && (
        <Notice variant="danger" title={t("pipelineHealthPage.loadError")}>
          {error}
        </Notice>
      )}

      {isLoading ? (
        <LoadingSpinner size="lg" text={t("pipelineHealthPage.loading")} className="py-20" />
      ) : (
        <>
          {/* Filtro de status: grupo de botões com aria-pressed (não são abas de
              conteúdo distinto, então role=tab/tabpanel seria contrato ARIA incorreto). */}
          <div role="group" aria-label={t("pipelineHealthPage.filters.groupLabel")} className="flex flex-wrap gap-2">
            {([
              { value: "all", label: t("pipelineHealthPage.filters.all", { count: integrations.length }) },
              { value: "healthy", label: t("pipelineHealthPage.filters.healthy", { count: counts.healthy }) },
              { value: "problem", label: t("pipelineHealthPage.filters.problem", { count: problemCount }) },
            ] as const).map((f) => (
              <button
                key={f.value}
                type="button"
                aria-pressed={filterTab === f.value}
                onClick={() => setFilterTab(f.value as FilterTab)}
                className={cn(
                  "rounded-md px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500",
                  filterTab === f.value
                    ? "bg-primary-600 text-text-inverse"
                    : "border border-border bg-surface text-text-secondary hover:bg-surface-hover hover:text-text",
                )}
              >
                {f.label}
              </button>
            ))}
          </div>

          <div className="mt-4">
            {filtered.length === 0 ? (
              <EmptyState
                icon={<ActivityIcon size={32} />}
                title={t("pipelineHealthPage.empty.title")}
                description={t("pipelineHealthPage.empty.description")}
              />
            ) : (
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
                {filtered.map((integration) => (
                  <HealthCard
                    key={integration.id}
                    integration={integration}
                    health={resolveHealth(integration.id)}
                  />
                ))}
              </div>
            )}
          </div>

          {/* ── Seção: Saúde por destino ──────────────────────────────── */}
          <section aria-labelledby="dest-health-heading" className="space-y-3 pt-4 border-t border-border">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <ServerIcon size={15} className="text-stage-route" aria-hidden="true" />
                <h2
                  id="dest-health-heading"
                  className="font-mono text-xs uppercase tracking-[0.1em] text-text-secondary"
                >
                  {t("pipelineHealthPage.destinations.title")}
                </h2>
              </div>
              <button
                type="button"
                aria-expanded={showDestinations}
                aria-controls="dest-health-grid"
                onClick={() => setShowDestinations((v) => !v)}
                className={cn(
                  "rounded-md px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500",
                  showDestinations
                    ? "bg-primary-600 text-text-inverse"
                    : "border border-border bg-surface text-text-secondary hover:bg-surface-hover hover:text-text",
                )}
              >
                {showDestinations ? t("pipelineHealthPage.destinations.hide") : t("pipelineHealthPage.destinations.show")}
              </button>
            </div>

            {showDestinations && (
              <div id="dest-health-grid">
                <DestinationHealthGrid />
              </div>
            )}
          </section>
        </>
      )}
    </div>
  )
}

export default PipelineHealthPage
