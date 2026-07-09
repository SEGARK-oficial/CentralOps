"use client"

import type React from "react"
import { forwardRef, useEffect, useId, useRef } from "react"
import { CheckIcon, MinusIcon } from "lucide-react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

// Nota: focus-ring é aplicado ao <span> wrapper via focus-within para que o
// anel apareça em volta do quadrado visual quando o <input> interno recebe foco.
// A utility focus-ring usa :focus-visible internamente, mas aqui precisamos de
// :focus-within (foco num filho → ring no pai visual). Alinhado via outline no span.
const checkboxVariants = cva(
  "relative inline-flex shrink-0 items-center justify-center rounded border transition-colors cursor-pointer " +
    "[&:has(:focus-visible)]:outline [&:has(:focus-visible)]:outline-2 [&:has(:focus-visible)]:outline-primary-500 [&:has(:focus-visible)]:outline-offset-2",
  {
    variants: {
      size: {
        sm: "h-4 w-4",
        md: "h-5 w-5",
      },
      state: {
        unchecked: "border-border bg-surface hover:border-border-hover",
        checked: "border-primary-600 bg-primary-600 text-white",
        indeterminate: "border-primary-600 bg-primary-600 text-white",
        disabled: "border-border bg-surface-tertiary cursor-not-allowed opacity-60",
      },
    },
    defaultVariants: {
      size: "md",
      state: "unchecked",
    },
  },
)

export interface CheckboxProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "size" | "type">,
    Omit<VariantProps<typeof checkboxVariants>, "state"> {
  /** Tri-state: when `true`, takes precedence over `checked` for visual rendering. */
  indeterminate?: boolean
  /** Visible label associated with the checkbox. If omitted, supply `aria-label`. */
  label?: React.ReactNode
  /** Optional helper text rendered below the label. */
  description?: React.ReactNode
  /** Visual error state (red border + optional message). */
  error?: string
  /** When true, label is rendered visually hidden but kept for screen readers. */
  hideLabel?: boolean
}

/**
 * Acessível, controlado, suporta indeterminate visual + a11y.
 * - `indeterminate=true` -> renderiza traço (—) e expõe `aria-checked="mixed"`.
 * - `indeterminate` é aplicado ao DOM via ref (HTMLInputElement.indeterminate).
 */
const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(
  (
    {
      className,
      id,
      size,
      indeterminate = false,
      checked,
      defaultChecked,
      disabled,
      label,
      description,
      error,
      hideLabel,
      onChange,
      "aria-label": ariaLabel,
      "aria-describedby": ariaDescribedBy,
      ...rest
    },
    forwardedRef,
  ) => {
    const generatedId = useId()
    const inputId = id ?? `checkbox-${generatedId.replace(/:/g, "")}`
    const descriptionId = description ? `${inputId}-description` : undefined
    const errorId = error ? `${inputId}-error` : undefined
    const describedBy = [ariaDescribedBy, descriptionId, errorId].filter(Boolean).join(" ") || undefined

    // Bridge externalRef + internalRef para aplicar `indeterminate` no DOM.
    const internalRef = useRef<HTMLInputElement | null>(null)
    useEffect(() => {
      if (internalRef.current) {
        internalRef.current.indeterminate = indeterminate
      }
    }, [indeterminate])

    const setRefs = (node: HTMLInputElement | null) => {
      internalRef.current = node
      if (typeof forwardedRef === "function") forwardedRef(node)
      else if (forwardedRef) (forwardedRef as React.MutableRefObject<HTMLInputElement | null>).current = node
    }

    const visualState: VariantProps<typeof checkboxVariants>["state"] = disabled
      ? "disabled"
      : indeterminate
        ? "indeterminate"
        : checked || defaultChecked
          ? "checked"
          : "unchecked"

    const iconSize = size === "sm" ? 12 : 14

    return (
      <div className={cn("inline-flex items-start gap-2", className)}>
        <span className={checkboxVariants({ size, state: visualState })}>
          <input
            ref={setRefs}
            id={inputId}
            type="checkbox"
            checked={checked}
            defaultChecked={defaultChecked}
            disabled={disabled}
            onChange={onChange}
            // Cobre toda a área do quadrado para que o clique/foco funcione,
            // mas mantemos opacity-0 — a UI é desenhada em volta.
            className="absolute inset-0 h-full w-full cursor-inherit opacity-0 disabled:cursor-not-allowed"
            aria-checked={indeterminate ? "mixed" : checked ? "true" : "false"}
            aria-label={!label && ariaLabel ? ariaLabel : undefined}
            aria-describedby={describedBy}
            aria-invalid={error ? "true" : undefined}
            {...rest}
          />
          {/* Ícones decorativos — leitura via aria-checked, não duplicar texto. */}
          {indeterminate ? (
            <MinusIcon size={iconSize} aria-hidden="true" className="pointer-events-none" />
          ) : checked || defaultChecked ? (
            <CheckIcon size={iconSize} aria-hidden="true" className="pointer-events-none" strokeWidth={3} />
          ) : null}
        </span>

        {label && (
          <label
            htmlFor={inputId}
            className={cn(
              "flex flex-col gap-0.5 text-sm leading-tight",
              hideLabel && "sr-only",
              disabled ? "text-text-tertiary" : "text-text",
              !disabled && "cursor-pointer",
            )}
          >
            <span>{label}</span>
            {description && (
              <span id={descriptionId} className="text-xs text-text-secondary">
                {description}
              </span>
            )}
            {error && (
              <span id={errorId} className="text-xs text-danger-500" role="alert">
                {error}
              </span>
            )}
          </label>
        )}
      </div>
    )
  },
)

Checkbox.displayName = "Checkbox"

export { Checkbox, checkboxVariants }
export default Checkbox
