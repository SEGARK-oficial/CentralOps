"use client"

import type React from "react"
import { useEffect, useId, useMemo, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { ChevronDownIcon, CheckIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"
import { getPortalPosition } from "@/lib/portal-positioning"

export interface SelectOption {
  value: string | number
  label: string
  disabled?: boolean
}

export type SelectValue = string | number | Array<string | number>

interface SelectProps {
  id?: string
  name?: string
  label?: string
  required?: boolean
  options: SelectOption[]
  value?: SelectValue
  placeholder?: string
  multiple?: boolean
  disabled?: boolean
  /** Altura/tipografia do trigger. "sm" (h-8/text-xs) para toolbars densas; "md" (h-9/text-sm) padrão. */
  size?: "sm" | "md"
  error?: string
  helperText?: string
  leftIcon?: React.ReactNode
  className?: string
  onChange?: (value: SelectValue) => void
  onValueChange?: (value: SelectValue) => void
  onBlur?: () => void
  "aria-label"?: string
  "aria-describedby"?: string
  "data-testid"?: string
}

export const Select: React.FC<SelectProps> = ({
  id,
  name,
  label,
  required = false,
  options,
  value,
  placeholder,
  multiple = false,
  disabled = false,
  size = "md",
  error,
  helperText,
  leftIcon,
  className,
  onChange,
  onValueChange,
  onBlur,
  "aria-label": ariaLabel,
  "aria-describedby": ariaDescribedBy,
  "data-testid": dataTestId,
}) => {
  const { t } = useTranslation("ui")
  const resolvedPlaceholder = placeholder ?? t("select.placeholder")
  const [isOpen, setIsOpen] = useState(false)
  const [searchTerm, setSearchTerm] = useState("")
  const [portalStyle, setPortalStyle] = useState<React.CSSProperties>({})
  const selectRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const portalRef = useRef<HTMLDivElement>(null)
  const generatedId = useId()

  const selectId = id || `select-${generatedId.replace(/:/g, "")}`
  const listboxId = `${selectId}-listbox`
  const errorId = error ? `${selectId}-error` : undefined
  const helperId = helperText ? `${selectId}-helper` : undefined
  const describedBy = [ariaDescribedBy, errorId, !error ? helperId : undefined].filter(Boolean).join(" ") || undefined

  const selectedValues = Array.isArray(value) ? value : value !== undefined && value !== "" ? [value] : []

  const filteredOptions = useMemo(
    () => options.filter((option) => option.label.toLowerCase().includes(searchTerm.toLowerCase())),
    [options, searchTerm],
  )
  const selectableValues = useMemo(
    () => options.filter((option) => !option.disabled).map((option) => option.value),
    [options],
  )
  const allSelected = multiple && selectableValues.length > 0 && selectableValues.every((v) => selectedValues.includes(v))

  const getDisplayValue = () => {
    if (selectedValues.length === 0) return resolvedPlaceholder
    if (multiple) {
      if (selectedValues.length === 1) {
        return options.find((opt) => opt.value === selectedValues[0])?.label || ""
      }
      return t("select.selectedCount", { count: selectedValues.length })
    }
    return options.find((opt) => opt.value === selectedValues[0])?.label || ""
  }

  const emitChange = (nextValue: SelectValue) => {
    onChange?.(nextValue)
    onValueChange?.(nextValue)
  }

  const handleOptionClick = (optionValue: string | number) => {
    if (multiple) {
      const newValues = selectedValues.includes(optionValue)
        ? selectedValues.filter((v) => v !== optionValue)
        : [...selectedValues, optionValue]
      emitChange(newValues)
    } else {
      emitChange(optionValue)
      setIsOpen(false)
      triggerRef.current?.focus()
    }
  }

  const handleSelectAll = () => {
    if (!multiple) return
    emitChange(selectableValues)
  }

  const handleClearAll = () => {
    if (!multiple) return
    emitChange([])
  }

  const handleTriggerKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (disabled) return
    if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault()
      setIsOpen(true)
    }
    if (event.key === "Escape") {
      event.preventDefault()
      setIsOpen(false)
    }
  }

  const handleOptionKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>, optionIndex: number) => {
    const optionButtons = portalRef.current?.querySelectorAll<HTMLButtonElement>("button[role='option']:not(:disabled)")
    if (!optionButtons || optionButtons.length === 0) return

    if (event.key === "ArrowDown") {
      event.preventDefault()
      optionButtons[Math.min(optionIndex + 1, optionButtons.length - 1)]?.focus()
    } else if (event.key === "ArrowUp") {
      event.preventDefault()
      optionButtons[Math.max(optionIndex - 1, 0)]?.focus()
    } else if (event.key === "Home") {
      event.preventDefault()
      optionButtons[0]?.focus()
    } else if (event.key === "End") {
      event.preventDefault()
      optionButtons[optionButtons.length - 1]?.focus()
    } else if (event.key === "Escape") {
      event.preventDefault()
      setIsOpen(false)
      triggerRef.current?.focus()
    }
  }

  // Click-outside: fecha se o clique não for nem no trigger/wrapper nem no portal
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      const target = event.target as Node
      const inTrigger = selectRef.current?.contains(target) ?? false
      const inPortal = portalRef.current?.contains(target) ?? false
      if (!inTrigger && !inPortal) {
        setIsOpen(false)
        onBlur?.()
      }
    }
    document.addEventListener("mousedown", handleClickOutside)
    return () => document.removeEventListener("mousedown", handleClickOutside)
  }, [onBlur])

  // Posicionamento do portal: recalcula ao abrir, scroll, resize
  useEffect(() => {
    if (!isOpen || !triggerRef.current) return

    const updatePosition = () => {
      if (!triggerRef.current) return
      // Estimativa da altura do dropdown para flip: 300px max (max-h-60 = 15rem)
      const ESTIMATED_HEIGHT = 300
      const pos = getPortalPosition(triggerRef.current, ESTIMATED_HEIGHT)
      setPortalStyle({
        position: "fixed",
        top: pos.top,
        left: pos.left,
        width: pos.width,
        // popover (1060) > modal (1050): a lista abre NA FRENTE quando o Select
        // está dentro de um Modal (ambos são portais irmãos no body).
        zIndex: "var(--z-index-popover)",
      })
    }

    updatePosition()

    // Fechar em scroll — mas ignorar scrolls dentro do próprio portal
    // (lista de opções tem max-h-60 overflow-auto e precisa rolar internamente).
    const handleScroll = (event: Event) => {
      const target = event.target as Node | null
      if (target && portalRef.current?.contains(target)) return
      setIsOpen(false)
    }
    window.addEventListener("scroll", handleScroll, { passive: true, capture: true })
    window.addEventListener("resize", updatePosition, { passive: true })
    return () => {
      window.removeEventListener("scroll", handleScroll, { capture: true })
      window.removeEventListener("resize", updatePosition)
    }
  }, [isOpen])

  // Foco inicial ao abrir o dropdown
  // O createPortal é síncrono mas o ref é preenchido após o commit do React,
  // então usamos um microtask (setTimeout 0) para garantir que o DOM está pronto.
  useEffect(() => {
    if (!isOpen) return
    const id = setTimeout(() => {
      if (options.length > 10) {
        inputRef.current?.focus()
        return
      }
      const selectedIndex = filteredOptions.findIndex((option) => selectedValues.includes(option.value))
      const optionButtons = portalRef.current?.querySelectorAll<HTMLButtonElement>("button[role='option']:not(:disabled)")
      if (!optionButtons || optionButtons.length === 0) return
      optionButtons[selectedIndex >= 0 ? selectedIndex : 0]?.focus()
    }, 0)
    return () => clearTimeout(id)
  }, [filteredOptions, isOpen, options.length, selectedValues])

  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      {label && (
        <label
          htmlFor={selectId}
          className={cn(
            "text-sm font-medium text-text",
            error && "text-danger-700",
            disabled && "opacity-50",
          )}
        >
          {label}
          {required && <span className="text-danger-500 ml-0.5" aria-hidden="true">*</span>}
        </label>
      )}

      <div ref={selectRef} className="relative">
        <button
          ref={triggerRef}
          type="button"
          id={selectId}
          name={name}
          className={cn(
            // focus-ring: estratégia única de foco do design system.
            "w-full flex items-center gap-2 rounded-md border bg-surface text-left transition-colors focus-ring",
            size === "sm" ? "h-8 px-2.5 text-xs" : "h-9 px-3 text-sm",
            "disabled:opacity-50 disabled:cursor-not-allowed",
            error ? "border-danger-500" : "border-border hover:border-border-hover",
            leftIcon && (size === "sm" ? "pl-8" : "pl-9"),
          )}
          onClick={() => !disabled && setIsOpen((prev) => !prev)}
          onKeyDown={handleTriggerKeyDown}
          disabled={disabled}
          aria-invalid={error ? "true" : "false"}
          aria-expanded={isOpen}
          aria-haspopup="listbox"
          aria-controls={isOpen ? listboxId : undefined}
          aria-label={ariaLabel}
          aria-describedby={describedBy}
          data-testid={dataTestId}
        >
          {leftIcon && (
            <span className="absolute left-3 top-1/2 -translate-y-1/2 text-text-tertiary" aria-hidden="true">
              {leftIcon}
            </span>
          )}
          <span className={cn("flex-1 truncate", selectedValues.length === 0 && "text-text-tertiary")}>
            {getDisplayValue()}
          </span>
          <ChevronDownIcon size={16} className={cn("text-text-tertiary shrink-0 transition-transform", isOpen && "rotate-180")} />
        </button>

        {isOpen && typeof document !== "undefined" && createPortal(
          <div
            ref={portalRef}
            style={portalStyle}
            className="bg-surface border border-border rounded-md shadow-lg animate-slide-down overflow-hidden"
          >
            {options.length > 10 && (
              <div className="p-2 border-b border-border">
                <input
                  ref={inputRef}
                  type="text"
                  placeholder={t("select.searchPlaceholder")}
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  className="w-full h-8 px-3 text-sm rounded border border-border bg-surface-secondary focus-ring"
                  aria-label={t("select.searchAriaLabel")}
                />
              </div>
            )}

            {multiple && (
              <div className="flex gap-2 px-3 py-2 border-b border-border text-xs">
                <button
                  type="button"
                  className="text-primary-600 hover:underline disabled:opacity-50"
                  onClick={handleSelectAll}
                  disabled={selectableValues.length === 0 || allSelected}
                >
                  {t("select.selectAll")}
                </button>
                <button
                  type="button"
                  className="text-primary-600 hover:underline disabled:opacity-50"
                  onClick={handleClearAll}
                  disabled={selectedValues.length === 0}
                >
                  {t("select.clearAll")}
                </button>
              </div>
            )}

            <ul className="max-h-60 overflow-y-auto scrollbar-thin py-1" role="listbox" id={listboxId} aria-multiselectable={multiple || undefined}>
              {filteredOptions.length === 0 ? (
                <li className="px-3 py-2 text-sm text-text-tertiary text-center">{t("select.noOptionsFound")}</li>
              ) : (
                filteredOptions.map((option, idx) => {
                  const isSelected = selectedValues.includes(option.value)
                  return (
                    <li key={option.value}>
                      <button
                        type="button"
                        className={cn(
                          // focus-ring: estratégia única; mantém bg de foco para feedback visual do item.
                          "w-full flex items-center gap-2 px-3 py-2 text-sm text-left transition-colors focus-ring",
                          "hover:bg-surface-tertiary focus-visible:bg-surface-tertiary",
                          isSelected && "bg-primary-50 text-primary-700 font-medium",
                          option.disabled && "opacity-50 cursor-not-allowed",
                        )}
                        onClick={() => !option.disabled && handleOptionClick(option.value)}
                        onKeyDown={(e) => handleOptionKeyDown(e, idx)}
                        role="option"
                        aria-selected={isSelected}
                        disabled={option.disabled}
                      >
                        <span className="flex-1 truncate">{option.label}</span>
                        {isSelected && <CheckIcon size={16} className="shrink-0 text-primary-600" />}
                      </button>
                    </li>
                  )
                })
              )}
            </ul>
          </div>,
          document.body,
        )}
      </div>

      {error ? (
        <div id={errorId} className="text-xs text-danger-500" role="alert">{error}</div>
      ) : helperText ? (
        <div id={helperId} className="text-xs text-text-tertiary">{helperText}</div>
      ) : null}
    </div>
  )
}

export default Select
