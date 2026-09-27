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
import { createPortal } from "react-dom"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"
import { getPortalPosition } from "@/lib/portal-positioning"

interface JMESPathInputProps {
  id?: string
  value: string
  onChange: (value: string) => void
  suggestions: string[]
  placeholder?: string
  /** Classe do WRAPPER (posicionamento). Para estilizar o campo, `inputClassName`. */
  className?: string
  /**
   * Classe do `<input>` em si. Existe porque `className` cai no wrapper: sem
   * separar as duas, um consumidor que passa `font-mono` estiliza a caixa
   * errada e o caminho perde a fonte monoespaçada que o torna legível.
   */
  inputClassName?: string
  /** Rótulo associado por `htmlFor` — paridade com o `Input` do design system. */
  label?: string
  helperText?: string
  /**
   * Mensagem de erro de validação. Quando presente substitui o `helperText`,
   * marca `aria-invalid` e a borda — paridade com o `Input` do design system,
   * para que um formulário possa trocar `Input` por este sem perder o erro.
   */
  error?: string
  required?: boolean
  name?: string
  onBlur?: React.FocusEventHandler<HTMLInputElement>
  disabled?: boolean
  /** Nome acessível quando não há `label` visível (linhas repetidas de um construtor). */
  "aria-label"?: string
}

const JMESPathInputInner: React.FC<JMESPathInputProps> = ({
  id: externalId,
  value,
  onChange,
  suggestions,
  placeholder,
  className,
  inputClassName,
  label,
  helperText,
  error,
  required,
  name,
  onBlur,
  disabled,
  "aria-label": ariaLabel,
}) => {
  const { t } = useTranslation("mappings")
  const autoId = useId()
  const inputId = externalId ?? `jmespath-input-${autoId.replace(/:/g, "")}`
  const listboxId = `${inputId}-listbox`
  const errorId = error ? `${inputId}-error` : undefined

  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)

  const inputRef = useRef<HTMLInputElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  // LAY-39: a lista é portada pro <body> (ver render abaixo) — precisa da
  // própria ref pro click-outside e pro cálculo de posição saberem dela.
  const portalRef = useRef<HTMLUListElement>(null)
  const [portalStyle, setPortalStyle] = useState<React.CSSProperties>({})

  const filtered = suggestions.filter(
    (s) => s.toLowerCase().includes(value.toLowerCase()) && s !== value,
  )

  const showDropdown = open && filtered.length > 0

  // Fecha o dropdown ao clicar fora (do input OU da lista portada).
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      const target = e.target as Node
      const inContainer = containerRef.current?.contains(target) ?? false
      const inPortal = portalRef.current?.contains(target) ?? false
      if (!inContainer && !inPortal) {
        setOpen(false)
        setActiveIndex(-1)
      }
    }
    document.addEventListener("mousedown", handleClickOutside)
    return () => document.removeEventListener("mousedown", handleClickOutside)
  }, [])

  // LAY-39: a lista de sugestões era `position:absolute` dentro do painel de
  // regras (`overflow-auto`) — perto do fim da área visível, o dropdown
  // aparecia CORTADO pelo próprio scroll container. Portal pro <body> com
  // `position:fixed` (mesma técnica do Select — ver Select.tsx) escapa de
  // qualquer ancestral com overflow clipping.
  useEffect(() => {
    if (!showDropdown || !containerRef.current) return

    const updatePosition = () => {
      if (!containerRef.current) return
      const ESTIMATED_HEIGHT = 224 // max-h-56
      const pos = getPortalPosition(containerRef.current, ESTIMATED_HEIGHT)
      setPortalStyle({
        position: "fixed",
        top: pos.top,
        left: pos.left,
        width: pos.width,
        zIndex: "var(--z-index-popover)",
      })
    }

    updatePosition()

    // Fecha em scroll — exceto scroll DENTRO da própria lista (ela rola
    // internamente, `max-h-56 overflow-auto`).
    const handleScroll = (event: Event) => {
      const target = event.target as Node | null
      if (target && portalRef.current?.contains(target)) return
      setOpen(false)
    }
    window.addEventListener("scroll", handleScroll, { passive: true, capture: true })
    window.addEventListener("resize", updatePosition, { passive: true })
    return () => {
      window.removeEventListener("scroll", handleScroll, { capture: true })
      window.removeEventListener("resize", updatePosition)
    }
  }, [showDropdown])

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
      {label && (
        <label
          htmlFor={inputId}
          className="mb-1.5 block text-sm font-medium text-text"
        >
          {label}
        </label>
      )}
      {/* role="combobox" envolve o input, não o input em si — ARIA 1.2 */}
      <div
        role="combobox"
        aria-expanded={showDropdown}
        aria-haspopup="listbox"
        aria-owns={showDropdown ? listboxId : undefined}
        // R4-9.1 (jsx-a11y/role-has-required-aria-props): o role="combobox"
        // exige `aria-controls` no PRÓPRIO elemento com o role — já existia
        // no <input> filho (linha abaixo), mas o wrapper também precisa.
        aria-controls={showDropdown ? listboxId : undefined}
      >
        <input
          ref={inputRef}
          id={inputId}
          name={name}
          type="text"
          value={value}
          onChange={handleInputChange}
          onBlur={onBlur}
          onFocus={() => { if (filtered.length > 0) setOpen(true) }}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          required={required}
          disabled={disabled}
          autoComplete="off"
          aria-autocomplete="list"
          aria-controls={showDropdown ? listboxId : undefined}
          aria-activedescendant={activeOptionId}
          aria-invalid={error ? "true" : "false"}
          aria-describedby={errorId}
          aria-label={ariaLabel}
          className={cn(
            "w-full h-9 px-3 text-sm rounded-md border border-border bg-surface text-text placeholder:text-text-tertiary",
            "transition-colors focus:outline-none focus:border-primary-500 focus:ring-2 focus:ring-primary-500/20",
            error && "border-danger-500",
            inputClassName,
          )}
        />
      </div>

      {error ? (
        <p id={errorId} role="alert" className="mt-1 text-xs text-danger-500">
          {error}
        </p>
      ) : (
        helperText && <p className="mt-1 text-xs text-text-tertiary">{helperText}</p>
      )}

      {showDropdown &&
        typeof document !== "undefined" &&
        createPortal(
          <ul
            ref={portalRef}
            id={listboxId}
            role="listbox"
            aria-label={t("jmespathInput.suggestionsAriaLabel")}
            style={portalStyle}
            className={cn(
              "rounded-md border border-border bg-surface shadow-md",
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
          </ul>,
          document.body,
        )}
    </div>
  )
}

export const JMESPathInput = memo(JMESPathInputInner)
JMESPathInput.displayName = "JMESPathInput"

export default JMESPathInput
