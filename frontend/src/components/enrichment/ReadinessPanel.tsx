import type React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { useNavigate } from "react-router-dom"
import {
  AlertTriangleIcon,
  CheckIcon,
  MinusIcon,
  RefreshCcwIcon,
  XIcon,
} from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { ErrorState } from "@/components/ui/ErrorState"
import { Notice } from "@/components/ui/Notice/Notice"
import { Select } from "@/components/ui/Select/Select"
import { SkeletonCard } from "@/components/ui/Skeleton"
import * as api from "@/services/api"
import type {
  EnrichmentActivityEntry,
  EnrichmentReadiness,
  EnrichmentReadinessStep,
  EnrichmentRuleMetrics,
} from "@/services/api"

/**
 * "Está funcionando aqui, e o que falta?" — a primeira aba do enriquecimento.
 *
 * A tela anterior tinha cinco abas espelhando cinco tabelas do banco (catálogo,
 * fontes, tabelas, políticas, execução) e nenhuma delas respondia à pergunta
 * que o operador de fato faz. O chamado nº 1 desta feature é "liguei e não faz
 * nada", e respondê-lo exigia abrir Execução, depois Fontes, depois conferir se
 * a tabela citada tinha versão publicada — cruzando informação de três lugares.
 *
 * O painel inverte isso: o backend avalia os pré-requisitos e devolve o passo
 * que falta, com o motivo e para onde ir. Duas decisões que vêm do domínio:
 *
 * - **Silêncio quando não se aplica.** Um passo só vira alerta se o problema
 *   afeta ESTA organização. Uma org que só usa tabela do cliente não é avisada
 *   sobre o cache externo estar ausente — avisá-la treinaria o operador a
 *   ignorar avisos, e aí o aviso verdadeiro morre junto.
 * - **A ação diz de quem é.** Passos que só um administrador global resolve vêm
 *   marcados; mandar um admin de organização para uma tela que ele não abre é
 *   pior do que não sugerir nada.
 */

interface Props {
  organizations?: Array<{ id: number; name: string }>
  selectedOrgId?: number | null
  /** Leva o operador à aba certa desta mesma página. */
  onNavigateTab?: (tab: string) => void
}

const RANGE_MINUTES = 60

function pct(n: number, total: number): string {
  if (total <= 0) return "0%"
  return `${Math.round((n / total) * 100)}%`
}

/** Ícone e cor por status. Forma além da cor: daltonismo é comum em SOC. */
function StepIcon({ status }: { status: EnrichmentReadinessStep["status"] }) {
  const base = "flex h-6 w-6 shrink-0 items-center justify-center rounded-full"
  if (status === "ok") {
    return (
      <span className={`${base} bg-success-500/15 text-success-500`}>
        <CheckIcon size={13} aria-hidden />
      </span>
    )
  }
  if (status === "blocked") {
    return (
      <span className={`${base} bg-danger-500/15 text-danger-500`}>
        <XIcon size={13} aria-hidden />
      </span>
    )
  }
  if (status === "warning") {
    return (
      <span className={`${base} bg-warning-500/15 text-warning-500`}>
        <AlertTriangleIcon size={13} aria-hidden />
      </span>
    )
  }
  return (
    <span className={`${base} bg-surface-tertiary text-text-tertiary`}>
      <MinusIcon size={13} aria-hidden />
    </span>
  )
}

