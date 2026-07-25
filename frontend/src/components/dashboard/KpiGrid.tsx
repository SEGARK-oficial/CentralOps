import type React from "react"
import { useTranslation } from "react-i18next"
import { ArrowDownIcon, ArrowRightIcon, ArrowUpIcon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Card } from "@/components/ui/Card/Card"
import { iconFor } from "@/lib/icons"
import { cn } from "@/lib/utils"
import type { DashSeverity, KpiCard } from "@/types"

/**
 * A matiz do card diz em QUE PONTO do pipeline o número está, e nada mais.
 * Os ids vêm do payload v2 (`routers/dashboard.py`); id desconhecido cai em
 * neutro, que é o comportamento certo — inventar matiz mentiria sobre o estágio.
 */
type Stage = "collect" | "normalize" | "reduce" | "route" | "detect"

const STAGE_BY_KPI: Record<string, Stage> = {
  ingest_eps: "collect",
  active_sources: "collect",
  mapping_coverage: "normalize",
  quarantine_rate: "detect",
  routed_events: "route",
  destinations_healthy: "route",
}

// Classes literais: o Tailwind varre o fonte, então nada de string montada.
const STAGE_RULE: Record<Stage, string> = {
  collect: "border-l-stage-collect",
  normalize: "border-l-stage-normalize",
  reduce: "border-l-stage-reduce",
  route: "border-l-stage-route",
  detect: "border-l-stage-detect",
}

const STAGE_ICON: Record<Stage, string> = {
  collect: "text-stage-collect",
  normalize: "text-stage-normalize",
  reduce: "text-stage-reduce",
  route: "text-stage-route",
  detect: "text-stage-detect",
}

// ── Trend chip ────────────────────────────────────────────────────────────────

interface TrendChipProps {
  trend: "up" | "down" | "flat"
  trendValue?: string | null
}

/**
 * Sem matiz: subir não é bom nem ruim sem saber de qual número se fala
 * (ingestão subindo é saúde; quarentena subindo é problema). Quem diz se
 * precisa de você é a severidade, não a seta.
 */
const TrendChip: React.FC<TrendChipProps> = ({ trend, trendValue }) => {
  const { t } = useTranslation("dashboard")
  const Icon = trend === "up" ? ArrowUpIcon : trend === "down" ? ArrowDownIcon : ArrowRightIcon

  return (
    <span
      className="inline-flex shrink-0 items-center gap-0.5 pb-0.5 font-mono text-xs text-text-tertiary"
      aria-label={t("dashboardPage.kpi.trendAriaLabel", { trend, value: trendValue ?? "" })}
      title={trendValue ?? undefined}
    >
      <Icon size={12} aria-hidden="true" />
      {/* A visibilidade segue a LARGURA DO CARD, não a da janela. Em xl o grid
          abre 6 colunas e o card fica MAIS ESTREITO do que a 2 colunas no
          celular: ali o número manda, e a seta sozinha basta. O valor continua
          no aria-label e no title. */}
      {trendValue && <span className="hidden sm:inline xl:hidden">{trendValue}</span>}
    </span>
  )
}

// ── Single KPI card ───────────────────────────────────────────────────────────

const SEVERITY_LABEL_KEY: Partial<Record<DashSeverity, string>> = {
  warn: "dashboardPage.kpi.severity.warn",
  critical: "dashboardPage.kpi.severity.critical",
}

interface KpiCardItemProps {
  kpi: KpiCard
}

const KpiCardItem: React.FC<KpiCardItemProps> = ({ kpi }) => {
  const { t } = useTranslation("dashboard")
  const Icon = iconFor(kpi.icon_id)
  const stage = STAGE_BY_KPI[kpi.id]
  const severityKey = kpi.severity ? SEVERITY_LABEL_KEY[kpi.severity] : undefined

  return (
    <Card
      padding="sm"
      className={cn("flex flex-col gap-3", stage && `border-l-[3px] ${STAGE_RULE[stage]}`)}
      aria-label={t("dashboardPage.kpi.cardAriaLabel", { label: kpi.label, value: kpi.value })}
    >
      <div className="flex items-start justify-between gap-2">
        {/* Duas linhas fixas: rótulo de uma linha e de duas param o número na
            mesma altura, e a faixa inteira lê como uma régua só. */}
        <span className="line-clamp-2 min-h-[2.2em] font-mono text-[11px] uppercase leading-tight tracking-[0.1em] text-text-tertiary">
          {kpi.label}
        </span>
        <Icon
          size={15}
          className={cn("mt-0.5 shrink-0", stage ? STAGE_ICON[stage] : "text-text-tertiary")}
          aria-hidden="true"
        />
      </div>

      {/* min-w-0 é o que segura a linha: sem ele o número em font-display não
          encolhe e empurra o TrendChip para FORA do card (medido: 264px de
          conteúdo numa caixa de 128px, chip 123px fora da borda). */}
      <div className="flex min-w-0 items-end gap-2">
        <span
          className={cn(
            // xl abre 6 colunas: o card cai para ~128px de conteúdo e 30px de
            // corpo não cabem em 9 dígitos. O número desce um degrau ali.
            "min-w-0 truncate font-display text-3xl leading-none xl:text-2xl",
            kpi.severity === "critical" ? "text-danger-700" : "text-text",
          )}
          title={String(kpi.value)}
        >
          {kpi.value}
        </span>
        {kpi.trend && kpi.trend !== "flat" && (
          <TrendChip trend={kpi.trend} trendValue={kpi.trend_value} />
        )}
      </div>

      {/* Nada aqui quando está tudo em ordem: a varredura procura o que NÃO é neutro. */}
      {(kpi.sub || severityKey) && (
        <div className="flex items-center justify-between gap-2">
          {kpi.sub && (
            <span className="truncate font-mono text-[11px] text-text-tertiary">{kpi.sub}</span>
          )}
          {severityKey && (
            <Badge
              variant={kpi.severity === "critical" ? "danger" : "warning"}
              size="sm"
              className="ml-auto"
            >
              {t(severityKey)}
            </Badge>
          )}
        </div>
      )}
    </Card>
  )
}

// ── KpiGrid ───────────────────────────────────────────────────────────────────

interface KpiGridProps {
  kpis: KpiCard[]
}

export const KpiGrid: React.FC<KpiGridProps> = ({ kpis }) => {
  const { t } = useTranslation("dashboard")
  if (kpis.length === 0) return null

  return (
    <div
      className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6"
      role="region"
      aria-label={t("dashboardPage.kpi.gridAriaLabel")}
    >
      {kpis.map((kpi) => (
        <KpiCardItem key={kpi.id} kpi={kpi} />
      ))}
    </div>
  )
}
