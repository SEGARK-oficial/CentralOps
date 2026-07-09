import type React from "react"
import { AlertCircleIcon, AlertTriangleIcon, CheckCircleIcon, InfoIcon } from "lucide-react"
import { cn } from "@/lib/utils"

type NoticeVariant = "info" | "success" | "warning" | "danger"

interface NoticeProps extends React.HTMLAttributes<HTMLDivElement> {
  variant?: NoticeVariant
  title?: React.ReactNode
  icon?: React.ReactNode
  action?: React.ReactNode
}

const variantStyles: Record<NoticeVariant, { wrapper: string; icon: React.ReactNode }> = {
  info: {
    wrapper: "border border-primary-200 bg-primary-50 text-primary-800",
    icon: <InfoIcon size={16} />,
  },
  success: {
    wrapper: "border border-success-200 bg-success-50 text-success-800",
    icon: <CheckCircleIcon size={16} />,
  },
  warning: {
    wrapper: "border border-warning-200 bg-warning-50 text-warning-800",
    icon: <AlertTriangleIcon size={16} />,
  },
  danger: {
    wrapper: "border border-danger-200 bg-danger-50 text-danger-800",
    icon: <AlertCircleIcon size={16} />,
  },
}

export const Notice: React.FC<NoticeProps> = ({
  className,
  variant = "info",
  title,
  icon,
  action,
  children,
  ...props
}) => {
  // Erros/avisos precisam interromper o leitor de tela (assertive); info/sucesso
  // são apenas informativos (polite). Sem prop role explícita, deriva da variante.
  const isUrgent = variant === "danger" || variant === "warning"
  const role = isUrgent ? "alert" : "status"
  const ariaLive = isUrgent ? "assertive" : "polite"

  return (
  <div
    className={cn("flex items-start gap-3 rounded-lg px-4 py-3 text-sm", variantStyles[variant].wrapper, className)}
    // role/aria-live derivam da variante; props (se passar role/aria-live) sobrescrevem.
    role={role}
    aria-live={ariaLive}
    {...props}
  >
    <div className="mt-0.5 shrink-0" aria-hidden="true">
      {icon ?? variantStyles[variant].icon}
    </div>
    <div className="min-w-0 flex-1">
      {title && <div className="font-semibold">{title}</div>}
      {children && <div className={cn(title && "mt-1", "leading-relaxed")}>{children}</div>}
    </div>
    {action && <div className="shrink-0">{action}</div>}
  </div>
  )
}

export default Notice
