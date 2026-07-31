/**
 * CaptureTrajectoryPanel
 *
 * A vida de UM evento dentro do pipeline: coletado → roteado → entregue, com o
 * payload de cada estágio lado a lado.
 *
 * Existe porque um único painel "antes/depois" MENTE por omissão. O mesmo
 * evento aparece no ring com três normalizações diferentes e nada as
 * distinguia: `quarantined` traz o raw íntegro, `dropped`/`sampled_out` trazem
 * o envelope PRÉ-transformação-por-destino, e `delivered` traz o payload PÓS
 * redação de PII, PÓS drop_raw e PÓS aggregate. Sem o rótulo de estágio, o
 * operador comparava coisas incomparáveis.
 *
 * Três avisos são obrigatórios aqui, e cada um corrige uma conclusão errada:
 *
 *   - `complete=false` → não há registro `collected`. O bruto saiu da janela do
 *     ring (é a entrada mais VELHA do grupo, logo a primeira a ser podada). Sem
 *     dizer isso, um painel vazio parece "o vendor não mandou nada".
 *   - `pii_redacted=false` → este registro NÃO passou pela redação da rota. A
 *     redação é por destino e alcança o bloco `raw`, então um evento dropado
 *     exibe em claro o que o destino teria recebido redigido.
 *   - fidelidade do wire → `format()` não é o byte exato para todos os sinks.
 *     Quatro níveis diferentes de "quase", e dois em que não existe wire por
 *     evento nenhum.
 */

import { useEffect, useState } from "react"
import type React from "react"
import { useTranslation } from "react-i18next"
import { Badge } from "@/components/ui/Badge/Badge"
import { Notice } from "@/components/ui/Notice/Notice"
import { getCaptureTrajectory } from "@/services/api"
import type { CaptureEvent, CaptureStage, CaptureTrajectory, WireFidelity } from "@/types"

interface Props {
  sessionId: string
  eventId: string
  orgId?: number | null
}

const STAGE_ORDER: CaptureStage[] = ["collected", "routed", "delivered"]

/** Tom por nível de fidelidade. `exact` é o ÚNICO verde — os demais são
 *  ressalvas, e pintá-los de sucesso convidaria o operador a diffar contra o
 *  SIEM e desconfiar do produto. */
const FIDELITY_TONE: Record<WireFidelity, "success" | "warning" | "danger" | "default"> = {
  exact: "success",
  nondeterministic: "warning",
  partial: "warning",
  not_representable: "default",
  error: "danger",
}

function payloadFor(ev: CaptureEvent): unknown {
  const p = ev.event as Record<string, unknown> | undefined
  if (!p) return {}
  // No estágio `collected` o que interessa é o bruto do vendor; nos demais, o
  // envelope inteiro (que já carrega raw + normalized).
  if (ev.stage === "collected" && p["raw"] !== undefined) return p["raw"]
  return p
}

