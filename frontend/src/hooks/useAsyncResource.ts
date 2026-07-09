/**
 * useAsyncResource — hook genérico para encapsular o padrão load/loading/error/retry.
 *
 * O erro persiste até ser resolvido (não some com timeout). Feedback de
 * ações pontuais (ex.: toast) deve ser tratado pelo chamador via callbacks.
 *
 * @example
 *   const { data, loading, error, reload } = useAsyncResource(() => api.getUser(id))
 */

import { useState, useCallback, useEffect, useRef } from "react"

// ── Tipos ─────────────────────────────────────────────────────────────────────

/** Função assíncrona que retorna os dados. Sem parâmetros; use closures para deps. */
export type AsyncLoader<T> = () => Promise<T>

export interface AsyncResourceState<T> {
  /** Dados retornados pelo loader após sucesso. `null` enquanto não carregado. */
  data: T | null
  /** `true` durante a execução do loader. */
  loading: boolean
  /** Erro persistente capturado na última execução. `null` enquanto sem erro. */
  error: Error | null
  /** Dispara nova execução do loader (ex.: ao clicar em "Tentar novamente"). */
  reload: () => void
}

// ── Hook ──────────────────────────────────────────────────────────────────────

/**
 * @param loader   Função assíncrona que retorna `T`. Deve ser estável (useCallback)
 *                 ou criada fora do componente para evitar re-runs infinitos.
 * @param options  Opções extras (ex.: `immediate: false` para carregar manualmente).
 */
export function useAsyncResource<T>(
  loader: AsyncLoader<T>,
  options: { immediate?: boolean } = {},
): AsyncResourceState<T> {
  const { immediate = true } = options

  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState<boolean>(immediate)
  const [error, setError] = useState<Error | null>(null)

  // Evita atualizar estado após desmontagem
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  // Contador de execução para descartar respostas de chamadas anteriores
  const callCountRef = useRef(0)

  const execute = useCallback(async () => {
    callCountRef.current += 1
    const currentCall = callCountRef.current

    setLoading(true)
    setError(null)

    try {
      const result = await loader()
      if (mountedRef.current && currentCall === callCountRef.current) {
        setData(result)
      }
    } catch (err) {
      if (mountedRef.current && currentCall === callCountRef.current) {
        setError(err instanceof Error ? err : new Error(String(err)))
      }
    } finally {
      if (mountedRef.current && currentCall === callCountRef.current) {
        setLoading(false)
      }
    }
  }, [loader])

  // Carga inicial
  useEffect(() => {
    if (immediate) {
      execute()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [execute])

  const reload = useCallback(() => {
    execute()
  }, [execute])

  return { data, loading, error, reload }
}
