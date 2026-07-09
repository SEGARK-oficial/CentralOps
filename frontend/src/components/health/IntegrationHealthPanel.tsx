import type React from "react"
import { useTranslation } from "react-i18next"
import { RefreshCwIcon } from "lucide-react"
import { Button } from "@/components/ui/Button/Button"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Notice } from "@/components/ui/Notice/Notice"
import { useIntegrationHealth } from "@/hooks/useIntegrationHealth"
import { formatRelativeDate } from "@/lib/utils"
import { LastErrorPanel } from "@/components/health/LastErrorPanel"
import { MappedFieldRatioCard } from "@/components/health/MappedFieldRatioCard"
import { MetricsGrid } from "@/components/health/MetricsGrid"
import { StatusCard } from "@/components/health/StatusCard"

interface IntegrationHealthPanelProps {
  integrationId: number
}

export const IntegrationHealthPanel: React.FC<IntegrationHealthPanelProps> = ({ integrationId }) => {
  const { t } = useTranslation("dashboard")
  const { data, isLoading, error, refetch } = useIntegrationHealth(integrationId)

  const cacheAge = data ? Math.floor((Date.now() - new Date(data.cached_at).getTime()) / 1000) : null

  return (
    <div
      className="space-y-4"
      data-testid="integration-health-panel"
    >
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-text">{t("health.integrationPanel.title")}</h2>
          {cacheAge !== null && (
            <p
              className="text-xs text-text-secondary"
              aria-live="polite"
              aria-atomic="true"
            >
              {t("health.integrationPanel.updatedSecondsAgo", { seconds: cacheAge })}
            </p>
          )}
        </div>

        <Button
          variant="outline"
          size="sm"
          leftIcon={<RefreshCwIcon size={14} />}
          onClick={() => refetch()}
          disabled={isLoading}
          aria-busy={isLoading}
          aria-label={t("health.integrationPanel.refreshAriaLabel")}
          data-testid="health-refresh-button"
        >
          {t("health.integrationPanel.refresh")}
        </Button>
      </div>

      {error && (
        <Notice
          variant="danger"
          title={t("health.integrationPanel.loadError")}
          action={
            <Button variant="ghost" size="xs" onClick={() => refetch()}>
              {t("common:actions.retry")}
            </Button>
          }
        >
          {error.message}
        </Notice>
      )}

      {isLoading && !data && (
        <LoadingSpinner size="md" text={t("health.integrationPanel.loading")} className="py-12" />
      )}

      {data && (
        <div className="space-y-4" aria-live="polite" aria-atomic="false">
          <StatusCard status={data.status} />

          <MetricsGrid data={data} />

          <LastErrorPanel lastError={data.last_error} />

          <MappedFieldRatioCard ratio={data.mapped_field_ratio} />

          {data.last_success_at && (
            <p className="text-xs text-text-secondary">
              {t("health.integrationPanel.lastSuccess", { time: formatRelativeDate(data.last_success_at) })}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
