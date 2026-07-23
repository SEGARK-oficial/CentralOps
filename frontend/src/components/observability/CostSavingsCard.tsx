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

/** Base 1000, deliberadamente. Estes bytes são a BASE DE CUSTO e o pricer EE
 *  fatura por GB decimal (`bytes_out / 1e9`, routers/collectors.py). Formatar em
 *  base 1024 rotulado "MB/GB" fazia o card divergir ~7% (GB) do valor cobrado. */
function fmtBytes(n: number): string {
  if (!n) return "0 B"
  const u = ["B", "kB", "MB", "GB", "TB", "PB"]
  const i = Math.min(u.length - 1, Math.floor(Math.log10(n) / 3))
  return `${(n / Math.pow(1000, i)).toFixed(i ? 1 : 0)} ${u[i]}`
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
  // Redução: com uma única org, RENDERIZA o valor do backend em vez de
  // recalcular — a fórmula vivia duplicada aqui e divergia em multi-org. Com
  // várias orgs a agregação é inevitável no cliente (o backend responde por
  // linha), e aí reusamos a MESMA fórmula, denominador contrafactual incluído.
  const base = totals.bytes_out + totals.bytes_saved
  const reductionPct =
    data.rows.length === 1 && data.rows[0].reduction_pct !== null
      ? data.rows[0].reduction_pct * 100
      : base > 0
        ? (totals.bytes_saved / base) * 100
        : 0
  const currency = data.rows.find((r) => r.cost)?.cost?.currency ?? "USD"

  // Funil impossível (Evitado > Coletado): bases de medição diferentes somadas.
  // Sinalizar é obrigatório — exibir em silêncio é o que gerou o chamado.
  const unitMismatch = data.rows.some((r) => r.unit_mismatch)

  // Decomposição por causa, agregada entre as orgs acessíveis.
  const byReason = Object.entries(
    data.rows.reduce<Record<string, number>>((acc, r) => {
      for (const [reason, n] of Object.entries(r.bytes_saved_by_reason ?? {})) {
        acc[reason] = (acc[reason] ?? 0) + n
      }
      return acc
    }, {}),
  ).sort((a, b) => b[1] - a[1])

  // Pricer EE registrado mas sem preço configurado devolve savings nulo em TODAS
  // as linhas. Exibir "US$ 0,00" nesse caso é indistinguível de "não economizou
  // nada" — o operador não tem como saber que falta preencher cost_per_gb.
  const pricingUnconfigured =
    data.pricing_available &&
    totals.bytes_saved > 0 &&
    data.rows.every((r) => r.savings_usd_per_day === null || r.savings_usd_per_day === 0)

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
        <Metric
          label={t("observability.costSavings.collected")}
          value={fmtBytes(totals.bytes_in)}
          sublabel={t("observability.costSavings.unitRaw")}
        />
        <Metric
          label={t("observability.costSavings.delivered")}
          value={fmtBytes(totals.bytes_out)}
          sublabel={t("observability.costSavings.unitEnvelope")}
        />
        <Metric
          label={t("observability.costSavings.avoided")}
          value={fmtBytes(totals.bytes_saved)}
          hint={<ScissorsIcon size={13} className="text-success-600" />}
          sublabel={t("observability.costSavings.unitMixed")}
        />
        <Metric
          label={t("observability.costSavings.reduction")}
          value={`${reductionPct.toFixed(1)}%`}
          accent="text-success-600"
          sublabel={t("observability.costSavings.reductionBasis")}
        />
      </div>

      {byReason.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5" data-testid="savings-by-reason">
          <span className="text-xs text-text-tertiary">{t("observability.costSavings.byReason")}</span>
          {byReason.map(([reason, n]) => (
            <Badge key={reason} variant="default" size="sm">
              {t(`observability.costSavings.reasons.${reason}`, { defaultValue: reason })} {fmtBytes(n)}
            </Badge>
          ))}
        </div>
      )}

      {unitMismatch && (
        <p className="text-xs text-warning-600" data-testid="unit-mismatch-notice">
          {t("observability.costSavings.unitMismatch")}
        </p>
      )}

      {!data.pricing_available ? (
        <p className="text-xs text-text-tertiary">
          {t("observability.costSavings.enablePricing")}
        </p>
      ) : pricingUnconfigured ? (
        <p className="text-xs text-text-tertiary" data-testid="pricing-unconfigured">
          {t("observability.costSavings.pricingUnconfigured")}
        </p>
      ) : (
        <div className="flex items-center gap-2 rounded-md border border-border bg-surface-tertiary px-3 py-2">
          <CoinsIcon size={15} className="text-success-600" />
          <span className="text-xs text-text-secondary">{t("observability.costSavings.estimatedSavings")}</span>
          <span className="ml-auto text-sm font-semibold text-success-600">
            {totals.savings.toLocaleString(currentLocale(), { style: "currency", currency })} {t("observability.costSavings.perDay")}
          </span>
        </div>
      )}
    </Card>
  )
}

function Metric({
  label,
  value,
  hint,
  accent,
  sublabel,
}: {
  label: string
  value: string
  hint?: React.ReactNode
  accent?: string
  /** Base de medição. Sem isto os quatro números parecem um balanço, e não são. */
  sublabel?: string
}) {
  return (
    <div>
      <div className="flex items-center gap-1 text-xs text-text-tertiary">
        {label} {hint}
      </div>
      <div className={`text-lg font-semibold ${accent ?? "text-text"}`}>{value}</div>
      {sublabel && <div className="text-[11px] leading-tight text-text-tertiary">{sublabel}</div>}
    </div>
  )
}
