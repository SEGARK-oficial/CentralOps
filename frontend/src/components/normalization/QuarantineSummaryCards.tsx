/**
 * QuarantineSummaryCards
 * Contadores por error_kind (top 4) + total.
 *
 * O backend só devolve o `total` filtrado e a página atual de `items`
 * (não um agregado por error_kind). Quando o total excede a página, a
 * contagem por kind é uma ESTIMATIVA proporcional extrapolada ao total
 * filtrado — sinalizada com "aprox." e tooltip — em vez de refletir só
 * a página corrente (que subestimaria a fila).
 */

import type React from "react"
import { useTranslation } from "react-i18next"
import { AlertOctagonIcon, PackageXIcon } from "lucide-react"
import { Card, CardContent } from "@/components/ui/Card/Card"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { formatNumber } from "@/lib/intl"
import type { QuarantineEntry } from "@/types"

interface QuarantineSummaryCardsProps {
  items: QuarantineEntry[]
  total: number
  isLoading: boolean
}

export const QuarantineSummaryCards: React.FC<QuarantineSummaryCardsProps> = ({
  items,
  total,
  isLoading,
}) => {
  const { t } = useTranslation("drift")
  // Conta por error_kind na página atual (amostra).
  const kindCounts = items.reduce<Record<string, number>>((acc, item) => {
    acc[item.error_kind] = (acc[item.error_kind] ?? 0) + 1
    return acc
  }, {})

  // Quando a página é uma amostra do total filtrado, extrapola cada
  // contagem proporcionalmente ao total real do backend.
  const sampleSize = items.length
  const isSample = sampleSize > 0 && total > sampleSize

  const top4 = Object.entries(kindCounts)
    .sort(([, a], [, b]) => b - a)
    .slice(0, 4)
    .map(([kind, sampleCount]) => {
      const estimated = isSample
        ? Math.round((sampleCount / sampleSize) * total)
        : sampleCount
      return { kind, count: estimated }
    })

  return (
    <div className="flex flex-wrap gap-4">
      <Card className="flex-1 min-w-[140px]">
        <CardContent>
          <div className="flex items-center justify-between gap-2">
            <div className="flex flex-col gap-1">
              <span className="text-xs font-medium text-text-secondary uppercase tracking-wide">{t("quarantine.summary.total")}</span>
              {isLoading ? (
                <LoadingSpinner size="sm" />
              ) : (
                <span className="text-2xl font-bold text-danger-600">{formatNumber(total)}</span>
              )}
            </div>
            <PackageXIcon size={28} className="text-danger-600 shrink-0" aria-hidden="true" />
          </div>
        </CardContent>
      </Card>

      {top4.map(({ kind, count }) => (
        <Card key={kind} className="flex-1 min-w-[140px]">
          <CardContent>
            <div className="flex items-center justify-between gap-2">
              <div className="flex flex-col gap-1">
                <span
                  className="text-xs font-medium text-text-secondary uppercase tracking-wide truncate max-w-[110px]"
                  title={kind}
                >
                  {kind}
                </span>
                {isLoading ? (
                  <LoadingSpinner size="sm" />
                ) : (
                  <span
                    className="text-2xl font-bold text-warning-600"
                    title={
                      isSample
                        ? t("quarantine.summary.estimateTooltip", { total: formatNumber(total) })
                        : undefined
                    }
                  >
                    {isSample ? "~" : ""}
                    {formatNumber(count)}
                  </span>
                )}
              </div>
              <AlertOctagonIcon size={28} className="text-warning-600 shrink-0" aria-hidden="true" />
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  )
}

export default QuarantineSummaryCards
