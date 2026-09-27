/**
 * api/_core — infraestrutura compartilhada por todo `src/services/api/*.ts`.
 *
 * R4-6.3: extraído de `src/services/api.ts` (3.438 linhas) na divisão por
 * domínio. `apiRequest`/`ApiRequestOptions`/`BASE_URL`/`ADMIN_REDIRECT_PATH`/
 * `V1_ACCEPT_HEADER` viram `export` aqui (não eram públicos no arquivo único)
 * só para uso INTERNO entre os módulos de domínio — o barrel
 * (`api/index.ts`) NÃO os re-exporta, preservando exatamente a superfície
 * pública que `@/services/api` tinha antes.
 */
import i18n from "@/i18n"

export const BASE_URL = import.meta.env.VITE_BACKEND_URL || "/api"
// Destino do 403 com `forbiddenRedirectTo`. Era "/search" — tela removida no
// ADR-0007 (não há mais rota), então o usuário sem permissão caía no 404.
export const ADMIN_REDIRECT_PATH = "/dashboard"
export const V1_ACCEPT_HEADER = { Accept: "application/vnd.centralops.v1+json" } as const

export interface ApiRequestOptions extends RequestInit {
  forbiddenRedirectTo?: string
  /**
   * R4-6.1: sem isto, uma requisição pendurada (proxy travado, backend
   * catatônico) nunca resolve nem rejeita — `inFlightRef`/`abortRef` de quem
   * chamou fica preso PARA SEMPRE, e qualquer poll que dependa de "a chamada
   * anterior terminou" (ex.: `useBackfillJobs`, `useQueryJobs`) para de vez.
   * `0`/`Infinity` desliga o timeout (upload/export grande).
   */
  timeoutMs?: number
}

// ACIMA do `proxy_read_timeout 60s` do nginx (frontend/nginx.single*.conf):
// uma operação legitimamente lenta (teste de conexão, dry-run, preview de
// regra, sync do Entra) recebe o 504 do proxy primeiro, com mensagem real, e
// este timeout só age sobre conexão de fato pendurada. Com 30s o cliente
// abortava operações que o backend ainda concluía (risco de reenvio duplo).
export const DEFAULT_REQUEST_TIMEOUT_MS = 75_000

// Upload (corpo binário/multipart) é limitado pela banda do usuário, não pela
// latência do servidor: sem timeout por padrão, salvo `timeoutMs` explícito.
function isUploadBody(body: RequestInit["body"]): boolean {
  return (
    (typeof FormData !== "undefined" && body instanceof FormData) ||
    (typeof Blob !== "undefined" && body instanceof Blob)
  )
}

/**
 * Timeout PRÓPRIO via `setTimeout`, não `AbortSignal.timeout()` — o timer
 * nativo do `AbortSignal.timeout()` roda fora do laço de eventos visível ao
 * JS em alguns runtimes e não respeita `vi.useFakeTimers()`; com
 * `setTimeout` cru, o teste controla o relógio.
 */
function createTimeoutSignal(ms: number): { signal: AbortSignal; clear: () => void } {
  const controller = new AbortController()
  const timer = setTimeout(() => {
    controller.abort(
      new DOMException(i18n.t("common:feedback.requestTimeout"), "TimeoutError"),
    )
  }, ms)
  return { signal: controller.signal, clear: () => clearTimeout(timer) }
}

/**
 * Combina o `signal` do chamador (cancelamento deliberado — troca de filtro,
 * unmount) com o do timeout, preservando o MOTIVO original de cada um: quem
 * trata o erro rio abaixo (`cause.name === "AbortError"` vs `"TimeoutError"`)
 * continua distinguindo "eu cancelei" de "o servidor não respondeu".
 * `AbortSignal.any` é Baseline 2024 — o fallback cobre runtime mais velho.
 */
function combineSignals(...signals: (AbortSignal | null | undefined)[]): AbortSignal | undefined {
  const present = signals.filter((s): s is AbortSignal => !!s)
  if (present.length === 0) return undefined
  if (present.length === 1) return present[0]
  if (typeof AbortSignal.any === "function") return AbortSignal.any(present)

  const controller = new AbortController()
  for (const s of present) {
    if (s.aborted) {
      controller.abort(s.reason)
      break
    }
    s.addEventListener("abort", () => controller.abort(s.reason), { once: true })
  }
  return controller.signal
}

export class ApiRequestError extends Error {
  statusCode: number
  code?: string
  details?: Record<string, unknown>

  constructor(message: string, statusCode: number, code?: string, details?: Record<string, unknown>) {
    super(message)
    this.name = "ApiRequestError"
    this.statusCode = statusCode
    this.code = code
    this.details = details
  }
}

// FastAPI devolve 422 com `detail` = lista de erros do Pydantic
// (`{type, loc, msg, input, ctx}`). Jogar esse array cru na tela via
// JSON.stringify mostrava `[{"type":"value_error","loc":[...]}]` pro
// usuário. Aqui vira "campo: mensagem" legível.
const VALIDATION_LOC_PREFIXES = new Set(["body", "query", "path", "header", "cookie"])

