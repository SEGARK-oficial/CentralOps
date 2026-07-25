import type React from "react"
import { forwardRef, useId } from "react"
import { cn } from "@/lib/utils"

export interface TextareaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  label?: string
  error?: string
  helperText?: string
}

const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, label, error, helperText, id, required, disabled, rows = 5, ...props }, ref) => {
    const generatedId = useId()
    const textareaId = id || `textarea-${generatedId.replace(/:/g, "")}`
    const errorId = error ? `${textareaId}-error` : undefined
    const helperId = helperText ? `${textareaId}-helper` : undefined
    const describedBy = [errorId, !error ? helperId : undefined].filter(Boolean).join(" ") || undefined

    return (
      <div className="flex flex-col gap-1.5">
        {label && (
          <label
            htmlFor={textareaId}
            className={cn("text-sm font-medium text-text", disabled && "opacity-50", error && "text-danger-700")}
          >
            {label}
            {/* `required` no textarea já anuncia; o asterisco é só a pista visual. */}
            {required && <span className="ml-0.5 text-danger-500" aria-hidden="true">*</span>}
          </label>
        )}

        <textarea
          ref={ref}
          id={textareaId}
          rows={rows}
          required={required}
          disabled={disabled}
          aria-invalid={error ? "true" : "false"}
          aria-describedby={describedBy}
          className={cn(
            // Mesmo poço do Input, e o MESMO foco: este era o único primitivo
            // que ainda tinha estratégia própria (border + ring) enquanto o
            // resto usa `focus-ring`. Duas caras de foco no mesmo formulário.
            "w-full rounded-md border border-border-field bg-surface-tertiary px-3 py-2 text-sm text-text placeholder:text-text-tertiary",
            "transition-colors hover:border-border-field-hover focus-ring",
            "disabled:cursor-not-allowed disabled:opacity-50",
            error && "border-danger-500",
            className,
          )}
          {...props}
        />

        {error ? (
          <div id={errorId} className="text-xs text-danger-500" role="alert">
            {error}
          </div>
        ) : helperText ? (
          <div id={helperId} className="text-xs text-text-tertiary">
            {helperText}
          </div>
        ) : null}
      </div>
    )
  },
)

Textarea.displayName = "Textarea"

export { Textarea }
export default Textarea
