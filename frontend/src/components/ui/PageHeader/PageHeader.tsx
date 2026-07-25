import type React from "react"
import { cn } from "@/lib/utils"

interface PageHeaderProps {
  title: string
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
    <div className="flex items-start gap-3">
      {/* Chip neutro: o ícone identifica a página, não sinaliza estágio nenhum.
          Violeta em todo cabeçalho seria decoração, e decoração gasta o canal
          de alarme. A elevação vem da hairline; a sombra saiu. */}
      {icon && (
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-border bg-surface-tertiary text-text-secondary">
          {icon}
        </div>
      )}
      <div className="space-y-1">
        {/* Eyebrow é rótulo de dado: mono, como no resto do painel. */}
        {eyebrow && (
          <div className="font-mono text-[11px] font-medium uppercase tracking-[0.14em] text-text-tertiary">{eyebrow}</div>
        )}
        {/* Archivo entra aqui e em número grande. Só. */}
        <h1 className="font-display text-2xl font-semibold tracking-tight text-text">{title}</h1>
        {description && <p className="max-w-2xl text-sm leading-relaxed text-text-secondary">{description}</p>}
      </div>
    </div>
    {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
  </div>
)

export default PageHeader