export const ReadinessPanel: React.FC<Props> = ({
  organizations = [],
  selectedOrgId = null,
  onNavigateTab,
}) => {
  const { t } = useTranslation("enrichment")
  const navigate = useNavigate()

  const [orgId, setOrgId] = useState<number | null>(
    selectedOrgId ?? organizations[0]?.id ?? null,
  )
  // As organizações chegam por fetch, então podem estar vazias na montagem.
  // Mesmo cuidado do ExecutionPanel: sem reconciliar, um admin global ficava
  // com `orgId` nulo para sempre e tomava 422 permanente.
  useEffect(() => {
    if (orgId != null) return
    const fallback = selectedOrgId ?? organizations[0]?.id ?? null
    if (fallback != null) setOrgId(fallback)
  }, [orgId, selectedOrgId, organizations])

  const [readiness, setReadiness] = useState<EnrichmentReadiness | null>(null)
  const [metrics, setMetrics] = useState<EnrichmentRuleMetrics[]>([])
  const [entries, setEntries] = useState<EnrichmentActivityEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const orgParam = orgId != null ? { organization_id: orgId } : {}
      // A prontidão é o que a tela promete; métricas e falhas são complemento.
      // Por isso só ela derruba a tela — as outras duas degradam para vazio,
      // porque o painel ainda responde a pergunta principal sem elas.
      const r = await api.getEnrichmentReadiness(orgParam)
      setReadiness(r)
      const [m, a] = await Promise.all([
        api
          .getEnrichmentMetrics({ ...orgParam, range_minutes: RANGE_MINUTES })
          .catch(() => null),
        api
          .getEnrichmentActivity({ ...orgParam, limit: 20, only_failures: true })
          .catch(() => null),
      ])
      setMetrics(m?.rules ?? [])
      setEntries(a?.entries ?? [])
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [orgId])

  useEffect(() => {
    void load()
  }, [load])

  const totals = useMemo(() => {
    let hit = 0
    let total = 0
    let unanswered = 0
    for (const m of metrics) {
      const t0 = m.hit + m.miss + m.skipped + m.error
      hit += m.hit
      total += t0
      unanswered += m.skipped
    }
    return { hit, total, unanswered }
  }, [metrics])

  function handleAction(step: EnrichmentReadinessStep) {
    const action = step.action
    if (!action) return
    // Rota de outra página vai pelo router; aba desta página é troca local, sem
    // recarregar o que já está em memória.
    if (action.route.startsWith("/enrichment?tab=")) {
      onNavigateTab?.(action.route.split("=")[1])
      return
    }
    navigate(action.route)
  }

  if (error) {
    return <ErrorState title={t("errorTitle")} message={error} onRetry={() => void load()} />
  }
  if (loading) return <SkeletonCard />
  if (!readiness) return null

  const blocking = readiness.steps.filter((s) => s.blocking)

  return (
    <div className="space-y-4" data-testid="readiness-panel">
      <div className="flex flex-wrap items-end justify-between gap-3">
        {organizations.length > 1 && (
          <div className="w-56">
            <Select
              label={t("execution.organization")}
              value={orgId != null ? String(orgId) : ""}
              onValueChange={(v) => setOrgId(Number(v))}
              options={organizations.map((o) => ({ value: String(o.id), label: o.name }))}
              size="sm"
            />
          </div>
        )}
        <Button
          variant="secondary"
          size="sm"
          onClick={() => void load()}
          leftIcon={<RefreshCcwIcon size={14} />}
        >
          {t("actions.refresh")}
        </Button>
      </div>

      {/* O veredito vem antes dos números: quem abre esta tela quer saber se
          está funcionando, não quantos eventos passaram. */}
      {readiness.ready ? (
        <Notice variant="success" title={t("readiness.readyTitle")}>
          {readiness.active_policy_name
            ? t("readiness.readyBody", { name: readiness.active_policy_name })
            : null}
        </Notice>
      ) : (
        <Notice
          variant="warning"
          title={t("readiness.blockedTitle", { count: blocking.length })}
        >
          {t("readiness.blockedBody")}
        </Notice>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <div className="space-y-1 p-4">
            <h3 className="mb-2 text-sm font-semibold">{t("readiness.stepsTitle")}</h3>
            <ul className="divide-y divide-border">
              {readiness.steps.map((step) => (
                <li
                  key={step.key}
                  className="flex items-start gap-3 py-3"
                  data-testid={`readiness-step-${step.key}`}
                >
                  <StepIcon status={step.status} />
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium">{step.title}</p>
                    <p className="text-xs text-muted">{step.detail}</p>
                  </div>
                  {step.action && (
                    <div className="flex shrink-0 flex-col items-end gap-1">
                      <Button
                        variant={step.blocking ? "primary" : "outline"}
                        size="xs"
                        onClick={() => handleAction(step)}
                      >
                        {step.action.label}
                      </Button>
                      {step.action.scope === "global" && (
                        <span className="text-[10px] text-muted">
                          {t("readiness.globalScope")}
                        </span>
                      )}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </div>
        </Card>

        <div className="space-y-4">
          <Card>
            <div className="space-y-3 p-4">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold">{t("readiness.hitRate")}</h3>
                <Badge variant="default">
                  {t("execution.rangeOption", { count: RANGE_MINUTES })}
                </Badge>
              </div>
              {metrics.length === 0 ? (
                <p className="text-sm text-muted">{t("execution.noRules")}</p>
              ) : (
                <>
                  <div className="flex flex-wrap gap-2">
                    <Badge variant="success">
                      {t("readiness.avgHit", { value: pct(totals.hit, totals.total) })}
                    </Badge>
                    {totals.unanswered > 0 && (
                      <Badge variant="danger">
                        {t("readiness.unanswered", {
                          value: pct(totals.unanswered, totals.total),
                        })}
                      </Badge>
                    )}
                  </div>
                  <ul className="space-y-2">
                    {metrics.map((m) => {
                      const total = m.hit + m.miss + m.skipped + m.error
                      const silent = total === 0
                      return (
                        <li key={m.rule_id} data-testid={`readiness-metric-${m.rule_id}`}>
                          <div className="flex items-center justify-between gap-2 text-xs">
                            <span className="truncate font-mono">{m.rule_id}</span>
                            {silent ? (
                              <Badge variant="default">{t("execution.silent")}</Badge>
                            ) : (
                              <span className="shrink-0 text-muted">
                                {pct(m.hit, total)} {t("execution.hit")}
                              </span>
                            )}
                          </div>
                          {/* Barra empilhada: acerto, sem correspondência e sem
                              resposta são leituras diferentes e a proporção
                              entre elas é o diagnóstico. */}
                          <div className="mt-1 flex h-1.5 overflow-hidden rounded bg-surface-tertiary">
                            <span
                              className="bg-success-500"
                              style={{ width: total ? `${(m.hit / total) * 100}%` : "0%" }}
                            />
                            <span
                              className="bg-warning-500"
                              style={{ width: total ? `${(m.miss / total) * 100}%` : "0%" }}
                            />
                            <span
                              className="bg-danger-500"
                              style={{
                                width: total ? `${(m.skipped / total) * 100}%` : "0%",
                              }}
                            />
                          </div>
                        </li>
                      )
                    })}
                  </ul>
                </>
              )}
            </div>
          </Card>

          <Card>
            <div className="space-y-2 p-4">
              <h3 className="text-sm font-semibold">
                {t("readiness.recentFailures", { count: entries.length })}
              </h3>
              {entries.length === 0 ? (
                <p className="text-sm text-muted">{t("execution.noFailures")}</p>
              ) : (
                <ul className="divide-y divide-border">
                  {entries.slice(0, 5).map((e, i) => (
                    <li key={`${e.ts}-${i}`} className="py-2">
                      <div className="flex flex-wrap items-center gap-2 text-xs">
                        <span className="font-mono">{e.rule_id ?? "-"}</span>
                        <Badge variant="default">{t(`execution.kind.${e.kind}`)}</Badge>
                        {e.source && <span className="text-muted">{e.source}</span>}
                      </div>
                      {/* A mensagem do provedor é o que permite agir sem abrir
                          log de worker. */}
                      <p className="mt-0.5 break-words font-mono text-xs text-danger-500">
                        {e.detail || e.reason}
                      </p>
                    </li>
                  ))}
                </ul>
              )}
              {entries.length > 5 && (
                <Button
                  variant="ghost"
                  size="xs"
                  onClick={() => onNavigateTab?.("execution")}
                >
                  {t("readiness.seeAll")}
                </Button>
              )}
            </div>
          </Card>
        </div>
      </div>
    </div>
  )
}

export default ReadinessPanel
