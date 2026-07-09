import type React from "react"
import { cn } from "@/lib/utils"

interface EmptyStateProps {
  icon?: React.ReactNode
  title: string
  description?: string
  action?: React.ReactNode
  className?: string
}

export const EmptyState: React.FC<EmptyStateProps> = ({ icon, title, description, action, className }) => (
  <div className={cn("flex flex-col items-center justify-center py-12 px-6 text-center", className)}>
    {icon && <div className="text-text-tertiary/40 mb-4">{icon}</div>}
    <h3 className="text-base font-semibold text-text mb-1">{title}</h3>
    {description && <p className="text-sm text-text-secondary max-w-sm">{description}</p>}
    {action && <div className="mt-4">{action}</div>}
  </div>
)

export default EmptyState
