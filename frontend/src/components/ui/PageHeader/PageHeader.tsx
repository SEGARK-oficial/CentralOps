import type React from "react"
import { cn } from "@/lib/utils"

interface PageHeaderProps {
  /** Normalmente uma string; aceita ReactNode pro raro caso de título com
   *  badge inline (ex.: contagem "12 / 20 orgs" ao lado do nome da página). */
  title: React.ReactNode
  description?: string
  icon?: React.ReactNode
  actions?: React.ReactNode
  eyebrow?: React.ReactNode
  className?: string
}

export const PageHeader: React.FC<PageHeaderProps> = ({
  title,
  description,
  icon,
  actions,
  eyebrow,
  className,
}) => (
  <div className={cn("flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between", className)}>
    <div className="flex min-w-0 items-start gap-3">
      {/* Chip neutro: o ícone identifica a página, não sinaliza estágio nenhum.
          Violeta em todo cabeçalho seria decoração, e decoração gasta o canal
          de alarme. A elevação vem da hairline; a sombra saiu.
          A11Y-43: puramente decorativo — aria-hidden evita ruído no leitor
          de tela (o lucide 0.294 não marca isso por padrão). */}
      {icon && (
        <div
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-border bg-surface-tertiary text-text-secondary"
          aria-hidden="true"
        >
          {icon}
        </div>
      )}
      {/* LAY-25: min-w-0 deixa o flex item encolher (senão o texto empurra o
          layout) e break-words evita que um título longo estoure a coluna. */}
      <div className="min-w-0 space-y-1">
        {/* Eyebrow é rótulo de dado: mono, como no resto do painel. */}
        {eyebrow && (
          <div className="font-mono text-[11px] font-medium uppercase tracking-[0.14em] text-text-tertiary">{eyebrow}</div>
        )}
        {/* Archivo entra aqui e em número grande. Só. */}
        <h1 className="break-words font-display text-2xl font-semibold tracking-tight text-text">{title}</h1>
        {description && <p className="max-w-2xl text-sm leading-relaxed text-text-secondary">{description}</p>}
      </div>
    </div>
    {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
  </div>
)

export default PageHeader
