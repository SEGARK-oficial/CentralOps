/**
 * DestinationHealthGrid — dimensão por destino no PipelineHealthPage.
 *
 * Lista cada destino da org com sua saúde:
 *   status (colorblind-safe via StatusBadge/severity),
 *   eps, bytes_per_min, dlq_24h, breaker_state.
 *
 * Carrega listDestinations + getDestinationHealth em paralelo.
 * Exibe Skeleton durante carregamento; Notice em caso de erro.
 */

import type React from "react"
import { useCallback, useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { RefreshCwIcon, ServerIcon } from "lucide-react"
import * as api from "@/services/api"
import type { Destination, DestinationHealth } from "@/types"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { Notice } from "@/components/ui/Notice/Notice"
import { SkeletonCard } from "@/components/ui/Skeleton"
import { fmtRate } from "@/lib/fmt"
import { healthEncoding, StatusBadge } from "@/lib/severity"

// ── mapeamento de DestinationHealthStatus → HealthStatus (severity.ts) ─────────

function destinationStatusToHealth(
  status: string | undefined | null,
): "healthy" | "degraded" | "down" | "unknown" {
  switch (status) {
    case "healthy":  return "healthy"
    case "degraded": return "degraded"
    case "unhealthy":
    case "disabled": return "down"
    default:         return "unknown"
  }
}

// ── BreakerBadge ──────────────────────────────────────────────────────────────

const BREAKER_LABEL_KEY: Record<string, string> = {
  closed: "health.destinationGrid.breaker.closed",
  open: "health.destinationGrid.breaker.open",
  half_open: "health.destinationGrid.breaker.halfOpen",
}

const BreakerBadge: React.FC<{ state: string | null }> = ({ state }) => {
  const { t } = useTranslation("dashboard")
  if (!state) return null
  const label = BREAKER_LABEL_KEY[state] ? t(BREAKER_LABEL_KEY[state]) : state
  const variant = state === "closed" ? "success" : state === "open" ? "danger" : "warning"
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium
        ${variant === "success" ? "bg-success-50 text-success-700" : ""}
        ${variant === "danger" ? "bg-danger-50 text-danger-700" : ""}
        ${variant === "warning" ? "bg-warning-50 text-warning-700" : ""}
      `}
      aria-label={t("health.destinationGrid.breaker.ariaLabel", { label })}
    >
      {label}
    </span>
  )
}

// ── DestinationCard ───────────────────────────────────────────────────────────

interface DestinationCardProps {
  destination: Destination
  health: DestinationHealth | null
}

const DestinationCard: React.FC<DestinationCardProps> = ({ destination, health }) => {
  const { t } = useTranslation("dashboard")
  const healthStatus = destinationStatusToHealth(health?.status)
  const encoding = healthEncoding(healthStatus)

  return (
    <Card
      padding="md"
      className="shadow-sm flex flex-col gap-3"
      data-testid={`destination-card-${destination.id}`}
      role="article"
      aria-label={t("health.destinationGrid.destinationAriaLabel", { name: destination.name })}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex items-center gap-2">
          <ServerIcon size={16} className="shrink-0 text-text-secondary" aria-hidden="true" />
          <div className="min-w-0">
            <h3 className="truncate font-semibold text-text">{destination.name}</h3>
            <p className="text-xs text-text-tertiary">{destination.kind}</p>
          </div>
        </div>
        <StatusBadge encoding={encoding} iconSize={13} />
      </div>

      <dl className="grid grid-cols-2 gap-1 text-xs" aria-label={t("health.destinationGrid.metricsAriaLabel")}>
        <dt className="text-text-secondary">{t("health.destinationGrid.eps")}</dt>
        <dd className="font-medium text-text" data-testid={`dest-eps-${destination.id}`}>
          {health?.eps !== null && health?.eps !== undefined
            ? fmtRate(health.eps)
            : "—"}
        </dd>

        <dt className="text-text-secondary">{t("health.destinationGrid.bytesPerMinute")}</dt>
        <dd className="font-medium text-text">
          {health?.bytes_per_min !== null && health?.bytes_per_min !== undefined
            ? fmtRate(health.bytes_per_min)
            : "—"}
        </dd>

        <dt className="text-text-secondary">{t("health.destinationGrid.dlq24h")}</dt>
        <dd className="font-medium text-text">
          {health ? String(health.dlq_24h) : "—"}
        </dd>

        <dt className="text-text-secondary">{t("health.destinationGrid.circuitBreaker")}</dt>
        <dd>
          <BreakerBadge state={health?.breaker_state ?? null} />
          {!health?.breaker_state && <span className="font-medium text-text">—</span>}
        </dd>
      </dl>

      {!destination.enabled && (
        <p className="text-xs text-text-secondary italic">{t("health.destinationGrid.disabled")}</p>
      )}
    </Card>
  )
}

// ── DestinationHealthGrid ──────────────────────────────────────────────────────

interface DestinationHealthGridState {
  status: "idle" | "loading" | "ready" | "error"
  error: string | null
  destinations: Destination[]
  healthMap: Map<string, DestinationHealth>
}

export const DestinationHealthGrid: React.FC = () => {
  const { t } = useTranslation("dashboard")
  const [state, setState] = useState<DestinationHealthGridState>({
    status: "idle",
    error: null,
    destinations: [],
    healthMap: new Map(),
  })

  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, status: "loading", error: null }))
    try {
      const destinations = await api.listDestinations({ include_disabled: true })

      const healthResults = await Promise.allSettled(
        destinations.map((d) => api.getDestinationHealth(d.id)),
      )

      const map = new Map<string, DestinationHealth>()
      for (let i = 0; i < healthResults.length; i++) {
        const result = healthResults[i]
        if (result.status === "fulfilled") {
          map.set(destinations[i].id, result.value)
        }
      }

      setState({
        status: "ready",
        error: null,
        destinations,
        healthMap: map,
      })
    } catch (err) {
      setState((prev) => ({
        ...prev,
        status: "error",
        error: err instanceof Error ? err.message : t("health.destinationGrid.loadError"),
      }))
    }
  }, [t])

  useEffect(() => {
    void load()
  }, [load])

  const { status, error, destinations, healthMap } = state

  if (status === "loading") {
    return (
      <div
        role="status"
        aria-label={t("health.destinationGrid.loadingAriaLabel")}
        className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4"
      >
        {Array.from({ length: 4 }).map((_, i) => (
          <SkeletonCard key={i} lines={3} />
        ))}
      </div>
    )
  }

  if (status === "error") {
    return (
      <Notice
        variant="danger"
        title={t("health.destinationGrid.loadError")}
        action={
          <Button
            variant="outline"
            size="sm"
            leftIcon={<RefreshCwIcon size={14} />}
            onClick={() => void load()}
          >
            {t("common:actions.retry")}
          </Button>
        }
      >
        {error}
      </Notice>
    )
  }

  if (status === "ready" && destinations.length === 0) {
    return (
      <div
        className="py-10 text-center text-text-secondary"
        data-testid="destinations-grid-empty"
      >
        <ServerIcon size={28} className="mx-auto mb-2 text-text-tertiary" aria-hidden="true" />
        <p className="text-sm">{t("health.destinationGrid.empty")}</p>
      </div>
    )
  }

  if (status !== "ready") return null

  return (
    <div
      className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4"
      data-testid="destination-health-grid"
      aria-label={t("health.destinationGrid.ariaLabel")}
    >
      {destinations.map((dest) => (
        <DestinationCard
          key={dest.id}
          destination={dest}
          health={healthMap.get(dest.id) ?? null}
        />
      ))}
    </div>
  )
}

export default DestinationHealthGrid
