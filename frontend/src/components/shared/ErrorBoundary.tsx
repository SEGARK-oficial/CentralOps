"use client"

import { Component, type ErrorInfo, type ReactNode } from "react"
import { AlertTriangleIcon, RefreshCwIcon } from "lucide-react"
import { Button } from "@/components/ui/Button/Button"

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

  render(): ReactNode {
    const { error } = this.state
    const { children, fallback, variant = "inline" } = this.props

    if (!error) return children
    if (fallback) return fallback(error, this.reset)

    const isPage = variant === "page"

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
        <h2 className="mt-4 text-lg font-semibold text-text">Algo deu errado</h2>
        <p className="mt-1 max-w-md text-sm text-text-secondary">
          Encontramos um erro inesperado ao renderizar esta área. Você pode tentar novamente ou recarregar a página.
        </p>
        {import.meta.env?.DEV && (
          <pre className="mt-4 max-w-lg overflow-auto rounded-md bg-surface-tertiary p-3 text-left text-xs text-danger-700">
            {error.message}
          </pre>
        )}
        <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
          <Button variant="primary" size="sm" onClick={this.reset} leftIcon={<RefreshCwIcon size={14} />}>
            Tentar novamente
          </Button>
          <Button variant="outline" size="sm" onClick={() => window.location.reload()}>
            Recarregar página
          </Button>
        </div>
      </div>
    )
  }
}

export default ErrorBoundary
