import type React from "react"
import { forwardRef, memo, useId } from "react"
import { cn } from "@/lib/utils"

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string
  error?: string
  helperText?: string
  leftIcon?: React.ReactNode
  rightIcon?: React.ReactNode
}

const InputInner = forwardRef<HTMLInputElement, InputProps>(
  ({ className, type = "text", label, error, helperText, leftIcon, rightIcon, id, required, disabled, ...props }, ref) => {
    const generatedId = useId()
    const inputId = id || `input-${generatedId.replace(/:/g, "")}`
    const errorId = error ? `${inputId}-error` : undefined
    const helperId = helperText ? `${inputId}-helper` : undefined
    const describedBy = [errorId, !error ? helperId : undefined].filter(Boolean).join(" ") || undefined

    return (
      <div className="flex flex-col gap-1.5">
        {label && (
          <label
            htmlFor={inputId}
            className={cn(
              "text-sm font-medium text-text flex items-center gap-1.5",
              disabled && "opacity-50",
              error && "text-danger-700",
            )}
          >
            {label}
            {/* O atributo `required` no input já anuncia a obrigatoriedade; o
                asterisco é a pista visual. Rotulá-lo de novo faz o leitor de
                tela falar duas vezes (e falava em pt fixo). */}
            {/* -ml-1 cancela o gap-1.5 do label: o asterisco cola no rótulo,
                como no Select. Sem isto os dois primitivos ficavam com
                espaçamentos diferentes lado a lado no mesmo formulário. */}
            {required && <span className="-ml-1 text-danger-500" aria-hidden="true">*</span>}
          </label>
        )}

        <div className="relative">
          {leftIcon && (
            <div className="absolute left-3 top-1/2 -translate-y-1/2 text-text-tertiary pointer-events-none" aria-hidden="true">
              {leftIcon}
            </div>
          )}

          <input
            type={type}
            className={cn(
              // Campo é POÇO: `surface-tertiary` afunda em relação ao painel
              // (`surface`) em que quase sempre está. Com o mesmo tom do card,
              // o campo sumia e só a borda dizia onde clicar.
              // focus-ring: estratégia única de foco do design system.
              "w-full h-9 px-3 text-sm rounded-md border border-border-field bg-surface-tertiary text-text placeholder:text-text-tertiary",
              "transition-colors hover:border-border-field-hover focus-ring",
              "disabled:opacity-50 disabled:cursor-not-allowed",
              leftIcon && "pl-9",
              rightIcon && "pr-9",
              error && "border-danger-500",
              className,
            )}
            ref={ref}
            id={inputId}
            disabled={disabled}
            required={required}
            aria-invalid={error ? "true" : "false"}
            aria-describedby={describedBy}
            {...props}
          />

          {rightIcon && (
            <div className="absolute right-3 top-1/2 -translate-y-1/2 text-text-tertiary pointer-events-none" aria-hidden="true">
              {rightIcon}
            </div>
          )}
        </div>

        {error && (
          <div id={errorId} className="text-xs text-danger-500" role="alert" aria-live="polite">
            {error}
          </div>
        )}

        {helperText && !error && (
          <div id={helperId} className="text-xs text-text-tertiary">
            {helperText}
          </div>
        )}
      </div>
    )
  },
)

InputInner.displayName = "Input"

const Input = memo(InputInner)
Input.displayName = "Input"

export { Input }
