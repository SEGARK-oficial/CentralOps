"use client"

import type React from "react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/Button/Button"
import { Notice } from "@/components/ui/Notice/Notice"
import { listPlatformsStreams } from "@/services/api"
import type { BackfillJob, CreateBackfillJobRequest } from "@/types"

// Fallback estático — usado apenas se o endpoint /collectors/platforms-streams
// estiver indisponível. A fonte de verdade é o backend (registry de collectors).
const FALLBACK_PLATFORM_STREAMS: Record<string, string[]> = {
  sophos: ["alerts", "cases", "detections"],
  microsoft_defender: ["alerts", "incidents"],
  ninjaone: ["activities"],
}

const DEFAULT_STREAMS: string[] = ["alerts"]

const MAX_WINDOW_DAYS = 90
const MS_PER_DAY = 86_400_000

interface DateRange {
  from: Date | null
  to: Date | null
}

export interface BackfillFormProps {
  integrationId: number
  platform: string
  onSuccess?: (job: BackfillJob) => void
  onCancel?: () => void
  onCreateJob: (payload: CreateBackfillJobRequest) => Promise<BackfillJob>
}

export const BackfillForm: React.FC<BackfillFormProps> = ({
  integrationId: _integrationId,
  platform,
  onSuccess,
  onCancel,
  onCreateJob,
}) => {
  const { t } = useTranslation("config")
  // Tenta buscar do backend (auto-discovery via registry); cai no
  // fallback estático se a chamada falhar (endpoint indisponível ou
  // erro de rede). Inicia com fallback pra render imediato.
  const [platformStreams, setPlatformStreams] = useState<Record<string, string[]>>(
    FALLBACK_PLATFORM_STREAMS,
  )

  useEffect(() => {
    let cancelled = false
    void listPlatformsStreams()
      .then((res) => {
        if (!cancelled && res?.platforms && Object.keys(res.platforms).length > 0) {
          setPlatformStreams(res.platforms)
        }
      })
      .catch(() => {
        // Silencia: já temos fallback. Em prod, log via Sentry seria ideal.
      })
    return () => {
      cancelled = true
    }
  }, [])

  const availableStreams = platformStreams[platform] ?? DEFAULT_STREAMS

  const [dateRange, setDateRange] = useState<DateRange>({ from: null, to: null })
  const [selectedStreams, setSelectedStreams] = useState<string[]>([])
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [success, setSuccess] = useState(false)

  // Validações
  const from = dateRange.from
  const to = dateRange.to
  const windowMs = from && to ? to.getTime() - from.getTime() : 0
  const windowDays = windowMs / MS_PER_DAY

  const errorDateOrder = from && to && from >= to
  const errorWindowExceeded = from && to && windowDays > MAX_WINDOW_DAYS
  const errorNoStreams = selectedStreams.length === 0
  const errorNoRange = !from || !to

  const isValid = !errorDateOrder && !errorWindowExceeded && !errorNoStreams && !errorNoRange

  const toggleStream = (stream: string) => {
    setSelectedStreams((prev) =>
      prev.includes(stream) ? prev.filter((s) => s !== stream) : [...prev, stream],
    )
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!isValid || !from || !to) return

    try {
      setSubmitting(true)
      setSubmitError(null)
      const job = await onCreateJob({
        streams: selectedStreams,
        from_ts: from.toISOString(),
        to_ts: to.toISOString(),
      })
      setSuccess(true)
      onSuccess?.(job)
    } catch (err) {
      const message = err instanceof Error ? err.message : t("backfill.form.createError")
      setSubmitError(message)
    } finally {
      setSubmitting(false)
    }
  }

  if (success) {
    return (
      <Notice variant="success" title={t("backfill.form.successTitle")}>
        {t("backfill.form.successBody")}
      </Notice>
    )
  }

  return (
    <form
      data-testid="backfill-form"
      onSubmit={(e) => void handleSubmit(e)}
      className="flex flex-col gap-5"
      noValidate
    >
      <Notice variant="info">
        {t("backfill.form.info", { maxDays: MAX_WINDOW_DAYS })}{" "}
        <a
          href="/docs/collector/backfill.md"
          className="underline hover:no-underline"
          target="_blank"
          rel="noopener noreferrer"
        >
          {t("backfill.form.learnMore")}
        </a>
      </Notice>

      {/* Período */}
      <div className="flex flex-col gap-1.5">
        <span className="text-sm font-medium text-text">
          {t("backfill.form.period")} <span className="text-danger-500 ml-0.5">*</span>
        </span>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:gap-3">
          <div className="flex-1">
            <label className="mb-1 block text-xs text-text-secondary" htmlFor="backfill-from-input">
              {t("backfill.form.from")}
            </label>
            <input
              id="backfill-from-input"
              data-testid="backfill-from-input"
              type="datetime-local"
              max={to ? to.toISOString().slice(0, 16) : new Date().toISOString().slice(0, 16)}
              value={from ? from.toISOString().slice(0, 16) : ""}
              onChange={(e) => {
                const val = e.target.value
                setDateRange((prev) => ({
                  ...prev,
                  from: val ? new Date(val) : null,
                }))
              }}
              className="h-9 w-full rounded-md border border-border bg-surface px-3 text-sm focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/20"
              aria-label={t("backfill.form.fromAriaLabel")}
              required
            />
          </div>
          <div className="flex-1">
            <label className="mb-1 block text-xs text-text-secondary" htmlFor="backfill-to-input">
              {t("backfill.form.to")}
            </label>
            <input
              id="backfill-to-input"
              data-testid="backfill-to-input"
              type="datetime-local"
              max={new Date().toISOString().slice(0, 16)}
              min={from ? from.toISOString().slice(0, 16) : undefined}
              value={to ? to.toISOString().slice(0, 16) : ""}
              onChange={(e) => {
                const val = e.target.value
                setDateRange((prev) => ({
                  ...prev,
                  to: val ? new Date(val) : null,
                }))
              }}
              className="h-9 w-full rounded-md border border-border bg-surface px-3 text-sm focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/20"
              aria-label={t("backfill.form.toAriaLabel")}
              required
            />
          </div>
        </div>

        {errorDateOrder && (
          <p className="text-xs text-danger-500" role="alert">
            {t("backfill.form.errorDateOrder")}
          </p>
        )}
        {errorWindowExceeded && (
          <Notice variant="warning" title={t("backfill.form.windowExceededTitle", { maxDays: MAX_WINDOW_DAYS })}>
            {t("backfill.form.windowExceededBody", { maxDays: MAX_WINDOW_DAYS })}
          </Notice>
        )}
      </div>

      {/* Streams */}
      <div className="flex flex-col gap-1.5">
        <span
          className="text-sm font-medium text-text"
          id="backfill-streams-label"
        >
          {t("backfill.form.streams")} <span className="text-danger-500 ml-0.5">*</span>
        </span>
        <div
          data-testid="backfill-streams-select"
          role="group"
          aria-labelledby="backfill-streams-label"
          className="flex flex-wrap gap-2"
        >
          {availableStreams.map((stream) => {
            const checked = selectedStreams.includes(stream)
            return (
              <label
                key={stream}
                className="flex cursor-pointer items-center gap-2 rounded-md border border-border px-3 py-2 text-sm transition-colors hover:border-primary-400 has-[:checked]:border-primary-500 has-[:checked]:bg-primary-50 has-[:checked]:text-primary-700"
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => toggleStream(stream)}
                  className="h-4 w-4 rounded border-border text-primary-600 focus:ring-primary-500"
                  aria-label={t("backfill.form.streamAriaLabel", { stream })}
                />
                {stream}
              </label>
            )
          })}
        </div>
        {errorNoStreams && (
          <p className="text-xs text-danger-500" role="alert">
            {t("backfill.form.errorNoStreams")}
          </p>
        )}
      </div>

      {/* Erro do backend */}
      {submitError && (
        <Notice variant="danger" title={t("backfill.form.createErrorTitle")}>
          {submitError}
        </Notice>
      )}

      {/* Ações */}
      <div className="flex justify-end gap-3 pt-1">
        {onCancel && (
          <Button type="button" variant="outline" onClick={onCancel} disabled={submitting}>
            {t("backfill.form.cancel")}
          </Button>
        )}
        <Button
          type="submit"
          data-testid="submit-backfill"
          disabled={!isValid}
          loading={submitting}
        >
          {t("backfill.form.submit")}
        </Button>
      </div>
    </form>
  )
}

export default BackfillForm
