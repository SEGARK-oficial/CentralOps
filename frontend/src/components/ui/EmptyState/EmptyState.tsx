import type React from "react"
import { cn } from "@/lib/utils"

/**
 * EmptyState — vazio é convite para agir, não lamento.
 *
 * Uma linha diz o que falta e o botão resolve. O ícone acompanha a linha em vez
 * de ocupar meia tela: ele é pontuação, não ilustração — por isso o tamanho é
 * forçado aqui, independente do `size` que o caller passar. A descrição existe
 * para o caso raro em que a linha não basta; quando ela só reafirma o título,
 * não passe.
 */

interface EmptyStateProps {
  icon?: React.ReactNode
  title: string
  description?: string
  action?: React.ReactNode
  className?: string
}

export const EmptyState: React.FC<EmptyStateProps> = ({ icon, title, description, action, className }) => (
  <div className={cn("flex flex-col items-center justify-center gap-3 px-6 py-10 text-center", className)}>
    <div className="flex items-center gap-2">
      {icon && (
        <span className="text-text-tertiary [&_svg]:h-4 [&_svg]:w-4" aria-hidden="true">
          {icon}
        </span>
      )}
      <h3 className="text-sm font-medium text-text">{title}</h3>
    </div>
    {description && <p className="max-w-sm text-xs leading-relaxed text-text-tertiary">{description}</p>}
    {action}
  </div>
)

export default EmptyState
