import type React from "react"
import { Link } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { Badge } from "@/components/ui/Badge/Badge"
import { Card } from "@/components/ui/Card/Card"
import type { IntegrationPipelineHealth } from "@/types"

type TFn = (key: string, opts?: Record<string, unknown>) => string

/**
 * Exportada porque a grade densa da /pipeline-health precisa formatar os MESMOS
 * dois atrasos com a mesma régua — "há 15h" tem de ler igual nos dois lugares.
 * As chaves vivem no namespace `dashboard`, usado por ambos.
 */
export function formatLag(seconds: number | null | undefined, t: TFn): string {
  // `== null` cobre `undefined` também: durante um rolling upgrade a API antiga
  // responde SEM o campo, e um `=== null` deixaria passar direto para o
  // `Math.floor(undefined / 60)` — a tela mostrava "há NaN dias". Idem para
  // qualquer não-finito que escape de um JSON malformado.
  if (seconds == null || !Number.isFinite(seconds)) return "—"
  if (seconds < 60) return t("health.metricsGrid.lag.secondsAgo", { count: seconds })
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return t("health.metricsGrid.lag.minutesAgo", { count: minutes })
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return t("health.metricsGrid.lag.hoursAgo", { count: hours })
  const days = Math.floor(hours / 24)
  return t("health.metricsGrid.lag.daysAgo", { count: days })
}

interface MetricCardProps {
  label: string
  value: React.ReactNode
  testId: string
  /** Uma linha dizendo o que o número mede — é o que separa "quando rodou" de "de quando é o dado". */
  hint?: string
  footer?: React.ReactNode
}

const MetricCard: React.FC<MetricCardProps> = ({ label, value, testId, hint, footer }) => (
  <Card padding="sm" className="shadow-sm" data-testid={testId}>
    <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{label}</div>
    <div className="mt-2 text-lg font-semibold text-text">{value}</div>
    {hint && <div className="mt-1 text-xs text-text-secondary">{hint}</div>}
    {footer && <div className="mt-1.5">{footer}</div>}
  </Card>
)

interface MetricsGridProps {
  data: IntegrationPipelineHealth
}

export const MetricsGrid: React.FC<MetricsGridProps> = ({ data }) => {
  const { t } = useTranslation("dashboard")
  const driftLink = `/drift?vendor=${encodeURIComponent(String(data.integration_id))}`
  const quarantineLink = `/quarantine?integration_id=${data.integration_id}`

  return (
    <div
      className="grid grid-cols-2 gap-3"
      role="region"
      aria-label={t("health.metricsGrid.ariaLabel")}
    >
      <MetricCard
        label={t("health.metricsGrid.eventsPerMinute")}
        value={data.events_per_minute !== null ? String(data.events_per_minute) : "—"}
        testId="metrics-events-per-minute"
      />

      {/* QUANDO O COLETOR RODOU. Sozinho, este número mentiu por semanas: ele é
          reescrito a cada ciclo que termina sem erro, mesmo quando o ciclo estava
          processando o dia anterior. O rótulo agora diz o que ele mede. */}
      <MetricCard
        label={t("health.metricsGrid.lastCollection")}
        value={
          <span aria-label={t("health.metricsGrid.lastCollectionAriaLabel", { value: formatLag(data.lag_seconds, t) })}>
            {formatLag(data.lag_seconds, t)}
          </span>
        }
        hint={t("health.metricsGrid.lastCollectionHint")}
        testId="metrics-lag"
      />

      {/* DE QUANDO É O DADO. Omitido quando não medível (cursor não temporal,
          nenhum watermark gravado, ou API antiga que nem manda o campo):
          renderizar "0" ou "—" no lugar afirmaria "em dia", que é exatamente a
          mentira que este indicador existe para desfazer. */}
      {data.watermark_lag_seconds != null && (
        <MetricCard
          label={t("health.metricsGrid.dataLag")}
          value={
            <span aria-label={t("health.metricsGrid.dataLagAriaLabel", { value: formatLag(data.watermark_lag_seconds, t) })}>
              {formatLag(data.watermark_lag_seconds, t)}
            </span>
          }
          hint={t("health.metricsGrid.dataLagHint")}
          testId="metrics-watermark-lag"
          footer={
            data.backlog_detected ? (
              <Badge variant="warning" size="sm" data-testid="metrics-backlog-badge">
                {t("health.metricsGrid.backlogBadge")}
              </Badge>
            ) : undefined
          }
        />
      )}

      <MetricCard
        label={t("health.metricsGrid.drift24h")}
        value={String(data.drift_count_24h)}
        testId="metrics-drift-24h"
        footer={
          <Link
            to={driftLink}
            className="text-xs text-primary-600 underline hover:text-primary-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
          >
            {t("health.metricsGrid.viewInDriftExplorer")}
          </Link>
        }
      />

      <MetricCard
        label={t("health.metricsGrid.quarantine24h")}
        value={String(data.quarantine_count_24h)}
        testId="metrics-quarantine-24h"
        footer={
          <Link
            to={quarantineLink}
            className="text-xs text-primary-600 underline hover:text-primary-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
          >
            {t("health.metricsGrid.viewInQuarantine")}
          </Link>
        }
      />

      {/* Backlog sem watermark medível ainda precisa aparecer: é o único sinal de
          que o teto por ciclo foi atingido e sobrou trabalho. */}
      {data.watermark_lag_seconds == null && data.backlog_detected && (
        <MetricCard
          label={t("health.metricsGrid.backlogBadge")}
          value={t("health.metricsGrid.backlogOnly")}
          hint={t("health.metricsGrid.backlogHint")}
          testId="metrics-backlog-only"
        />
      )}
    </div>
  )
}
