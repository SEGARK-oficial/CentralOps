import type React from "react"
import { forwardRef } from "react"
import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"

// touch-target: em viewport de toque (pointer:coarse), expande padding vertical
// para garantir área mínima de 44px sem alterar a altura visual em desktop (data-dense).
const TOUCH_TARGET_XS = "[@media(pointer:coarse)]:py-[8px]" // h-7 (28px) + 2×8 = 44px
const TOUCH_TARGET_SM = "[@media(pointer:coarse)]:py-[6px]" // h-8 (32px) + 2×6 = 44px

const buttonVariants = cva(
  // focus-ring: estratégia única de foco do design system (globals.css:288-295).
  // Remove variantes ad-hoc anteriores (focus-visible:outline-* / focus-visible:ring-*).
  "inline-flex items-center justify-center gap-2 font-medium transition-colors rounded-md cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed focus-ring",
  {
    variants: {
      variant: {
        primary: "bg-primary-600 text-white hover:bg-primary-700 active:bg-primary-800",
        secondary: "bg-surface-tertiary text-text hover:bg-border active:bg-border-hover",
        outline: "border border-border text-text hover:bg-surface-tertiary active:bg-border/50",
        ghost: "text-text-secondary hover:bg-surface-tertiary hover:text-text active:bg-border/50",
        danger: "bg-danger-500 text-white hover:bg-danger-700 active:bg-danger-700/90",
      },
      size: {
        xs: `h-7 px-2 text-xs rounded ${TOUCH_TARGET_XS}`,
        sm: `h-8 px-3 text-sm ${TOUCH_TARGET_SM}`,
        md: "h-9 px-4 text-sm",
        lg: "h-11 px-6 text-base",
      },
    },
    defaultVariants: {
      variant: "primary",
      size: "md",
    },
  },
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
  loading?: boolean
  leftIcon?: React.ReactNode
  rightIcon?: React.ReactNode
}

const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, loading = false, leftIcon, rightIcon, children, disabled, type, ...props }, ref) => {
    const { t } = useTranslation("ui")
    const Comp = asChild ? Slot : "button"

    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        disabled={disabled || loading}
        aria-disabled={disabled || loading}
        type={!asChild ? (type ?? "button") : undefined}
        {...props}
      >
        {loading && (
          <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" strokeDasharray="32" strokeDashoffset="32" />
          </svg>
        )}
        {leftIcon && !loading && <span aria-hidden="true">{leftIcon}</span>}
        <span className={loading ? "opacity-0" : ""}>{children}</span>
        {rightIcon && !loading && <span aria-hidden="true">{rightIcon}</span>}
        {loading && <span className="sr-only">{t("button.loading")}</span>}
      </Comp>
    )
  },
)

Button.displayName = "Button"

export { Button, buttonVariants }