export const CaptureTrajectoryPanel: React.FC<Props> = ({ sessionId, eventId, orgId }) => {
  const { t } = useTranslation("config")
  const [data, setData] = useState<CaptureTrajectory | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!sessionId || !eventId) return
    let alive = true
    setLoading(true)
    setError(null)
    getCaptureTrajectory(sessionId, eventId, orgId)
      .then((r) => {
        if (alive) setData(r)
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e : new Error(String(e)))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [sessionId, eventId, orgId])

  if (loading) {
    return <p className="text-xs text-text-tertiary">{t("capture.trajectory.loading")}</p>
  }
  if (error) {
    return (
      <Notice variant="danger" title={t("capture.trajectory.errorTitle")}>
        {error.message}
      </Notice>
    )
  }
  if (!data || data.count === 0) {
    return <p className="text-xs text-text-tertiary">{t("capture.trajectory.empty")}</p>
  }

  const byStage = new Map<CaptureStage, CaptureEvent[]>()
  for (const ev of data.events) {
    const s = (ev.stage || "routed") as CaptureStage
    byStage.set(s, [...(byStage.get(s) ?? []), ev])
  }

  return (
    <div className="space-y-3" data-testid="capture-trajectory">
      {/* O bruto é a entrada mais VELHA do grupo e a primeira a ser podada.
          Dizer isso evita que um painel vazio seja lido como "o vendor não
          mandou nada". */}
      {!data.complete && (
        <Notice variant="warning" data-testid="trajectory-incomplete">
          {t("capture.trajectory.rawEvicted")}
        </Notice>
      )}

      <div className="flex flex-wrap items-center gap-1.5 text-xs text-text-tertiary">
        <span>{t("capture.trajectory.stages")}</span>
        {STAGE_ORDER.map((s) => (
          <Badge key={s} variant={byStage.has(s) ? "primary" : "default"} size="sm">
            {t(`capture.trajectory.stage.${s}`)}
            {byStage.has(s) ? ` (${byStage.get(s)!.length})` : " —"}
          </Badge>
        ))}
      </div>

      {STAGE_ORDER.filter((s) => byStage.has(s)).map((stage) =>
        byStage.get(stage)!.map((ev, i) => (
          <div
            key={`${stage}-${i}`}
            className="rounded border border-border bg-surface-tertiary p-3"
            data-testid={`trajectory-${stage}`}
          >
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <Badge variant="primary" size="sm">
                {t(`capture.trajectory.stage.${stage}`)}
              </Badge>
              <span className="text-xs text-text-secondary">
                {t(`capture.outcomes.${ev.outcome}`, { defaultValue: ev.outcome })}
              </span>
              {ev.destination_kind && (
                <span className="text-xs text-text-tertiary">{ev.destination_kind}</span>
              )}
              {/* Com aggregate ligado o evento individual NÃO existe no destino:
                  o lote virou metric-events sintéticos. Nenhum desenho recupera
                  isso — a tela admite. */}
              {ev.payload_kind === "aggregate_metric" && (
                <Badge variant="warning" size="sm" data-testid="badge-aggregate">
                  {t("capture.trajectory.aggregated")}
                </Badge>
              )}
              {/* A redação é POR ROTA e alcança o bloco `raw`. Um registro
                  pré-entrega mostra em claro o que o destino receberia redigido. */}
              {ev.pii_redacted === false && (
                <Badge variant="warning" size="sm" data-testid="badge-pii-clear">
                  {t("capture.trajectory.piiNotRedacted")}
                </Badge>
              )}
            </div>

            <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-all text-xs text-text">
              {JSON.stringify(payloadFor(ev), null, 2)}
            </pre>

            {ev.wire && (
              <div className="mt-2 border-t border-border pt-2" data-testid="trajectory-wire">
                <div className="mb-1 flex flex-wrap items-center gap-2">
                  <span className="text-xs font-semibold uppercase tracking-wide text-text-secondary">
                    {t("capture.trajectory.wire")}
                  </span>
                  <Badge variant={FIDELITY_TONE[ev.wire.fidelity] ?? "default"} size="sm">
                    {t(`capture.trajectory.fidelity.${ev.wire.fidelity}`, {
                      defaultValue: ev.wire.fidelity,
                    })}
                  </Badge>
                </div>
                {/* A nota diz O QUE FALTA para ser o byte exato. É ela que
                    impede o operador de concluir que o produto está errado
                    quando o diff contra o SIEM não bate. */}
                {ev.wire.note && (
                  <p className="mb-1 text-xs text-text-tertiary">{ev.wire.note}</p>
                )}
                {ev.wire.text !== undefined ? (
                  <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all text-xs text-text">
                    {ev.wire.text}
                    {ev.wire.truncated && (
                      <span className="text-text-tertiary">
                        {"\n"}
                        {t("capture.trajectory.wireTruncated", { bytes: ev.wire.bytes })}
                      </span>
                    )}
                  </pre>
                ) : (
                  // `not_representable`: a unidade do fio é o LOTE. Mostrar um
                  // fragmento por evento induziria exatamente a comparação
                  // errada — por isso NÃO há preview, só a explicação.
                  <p className="text-xs text-text-tertiary" data-testid="wire-no-preview">
                    {t("capture.trajectory.noWirePreview")}
                  </p>
                )}
              </div>
            )}
          </div>
        )),
      )}
    </div>
  )
}
