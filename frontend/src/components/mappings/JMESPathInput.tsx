/**
 * JMESPathInput
 * Input com autocomplete de campos JMESPath descobertos pelo backend.
 *
 * Quando `suggestions` está vazio (backend ainda não coletou eventos ou
 * falhou) o componente se comporta como um <Input> texto livre normal —
 * sem dropdown, sem nenhuma mensagem de erro extra.
 *
 * Acessibilidade: usa padrão combobox/listbox (ARIA 1.2).
 *   - role="combobox" no wrapper
 *   - aria-expanded / aria-controls / aria-activedescendant no input
 *   - role="listbox" + role="option" na lista de sugestões
 *   - Navegação por teclado: ArrowDown/Up, Enter, Escape
 */

import type React from "react"
import { memo, useState, useRef, useCallback, useId, useEffect } from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"

interface JMESPathInputProps {
  id?: string
  value: string
  onChange: (value: string) => void
  suggestions: string[]
  placeholder?: string
  className?: string
}

const JMESPathInputInner: React.FC<JMESPathInputProps> = ({
  id: externalId,
  value,
  onChange,
  suggestions,
  placeholder,
  className,
}) => {
  const { t } = useTranslation("mappings")
  const autoId = useId()
  const inputId = externalId ?? `jmespath-input-${autoId.replace(/:/g, "")}`
  const listboxId = `${inputId}-listbox`

  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)

  const inputRef = useRef<HTMLInputElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  const filtered = suggestions.filter(
    (s) => s.toLowerCase().includes(value.toLowerCase()) && s !== value,
  )

  const showDropdown = open && filtered.length > 0

  // Fecha o dropdown ao clicar fora
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
        setActiveIndex(-1)
      }
    }
    document.addEventListener("mousedown", handleClickOutside)
    return () => document.removeEventListener("mousedown", handleClickOutside)
  }, [])

  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      onChange(e.target.value)
      setOpen(true)
      setActiveIndex(-1)
    },
    [onChange],
  )

  const handleSelect = useCallback(
    (suggestion: string) => {
      onChange(suggestion)
      setOpen(false)
      setActiveIndex(-1)
      inputRef.current?.focus()
    },
    [onChange],
  )

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (!showDropdown) {
        if (e.key === "ArrowDown" && filtered.length > 0) {
          setOpen(true)
          setActiveIndex(0)
          e.preventDefault()
        }
        return
      }

      switch (e.key) {
        case "ArrowDown":
          e.preventDefault()
          setActiveIndex((i) => Math.min(i + 1, filtered.length - 1))
          break
        case "ArrowUp":
          e.preventDefault()
          setActiveIndex((i) => Math.max(i - 1, 0))
          break
        case "Enter":
          if (activeIndex >= 0 && activeIndex < filtered.length) {
            e.preventDefault()
            handleSelect(filtered[activeIndex])
          }
          break
        case "Escape":
          e.preventDefault()
          setOpen(false)
          setActiveIndex(-1)
          break
        case "Tab":
          setOpen(false)
          setActiveIndex(-1)
          break
      }
    },
    [showDropdown, filtered, activeIndex, handleSelect],
  )

  const activeOptionId =
    activeIndex >= 0 ? `${listboxId}-option-${activeIndex}` : undefined

  return (
    <div ref={containerRef} className={cn("relative", className)}>
      {/* role="combobox" envolve o input, não o input em si — ARIA 1.2 */}
      <div
        role="combobox"
        aria-expanded={showDropdown}
        aria-haspopup="listbox"
        aria-owns={showDropdown ? listboxId : undefined}
      >
        <input
          ref={inputRef}
          id={inputId}
          type="text"
          value={value}
          onChange={handleInputChange}
          onFocus={() => { if (filtered.length > 0) setOpen(true) }}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          autoComplete="off"
          aria-autocomplete="list"
          aria-controls={showDropdown ? listboxId : undefined}
          aria-activedescendant={activeOptionId}
          className={cn(
            "w-full h-9 px-3 text-sm rounded-md border border-border bg-surface text-text placeholder:text-text-tertiary",
            "transition-colors focus:outline-none focus:border-primary-500 focus:ring-2 focus:ring-primary-500/20",
          )}
        />
      </div>

      {showDropdown && (
        <ul
          id={listboxId}
          role="listbox"
          aria-label={t("jmespathInput.suggestionsAriaLabel")}
          className={cn(
            "absolute z-50 mt-1 w-full rounded-md border border-border bg-surface shadow-md",
            "max-h-56 overflow-auto py-1 text-sm",
          )}
        >
          {filtered.map((suggestion, i) => (
            <li
              key={suggestion}
              id={`${listboxId}-option-${i}`}
              role="option"
              aria-selected={i === activeIndex}
              onMouseDown={(e) => {
                // Previne blur no input antes do click ser processado
                e.preventDefault()
                handleSelect(suggestion)
              }}
              className={cn(
                "cursor-pointer px-3 py-1.5 font-mono text-xs",
                i === activeIndex
                  ? "bg-primary-100 text-primary-800"
                  : "text-text hover:bg-surface-tertiary",
              )}
            >
              {suggestion}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export const JMESPathInput = memo(JMESPathInputInner)
JMESPathInput.displayName = "JMESPathInput"

export default JMESPathInput
