/**
 * useMappingDryRun
 * Executa dry-run debounced de regras de mapping contra samples do reservoir.
 * Cancela requests anteriores via AbortController para evitar race conditions.
 */

import { useEffect, useRef, useState } from "react"
import type { MappingRule, PreprocessOp, DryRunResult } from "@/types"
import { postMappingDryRun } from "@/services/api"

interface DryRunOptions {
  debounceMs?: number
  vendor?: string
  eventType?: string
  limit?: number
  /** org cujo reservoir inspecionar (admin global). null = própria org. */
  organizationId?: number | null
  /** Lista de ops de pré-processamento (default: vazia). */
  preprocess?: PreprocessOp[]
}

interface UseMappingDryRunReturn {
  result: DryRunResult | null
  isPending: boolean
  error: Error | null
}

export function useMappingDryRun(
  rules: MappingRule[],
  rawEvents: Record<string, unknown>[] | null,
  options?: DryRunOptions,
): UseMappingDryRunReturn {
  // Debounce removido deste hook: o caller (MappingEditorPage) já passa
  // `effectiveRules` que é a única fonte de verdade do debounce.
  // Manter debounce aqui causava double-debounce e dry-run thrashing.
  // Callers que usam debounceMs > 0 precisam debounçar externamente.

  const abortRef = useRef<AbortController | null>(null)
  const [result, setResult] = useState<DryRunResult | null>(null)
  const [isPending, setIsPending] = useState(false)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    // Cancela request anterior se ainda em voo
    abortRef.current?.abort()

    if (!rules.length) {
      setResult(null)
      setIsPending(false)
      return
    }

    const controller = new AbortController()
    abortRef.current = controller

    setIsPending(true)
    setError(null)

    postMappingDryRun(
      {
        rules,
        preprocess: options?.preprocess,
        raw_events: rawEvents ?? undefined,
        vendor: options?.vendor,
        event_type: options?.eventType,
        limit: options?.limit ?? 100,
        organization_id: options?.organizationId ?? undefined,
      },
      { signal: controller.signal },
    )
      .then((r) => {
        setResult(r)
        setError(null)
      })
      .catch((e: unknown) => {
        // AbortError = request cancelada intencionalmente; ignorar silenciosamente
        if (e instanceof Error && e.name === "AbortError") return
        setError(e instanceof Error ? e : new Error(String(e)))
      })
      .finally(() => {
        // Só limpa isPending se este controller não foi abortado
        if (!controller.signal.aborted) setIsPending(false)
      })

    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rules, rawEvents, options?.vendor, options?.eventType, options?.limit, options?.organizationId, options?.preprocess])

  return { result, isPending, error }
}
