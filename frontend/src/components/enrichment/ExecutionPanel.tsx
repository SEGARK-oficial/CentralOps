import type React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { AlertTriangleIcon, CheckCircleIcon, RefreshCcwIcon, XCircleIcon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { ErrorState } from "@/components/ui/ErrorState"
import { Notice } from "@/components/ui/Notice/Notice"
import { Select } from "@/components/ui/Select/Select"
import { SkeletonCard } from "@/components/ui/Skeleton"
import * as api from "@/services/api"
import type { EnrichmentActivityEntry, EnrichmentRuleMetrics } from "@/services/api"

interface ExecutionPanelProps {
  /**
   * Orgs visíveis ao chamador. Um token escopado vê uma; um global vê várias, e
   * aí a escolha é obrigatória: não existe enriquecimento global, então a API
   * responde 422 sem `organization_id` (`_resolve_target_org`).
   */
  organizations?: Array<{ id: number; name: string }>
  /** Org do filtro global do topo. Num MSP é ela que o operador está olhando. */
  selectedOrgId?: number | null
}

const RANGE_OPTIONS = [15, 60, 180]

function pct(n: number, total: number): string {
  if (total <= 0) return "0%"
  return `${Math.round((n / total) * 100)}%`
}

function useAgo(): (ts: number) => string {
  const { t } = useTranslation("enrichment")
  return (ts: number) => {
    const secs = Math.max(0, Math.floor(Date.now() / 1000 - ts))
    if (secs < 60) return t("execution.agoSeconds", { count: secs })
    if (secs < 3600) return t("execution.agoMinutes", { count: Math.floor(secs / 60) })
    return t("execution.agoHours", { count: Math.floor(secs / 3600) })
  }
}

/**
 * Responde "esse enriquecimento está funcionando?" com duas leituras distintas.
 *
 * **Consultas** mostram cada tentativa de ir buscar dado: carga de tabela (uma
 * por ciclo) e consulta remota (uma por lote). É aqui que aparecem 401 de token
 * errado, DNS quebrado e coleção TAXII vazia, com a mensagem do provedor.
 *
 * **Aproveitamento** mostra, por regra, quanto casou. Uma regra que vinha em
 * 90% de acerto e foi a zero mudou de schema na origem: a consulta continua
 * respondendo 200, e só este número denuncia.
 *
 * A separação é o ponto. Consulta OK com acerto em queda é problema de DADO;
 * consulta falhando é credencial ou rede. A ação do operador é diferente, e
 * um número só não distingue os dois casos.
 */
export const ExecutionPanel: React.FC<ExecutionPanelProps> = ({
  organizations = [],
  selectedOrgId = null,
}) => {
  const { t } = useTranslation("enrichment")
  const ago = useAgo()

  // Uma org só: escolher seria pergunta com uma resposta. Várias: começa pela
  // do filtro global do topo, não por `organizations[0]`. Num MSP as duas
  // divergem, e o painel reportaria a saúde de um tenant enquanto o resto da
  // tela fala de outro.
  const [orgId, setOrgId] = useState<number | null>(
    selectedOrgId ?? organizations[0]?.id ?? null,
  )

  // As organizações chegam por fetch (`PlatformContext`), então podem estar
  // vazias na montagem. Sem reconciliar, `orgId` ficava `null` para sempre e um
  // admin global tomava 422 permanente — e com uma org só o seletor nem
  // aparece, então não sobrava controle na tela para corrigir.
  useEffect(() => {
    if (orgId != null) return
    const fallback = selectedOrgId ?? organizations[0]?.id ?? null
    if (fallback != null) setOrgId(fallback)
  }, [orgId, selectedOrgId, organizations])
  const [rangeMinutes, setRangeMinutes] = useState(60)
  const [onlyFailures, setOnlyFailures] = useState(false)
  const [metrics, setMetrics] = useState<EnrichmentRuleMetrics[]>([])
  const [policyName, setPolicyName] = useState<string | null>(null)
  const [entries, setEntries] = useState<EnrichmentActivityEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const orgParam = orgId != null ? { organization_id: orgId } : {}
      const [m, a] = await Promise.all([
        api.getEnrichmentMetrics({ ...orgParam, range_minutes: rangeMinutes }),
        api.getEnrichmentActivity({ ...orgParam, limit: 100, only_failures: onlyFailures }),
      ])
      setMetrics(m.rules)
      setPolicyName(m.policy_name ?? null)
      setEntries(a.entries)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [orgId, rangeMinutes, onlyFailures])

  useEffect(() => {
    void load()
  }, [load])

  const failures = useMemo(() => entries.filter((e) => !e.ok).length, [entries])

  if (error) {
    return <ErrorState title={t("errorTitle")} message={error} onRetry={() => void load()} />
  }

  if (loading) return <SkeletonCard />

  const nothingYet = metrics.length === 0 && entries.length === 0

  return (
    <div className="space-y-4" data-testid="execution-panel">
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
        <div className="w-40">
          <Select
            label={t("execution.range")}
            value={String(rangeMinutes)}
            onValueChange={(v) => setRangeMinutes(Number(v))}
            options={RANGE_OPTIONS.map((m) => ({
              value: String(m),
              label: t("execution.rangeOption", { count: m }),
            }))}
            size="sm"
          />
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant={onlyFailures ? "primary" : "outline"}
            size="sm"
            onClick={() => setOnlyFailures((v) => !v)}
            data-testid="toggle-only-failures"
            leftIcon={<AlertTriangleIcon size={14} />}
          >
            {t("execution.onlyFailures", { count: failures })}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => void load()}
            leftIcon={<RefreshCcwIcon size={14} />}
          >
            {t("actions.refresh")}
          </Button>
        </div>
      </div>

      {nothingYet ? (
        <EmptyState
          title={t("execution.emptyTitle")}
          description={t("execution.emptyBody")}
        />
      ) : (
        <>
          {/* Aproveitamento por regra */}
          <Card>
            <div className="space-y-3 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-semibold">{t("execution.hitRate")}</h3>
                {/* Qual política está valendo não é detalhe: o worker aplica
                    UMA por organização, a mais antiga habilitada. Sem dizer
                    qual, editar a política errada parece não ter efeito. */}
                {policyName && (
                  <Badge variant="default" data-testid="active-policy">
                    {t("execution.activePolicy", { name: policyName })}
                  </Badge>
                )}
              </div>
              {metrics.length === 0 ? (
                <p className="text-sm text-muted">{t("execution.noRules")}</p>
              ) : (
                <ul className="divide-y divide-border">
                  {metrics.map((m) => {
                    const total = m.hit + m.miss + m.skipped + m.error
                    // Regra que não roda há uma hora não é "0% de acerto": é
                    // muda. Confundir as duas manda o operador mexer na tabela
                    // quando o problema é a política estar desligada.
                    const silent = total === 0
                    // `skipped` é "a consulta não respondeu por esta regra":
                    // timeout, provedor fora do ar, fonte não resolvida,
                    // orçamento estourado, cache L2 ausente (applier.py:302).
                    // Estava no denominador e fora da tela, então falha de
                    // INFRAESTRUTURA aparecia como queda de acerto e a dica ao
                    // lado mandava procurar mudança de formato no dado.
                    const unanswered = m.skipped > 0
                    return (
                      <li
                        key={m.rule_id}
                        className="flex flex-wrap items-center justify-between gap-2 py-2"
                        data-testid={`metric-${m.rule_id}`}
                      >
                        <div className="min-w-0">
                          <span className="font-mono text-sm">{m.rule_id}</span>
                          <span className="ml-2 text-xs text-muted">
                            {m.enricher}
                            {m.source ? ` · ${m.source}` : ""}
                          </span>
                        </div>
                        {silent ? (
                          <Badge variant="default">{t("execution.silent")}</Badge>
                        ) : (
                          <div className="flex flex-wrap gap-1.5">
                            <Badge variant="success">
                              {t("execution.hit")} {pct(m.hit, total)}
                            </Badge>
                            <Badge variant="warning">
                              {t("execution.miss")} {pct(m.miss, total)}
                            </Badge>
                            {unanswered && (
                              <Badge variant="danger">
                                {t("execution.unanswered")} {pct(m.skipped, total)}
                              </Badge>
                            )}
                            {m.error > 0 && (
                              <Badge variant="danger">
                                {t("execution.error")} {m.error}
                              </Badge>
                            )}
                          </div>
                        )}
                      </li>
                    )
                  })}
                </ul>
              )}
              <p className="text-xs text-muted">
                {metrics.some((m) => m.skipped > 0)
                  ? t("execution.hitRateHintUnanswered")
                  : t("execution.hitRateHint")}
              </p>
            </div>
          </Card>

          {/* Consultas */}
          <Card>
            <div className="space-y-3 p-4">
              <h3 className="text-sm font-semibold">{t("execution.queries")}</h3>
              {entries.length === 0 ? (
                <p className="text-sm text-muted">
                  {onlyFailures ? t("execution.noFailures") : t("execution.noQueries")}
                </p>
              ) : (
                <ul className="divide-y divide-border">
                  {entries.map((e, i) => (
                    <li
                      key={`${e.ts}-${i}`}
                      className="flex items-start gap-3 py-2"
                      data-testid={`activity-${i}`}
                    >
                      {e.ok ? (
                        <CheckCircleIcon size={16} className="mt-0.5 shrink-0 text-success-500" aria-hidden />
                      ) : (
                        <XCircleIcon size={16} className="mt-0.5 shrink-0 text-danger-500" aria-hidden />
                      )}
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-mono text-sm">{e.rule_id ?? "-"}</span>
                          <Badge variant="default">{t(`execution.kind.${e.kind}`)}</Badge>
                          {e.enricher && <span className="text-xs text-muted">{e.enricher}</span>}
                          {e.source && <span className="text-xs text-muted">· {e.source}</span>}
                        </div>
                        {/* A mensagem do provedor é o que permite agir sem
                            abrir log de worker. Fica em destaque, não escondida. */}
                        {!e.ok && (e.detail || e.reason) && (
                          <p className="mt-0.5 break-words font-mono text-xs text-danger-500">
                            {e.detail || e.reason}
                          </p>
                        )}
                        <p className="mt-0.5 text-xs text-muted">
                          {ago(e.ts)}
                          {e.entries != null && ` · ${t("execution.entries", { count: e.entries })}`}
                          {e.keys != null && ` · ${t("execution.keys", { count: e.keys })}`}
                          {e.latency_ms != null && ` · ${Math.round(e.latency_ms)} ms`}
                        </p>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
              <p className="text-xs text-muted">{t("execution.queriesHint")}</p>
            </div>
          </Card>
        </>
      )}

      {/* Não é ruído: o log vive em Redis com janela curta, e alguém vai abrir
          esta aba na segunda-feira procurando o erro de sexta. */}
      <Notice variant="info" title={t("execution.retentionTitle")}>
        {t("execution.retentionBody")}
      </Notice>
    </div>
  )
}

export default ExecutionPanel
