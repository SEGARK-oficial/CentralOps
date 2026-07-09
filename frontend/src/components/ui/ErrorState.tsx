/**
 * ErrorState — estado de erro persistente com ícone, título, mensagem e
 * botão "Tentar novamente".
 *
 * Variantes:
 *   inline    — encaixado em linha com o conteúdo (padrão)
 *   full-page — centralizado verticalmente para ocupar a tela toda
 *
 * Não some automaticamente; a ação de retry é responsabilidade do chamador.
 * Feedback de ações pontuais (toast) deve ser tratado separadamente.
 */

import type React from "react"
import { AlertTriangle } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/Button/Button"

// ── Tipos ─────────────────────────────────────────────────────────────────────

export type ErrorStateVariant = "inline" | "full-page"

export interface ErrorStateProps {
  /** Título do erro (curto, ex.: "Falha ao carregar dados"). */
  title: string
  /** Mensagem detalhada opcional. */
  message?: string
  /** Callback chamado ao clicar em "Tentar novamente". Se ausente, o botão não aparece. */
  onRetry?: () => void
  /** Variante de layout. */
  variant?: ErrorStateVariant
  className?: string
}

// ── Componente ────────────────────────────────────────────────────────────────

export const ErrorState: React.FC<ErrorStateProps> = ({
  title,
  message,
  onRetry,
  variant = "inline",
  className,
}) => {
  const { t } = useTranslation("ui")
  const isFullPage = variant === "full-page"

  return (
    <div
      role="alert"
      aria-live="assertive"
      className={cn(
        "flex flex-col items-center text-center",
        isFullPage
          ? "min-h-screen justify-center px-6 py-12"
          : "justify-center py-10 px-6",
        className,
      )}
    >
      {/* Ícone */}
      <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-danger-500/10 text-danger-500">
        <AlertTriangle className="h-6 w-6" aria-hidden="true" />
      </div>

      {/* Título */}
      <h3
        className={cn(
          "font-semibold text-text",
          isFullPage ? "text-xl mb-2" : "text-base mb-1",
        )}
      >
        {title}
      </h3>

      {/* Mensagem */}
      {message && (
        <p className="text-sm text-text-secondary max-w-sm mb-6">{message}</p>
      )}

      {/* Botão de retry */}
      {onRetry && (
        <Button
          variant="outline"
          size="sm"
          onClick={onRetry}
          className={message ? "" : "mt-4"}
        >
          {t("errorState.retry")}
        </Button>
      )}
    </div>
  )
}

export default ErrorState
