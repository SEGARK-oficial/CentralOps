/**
 * CostSavingsCard — volume ingerido vs entregue + economia das
 * alavancas de redução (trim/sample/suppress/aggregate), por org na janela de metering.
 *
 * Community mostra volume + % de redução (adimensional). O bloco US$ (savings/dia) só
 * aparece quando o pacote Enterprise registra um cost pricer — sem ele,
 * o card é honesto e omite o $. Auto-oculta quando o metering está desligado.
 */
import type React from "react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { TrendingDownIcon, CoinsIcon, ScissorsIcon } from "lucide-react"
import * as api from "@/services/api"
import type { CostSummary } from "@/services/api"
import { Card } from "@/components/ui/Card/Card"
import { Badge } from "@/components/ui/Badge/Badge"
import { currentLocale } from "@/lib/intl"

function fmtBytes(n: number): string {
  if (!n) return "0 B"
  const u = ["B", "KB", "MB", "GB", "TB", "PB"]
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)))
  return `${(n / Math.pow(1024, i)).toFixed(i ? 1 : 0)} ${u[i]}`
}

export const CostSavingsCard: React.FC = () => {
  const { t } = useTranslation("dashboard")
  const [data, setData] = useState<CostSummary | null>(null)
  const [hidden, setHidden] = useState(false)

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const s = await api.getCostSummary()
        if (cancelled) return
        // Metering desligado, ou sem dado na janela → não polui a página.
        if (!s || !s.enabled || s.rows.length === 0) setHidden(true)
        else setData(s)
      } catch {
        if (!cancelled) setHidden(true)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  if (hidden || !data) return null

  // Agrega as orgs acessíveis (o backend já fez o escopo por-tenant).
  const totals = data.rows.reduce(
    (a, r) => ({
      bytes_in: a.bytes_in + r.bytes_in,
      bytes_out: a.bytes_out + r.bytes_out,
      bytes_saved: a.bytes_saved + r.bytes_saved,
      savings: a.savings + (r.savings_usd_per_day ?? 0),
    }),
    { bytes_in: 0, bytes_out: 0, bytes_saved: 0, savings: 0 },
  )
  const base = totals.bytes_out + totals.bytes_saved
  const reductionPct = base > 0 ? (totals.bytes_saved / base) * 100 : 0
  const currency = data.rows.find((r) => r.cost)?.cost?.currency ?? "USD"

  return (
    <Card className="space-y-4">
      <div className="flex items-center gap-2">
        <TrendingDownIcon size={16} className="text-primary-600" />
        <h3 className="text-sm font-semibold text-text">{t("observability.costSavings.title")}</h3>
        <span className="text-xs text-text-tertiary">{t("observability.costSavings.windowMinutes", { minutes: data.window_minutes })}</span>
        {!data.pricing_available && (
          <Badge variant="default" size="sm" className="ml-auto">{t("observability.costSavings.enterpriseBadge")}</Badge>
        )}
      </div>

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Metric label={t("observability.costSavings.collected")} value={fmtBytes(totals.bytes_in)} />
        <Metric label={t("observability.costSavings.delivered")} value={fmtBytes(totals.bytes_out)} />
        <Metric
          label={t("observability.costSavings.avoided")}
          value={fmtBytes(totals.bytes_saved)}
          hint={<ScissorsIcon size={13} className="text-success-600" />}
        />
        <Metric
          label={t("observability.costSavings.reduction")}
          value={`${reductionPct.toFixed(1)}%`}
          accent="text-success-600"
        />
      </div>

      {data.pricing_available ? (
        <div className="flex items-center gap-2 rounded-md border border-border bg-surface-tertiary px-3 py-2">
          <CoinsIcon size={15} className="text-success-600" />
          <span className="text-xs text-text-secondary">{t("observability.costSavings.estimatedSavings")}</span>
          <span className="ml-auto text-sm font-semibold text-success-600">
            {totals.savings.toLocaleString(currentLocale(), { style: "currency", currency })} {t("observability.costSavings.perDay")}
          </span>
        </div>
      ) : (
        <p className="text-xs text-text-tertiary">
          {t("observability.costSavings.enablePricing")}
        </p>
      )}
    </Card>
  )
}

function Metric({
  label,
  value,
  hint,
  accent,
}: {
  label: string
  value: string
  hint?: React.ReactNode
  accent?: string
}) {
  return (
    <div>
      <div className="flex items-center gap-1 text-xs text-text-tertiary">
        {label} {hint}
      </div>
      <div className={`text-lg font-semibold ${accent ?? "text-text"}`}>{value}</div>
    </div>
  )
}
