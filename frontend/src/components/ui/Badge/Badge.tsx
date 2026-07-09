import type React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

const badgeVariants = cva(
  "inline-flex items-center gap-1 font-medium rounded-full whitespace-nowrap",
  {
    variants: {
      variant: {
        default: "bg-surface-tertiary text-text-secondary",
        primary: "bg-primary-100 text-primary-700",
        success: "bg-success-50 text-success-700",
        warning: "bg-warning-50 text-warning-700",
        danger: "bg-danger-50 text-danger-700",
        outline: "border border-border text-text-secondary",
      },
      size: {
        sm: "px-2 py-0.5 text-xs",
        md: "px-2.5 py-0.5 text-xs",
        lg: "px-3 py-1 text-sm",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "md",
    },
  },
)

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {
  dot?: boolean
  dotColor?: string
}

export const Badge: React.FC<BadgeProps> = ({ className, variant, size, dot, dotColor, children, ...props }) => (
  <span className={cn(badgeVariants({ variant, size }), className)} {...props}>
    {dot && (
      <span
        className="w-1.5 h-1.5 rounded-full"
        style={{ backgroundColor: dotColor || "currentColor" }}
        aria-hidden="true"
      />
    )}
    {children}
  </span>
)