export function formatValidationDetail(detail: unknown): string | null {
  if (!Array.isArray(detail)) return null
  const parts: string[] = []
  for (const item of detail) {
    if (!item || typeof item !== "object") continue
    const { loc, msg } = item as { loc?: unknown; msg?: unknown }
    if (typeof msg !== "string" || !msg) continue
    // Pydantic prefixa validador custom com "Value error, ".
    const message = msg.replace(/^Value error,\s*/, "")
    const segments = Array.isArray(loc) ? loc.map(String) : []
    if (segments.length > 1 && VALIDATION_LOC_PREFIXES.has(segments[0])) {
      segments.shift()
    }
    const field = segments.join(".")
    parts.push(field ? `${field}: ${message}` : message)
  }
  return parts.length ? parts.join("; ") : null
}

// Helper para fazer requests
export async function apiRequest<T>(endpoint: string, options: ApiRequestOptions = {}): Promise<T> {
  const url = `${BASE_URL}${endpoint}`
  const { forbiddenRedirectTo, timeoutMs: explicitTimeoutMs, ...requestOptions } = options
  const timeoutMs =
    explicitTimeoutMs ?? (isUploadBody(requestOptions.body) ? 0 : DEFAULT_REQUEST_TIMEOUT_MS)

  const defaultHeaders: Record<string, string> = {
    "Content-Type": "application/json",
    // tell the backend the user's chosen language so localized
    // API errors and emails come back in it. i18n.language is a base code
    // (pt/en/es); the backend's Accept-Language parser resolves it.
    "Accept-Language": i18n.language || "pt",
  }

  // R4-6.1: timeout PRÓPRIO combinado com o `signal` do chamador (se houver)
  // — uma requisição pendurada (proxy travado, backend catatônico) sem isto
  // nunca resolve nem rejeita, e trava `inFlightRef`/poll de quem chamou pra
  // sempre. `timeoutMs<=0` desliga (upload/export grande que já usa `fetch`
  // cru fora daqui, ou uma chamada que precise ficar pendurada de propósito).
  const timeout = timeoutMs > 0 && Number.isFinite(timeoutMs) ? createTimeoutSignal(timeoutMs) : null
  const signal = timeout ? combineSignals(requestOptions.signal, timeout.signal) : requestOptions.signal

  // R2-6.9: `headers` mesclado (default + chamador) tinha que ser a ÚLTIMA
  // propriedade do objeto — antes, `...requestOptions` vinha DEPOIS de
  // `headers` e `requestOptions` já carrega a sua PRÓPRIA chave `headers`
  // (não mesclada), então o spread final SOBRESCREVIA o merge inteiro: quem
  // passasse `headers` próprios perdia silenciosamente `Content-Type` e
  // `Accept-Language`.
  let response: Response
  try {
    response = await fetch(url, {
      credentials: "include",
      ...requestOptions,
      headers: {
        ...defaultHeaders,
        ...requestOptions.headers,
      },
      signal,
    })
  } finally {
    timeout?.clear()
  }

  if (!response.ok) {
    if (response.status === 401 && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("app-auth-expired"))
    }

    if (response.status === 403 && forbiddenRedirectTo && typeof window !== "undefined") {
      window.dispatchEvent(
        new CustomEvent("app-api-forbidden", {
          detail: { redirectTo: forbiddenRedirectTo },
        }),
      )
    }

    let errorMessage = `HTTP error! status: ${response.status}`
    let errorCode: string | undefined
    let errorDetails: Record<string, unknown> | undefined
    try {
      const errorData = await response.json()
      const structuredError = errorData?.error ?? (typeof errorData?.detail === "object" ? errorData.detail?.error : undefined)
      if (structuredError && typeof structuredError === "object") {
        if (typeof structuredError.message === "string") {
          errorMessage = structuredError.message
        }
        if (typeof structuredError.code === "string") {
          errorCode = structuredError.code
        }
        if (structuredError.details && typeof structuredError.details === "object") {
          errorDetails = structuredError.details as Record<string, unknown>
        }
      } else if (typeof errorData?.detail === "string") {
        errorMessage = errorData.detail
      } else if (Array.isArray(errorData?.detail)) {
        errorMessage = formatValidationDetail(errorData.detail) ?? JSON.stringify(errorData.detail)
      } else if (errorData?.detail) {
        errorMessage = JSON.stringify(errorData.detail)
      } else if (typeof errorData?.message === "string") {
        errorMessage = errorData.message
      }
    } catch {
      // Se não conseguir fazer parse do JSON, usar mensagem padrão
    }
    throw new ApiRequestError(errorMessage, response.status, errorCode, errorDetails)
  }

  // Handle empty responses (like DELETE)
  if (response.status === 204) {
    return {} as T
  }

  return response.json()
}
