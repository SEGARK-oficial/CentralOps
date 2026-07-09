/**
 * EnvelopePreview
 * Painel direito do editor — mostra o resultado do dry-run.
 * Estados: loading, vazio, resultado, falhas, erro de API.
 * Fase 4.1b: passa default_hit_warnings ao DryRunStatusBar e gerencia o popover.
 */

import type React from "react"
import { useId, useState, useCallback } from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"
import type { DryRunResult, DryRunDefaultHitWarning } from "@/types"
import { JsonViewer } from "@/components/shared/JsonViewer"
import { Notice } from "@/components/ui/Notice/Notice"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { DryRunStatusBar } from "@/components/mappings/DryRunStatusBar"
import { DefaultHitWarningsPopover } from "@/components/mappings/DefaultHitWarningsPopover"

interface EnvelopePreviewProps {
  result: DryRunResult | null
  isPending: boolean
  error: Error | null
  /** Fase 4.1b: callback acionado quando o usuário clica "Marcar como intencional". */
  onMarkIntentional?: (target: string) => void
  className?: string
}

export const EnvelopePreview: React.FC<EnvelopePreviewProps> = ({
  result,
  isPending,
  error,
  onMarkIntentional,
  className,
}) => {
  const { t } = useTranslation("mappings")
  const headingId = useId()
  const [warningsOpen, setWarningsOpen] = useState(false)

  const handleWarningsClick = useCallback(() => setWarningsOpen(true), [])
  const handleWarningsClose = useCallback(() => setWarningsOpen(false), [])

  const handleMarkIntentional = useCallback(
    (target: string) => {
      onMarkIntentional?.(target)
      // Fecha o popover apenas quando a última warning foi marcada
      // (o pai vai remover o item da lista via dry-run re-run)
    },
    [onMarkIntentional],
  )

  const firstExample = result?.output_examples?.[0] ?? null
  const hasFailures = (result?.fail_count ?? 0) > 0
  const firstFailure = result?.rule_failures?.[0] ?? null
  const defaultHitWarnings: DryRunDefaultHitWarning[] = result?.default_hit_warnings ?? []

  return (
    <section
      role="region"
      aria-labelledby={headingId}
      data-testid="envelope-preview"
      className={cn(
        "flex flex-col gap-3 rounded-lg border border-border bg-surface p-4 min-h-0",
        className,
      )}
    >
      {/* Cabeçalho */}
      <div className="flex items-center gap-2">
        <h2
          id={headingId}
          className="text-sm font-semibold text-text flex-1"
        >
          {t("envelopePreview.heading")}
          <span className="text-text-tertiary font-normal ml-1">{t("envelopePreview.previewSuffix")}</span>
        </h2>
        {isPending && !result && (
          <LoadingSpinner size="sm" />
        )}
      </div>

      {/* Status bar — só quando há resultado ou está pendente com resultado anterior */}
      {result && (
        <DryRunStatusBar
          sampleSize={result.sample_size}
          okCount={result.ok_count}
          failCount={result.fail_count}
          isPending={isPending}
          default_hit_warnings={defaultHitWarnings}
          onWarningsClick={handleWarningsClick}
        />
      )}

      {/* Popover de avisos de 100% default */}
      {warningsOpen && defaultHitWarnings.length > 0 && (
        <DefaultHitWarningsPopover
          warnings={defaultHitWarnings}
          onMarkIntentional={handleMarkIntentional}
          onClose={handleWarningsClose}
        />
      )}

      {/* Erro de API */}
      {error && !isPending && (
        <Notice variant="danger" title={t("envelopePreview.simulationErrorTitle")}>
          {error.message}
        </Notice>
      )}

      {/* Aviso de falhas em regras */}
      {hasFailures && firstFailure && (
        <Notice variant="warning" title={t("envelopePreview.ruleFailuresTitle")}>
          {t("envelopePreview.ruleFailedBefore")} <code className="font-mono text-xs">{firstFailure.target}</code>{" "}
          {t("envelopePreview.ruleFailedMiddle", { count: firstFailure.fail_count })}
          {firstFailure.fail_examples[0] ? (
            <>: <span className="text-xs">{firstFailure.fail_examples[0]}</span></>
          ) : null}
        </Notice>
      )}

      {/* Resultado — JSON do primeiro output_example */}
      {firstExample !== null && (
        <div className="rounded-md border border-border bg-surface-secondary p-3 overflow-auto flex-1">
          <JsonViewer data={firstExample} collapseLevel={3} />
        </div>
      )}

      {/* Estado loading inicial (sem resultado anterior) */}
      {isPending && !result && !error && (
        <div className="flex-1 flex items-center justify-center py-12">
          <LoadingSpinner size="md" text={t("envelopePreview.calculating")} />
        </div>
      )}

      {/* Estado vazio — sem payload fornecido ou sem resultado */}
      {!isPending && !result && !error && (
        <EmptyState
          title={t("envelopePreview.empty.title")}
          description={t("envelopePreview.empty.description")}
        />
      )}
    </section>
  )
}

export default EnvelopePreview
