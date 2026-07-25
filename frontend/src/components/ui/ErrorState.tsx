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
        // gap uniforme no lugar das margens por elemento: com margem, tirar a
        // mensagem deixava um buraco e o botão precisava compensar na unha.
        "flex flex-col items-center gap-3 text-center",
        isFullPage
          ? "min-h-screen justify-center px-6 py-12"
          : "justify-center py-8 px-6",
        className,
      )}
    >
      {/* Falha É estado, então ganha a matiz de estado. Vermelhão contido:
          o alarme é o ícone, não o bloco inteiro. */}
      <div className="flex h-9 w-9 items-center justify-center rounded-full bg-danger-500/10 text-danger-500">
        <AlertTriangle className="h-5 w-5" aria-hidden="true" />
      </div>

      <h3 className={cn("font-semibold text-text", isFullPage ? "text-xl" : "text-sm")}>
        {title}
      </h3>

      {message && (
        <p className="max-w-sm text-xs leading-relaxed text-text-secondary">{message}</p>
      )}

      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          {t("errorState.retry")}
        </Button>
      )}
    </div>
  )
}

export default ErrorState
