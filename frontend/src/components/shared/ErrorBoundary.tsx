"use client"

import { Component, type ErrorInfo, type ReactNode } from "react"
import { AlertTriangleIcon, RefreshCwIcon } from "lucide-react"
import { Button } from "@/components/ui/Button/Button"
import i18n from "@/i18n"

/**
 * R2-6.5: `import()` de um chunk lazy (`React.lazy`) que falha (deploy novo
 * trocou os hashes dos arquivos enquanto a aba ainda está com o `index.html`
 * antigo em memória, ou a rede caiu no meio do fetch) deixa a promise
 * REJEITADA cacheada dentro do `React.lazy` — remontar a subárvore
 * (`this.reset`) reusa a MESMA promise já rejeitada e falha de novo, na
 * hora, em loop. Só um reload de página de verdade força um novo `import()`
 * com os chunks atuais.
 */
function isChunkLoadError(error: Error): boolean {
  return /(fetch|load(ing)?).{0,40}(dynamically imported module|chunk)/i.test(error.message)
}

interface ErrorBoundaryProps {
  children: ReactNode
  /** Render alternativo. Recebe o erro e um `reset` para tentar remontar a subárvore. */
  fallback?: (error: Error, reset: () => void) => ReactNode
  /** "page" ocupa a área toda; "inline" preserva o shell ao redor. */
  variant?: "page" | "inline"
  /**
   * Quando muda, o boundary se reseta automaticamente. Use a rota atual aqui
   * para que navegar para longe de uma página quebrada limpe o erro.
   */
  resetKey?: string
}

interface ErrorBoundaryState {
  error: Error | null
  prevResetKey?: string
}

/**
 * Captura exceções de render na subárvore e exibe um fallback acionável em vez
 * de derrubar o app inteiro com tela branca (React 18 desmonta a árvore a partir
 * da raiz em erro não capturado). Usado em dois níveis no App: global e por-rota.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null, prevResetKey: this.props.resetKey }

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { error }
  }

  // Reseta o erro quando a `resetKey` muda (ex.: nova rota), sem precisar de F5.
  static getDerivedStateFromProps(
    props: ErrorBoundaryProps,
    state: ErrorBoundaryState,
  ): Partial<ErrorBoundaryState> | null {
    if (props.resetKey !== state.prevResetKey) {
      return { error: null, prevResetKey: props.resetKey }
    }
    return null
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Ponto de integração com observabilidade (Sentry/Grafana) no futuro.
    console.error("[ErrorBoundary]", error, info.componentStack)
  }

  private reset = (): void => {
    this.setState({ error: null })
  }

  // R2-6.5: erro de import() de chunk → "Tentar novamente" recarrega a
  // página de verdade (ver `isChunkLoadError`), em vez de só resetar o
  // estado local e bater na mesma promise rejeitada cacheada pelo
  // `React.lazy`.
  private handlePrimaryRetry = (): void => {
    const { error } = this.state
    if (error && isChunkLoadError(error)) {
      window.location.reload()
      return
    }
    this.reset()
  }

  render(): ReactNode {
    const { error } = this.state
    const { children, fallback, variant = "inline" } = this.props

    if (!error) return children
    if (fallback) return fallback(error, this.reset)

    const isPage = variant === "page"
    const chunkError = isChunkLoadError(error)
    // i18n.t() direto (não o hook `useTranslation`) porque isto é um
    // componente de CLASSE — e mesmo que fosse função, um error boundary não
    // pode depender do Suspense do i18n lazy-loading (R2-5.1): se ele
    // suspendesse esperando um namespace, um erro de render viraria uma
    // segunda tela em branco. `common` está em `SHELL_NAMESPACES` — sempre
    // carregado antes do 1º render (`i18nReady` em main.tsx), nunca suspende.
    const t = (key: string, opts?: Record<string, unknown>) => i18n.t(key, { ns: "common", ...opts })

    return (
      <div
        role="alert"
        className={
          isPage
            ? "flex min-h-screen flex-col items-center justify-center bg-surface-secondary px-6"
            : "flex flex-col items-center justify-center rounded-lg border border-border bg-surface px-6 py-16 text-center"
        }
      >
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-danger-50 text-danger-600" aria-hidden="true">
          <AlertTriangleIcon size={24} />
        </div>
        <h2 className="mt-4 text-lg font-semibold text-text">{t("errorBoundary.title")}</h2>
        <p className="mt-1 max-w-md text-sm text-text-secondary">
          {chunkError ? t("errorBoundary.chunkErrorDescription") : t("errorBoundary.description")}
        </p>
        {import.meta.env?.DEV && (
          <pre className="mt-4 max-w-lg overflow-auto rounded-md bg-surface-tertiary p-3 text-left text-xs text-danger-700">
            {error.message}
          </pre>
        )}
        <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
          <Button variant="primary" size="sm" onClick={this.handlePrimaryRetry} leftIcon={<RefreshCwIcon size={14} />}>
            {chunkError ? t("errorBoundary.reload") : t("errorBoundary.retry")}
          </Button>
          {!chunkError && (
            <Button variant="outline" size="sm" onClick={() => window.location.reload()}>
              {t("errorBoundary.reload")}
            </Button>
          )}
        </div>
      </div>
    )
  }
}

export default ErrorBoundary
