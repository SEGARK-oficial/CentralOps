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
  <div className={cn("flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between", className)}>
    <div className="flex items-start gap-4">
      {icon && (
        <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-primary-50 text-primary-700 shadow-sm ring-1 ring-primary-100">
          {icon}
        </div>
      )}
      <div className="space-y-1">
        {eyebrow && <div className="text-xs font-semibold uppercase tracking-[0.2em] text-text-tertiary">{eyebrow}</div>}
        <h1 className="text-2xl font-bold tracking-tight text-text">{title}</h1>
        {description && <p className="max-w-3xl text-sm leading-relaxed text-text-secondary">{description}</p>}
      </div>
    </div>
    {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
  </div>
)

export default PageHeader
