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
  /**
   * A11Y-33: o `<h3>` fixo furava a hierarquia de headings sempre que o
   * EmptyState aparecia sem um `<h2>` por perto (ex.: direto sob o `<h1>`
   * da página). Padrão `3` preserva o comportamento atual.
   */
  headingLevel?: 2 | 3 | 4
}

export const EmptyState: React.FC<EmptyStateProps> = ({ icon, title, description, action, className, headingLevel = 3 }) => {
  const Heading = `h${headingLevel}` as "h2" | "h3" | "h4"
  return (
    <div className={cn("flex flex-col items-center justify-center gap-3 px-6 py-10 text-center", className)}>
      <div className="flex items-center gap-2">
        {icon && (
          <span className="text-text-tertiary [&_svg]:h-4 [&_svg]:w-4" aria-hidden="true">
            {icon}
          </span>
        )}
        <Heading className="text-sm font-medium text-text">{title}</Heading>
      </div>
      {description && <p className="max-w-sm text-xs leading-relaxed text-text-tertiary">{description}</p>}
      {action}
    </div>
  )
}

export default EmptyState
