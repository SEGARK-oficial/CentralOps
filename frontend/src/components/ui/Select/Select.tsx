"use client"

import type React from "react"
import { forwardRef, useContext, useEffect, useId, useMemo, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { ChevronDownIcon, CheckIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"
import { getPortalPosition } from "@/lib/portal-positioning"
import { PortalContainerContext } from "@/components/ui/Modal/Modal"

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

// R3-8.4: `forwardRef` para o TRIGGER (o botão que abre o dropdown) —
// retrocompatível de propósito (o EE importa `Select` do Core e não passa
// `ref`; sem `ref`, o comportamento é idêntico a antes). Sem isto,
// `registerField`/`useFirstInvalidFocus`/`useForm` não conseguiam focar um
// Select inválido — cada consumidor tinha que recorrer a `document.
// getElementById(...).focus()` com um `id` explícito só pra contornar.
export const Select = forwardRef<HTMLButtonElement, SelectProps>(function Select({
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
}, forwardedRef) {
  const { t } = useTranslation("ui")
  const resolvedPlaceholder = placeholder ?? t("select.placeholder")
  const [isOpen, setIsOpen] = useState(false)
  const [searchTerm, setSearchTerm] = useState("")
  const [portalStyle, setPortalStyle] = useState<React.CSSProperties>({})
  const selectRef = useRef<HTMLDivElement>(null)
  // `HTMLButtonElement | null` (não só `HTMLButtonElement`) — precisamos
  // ESCREVER em `.current` manualmente pra mesclar com o `forwardedRef`
  // (ver `setTriggerRef` abaixo); com só `<HTMLButtonElement>`, o TS resolve
  // pro overload que devolve `RefObject` (`.current` readonly).
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  // Mescla o `ref` interno (usado o tempo todo aqui dentro: foco, posição do
  // portal) com o `forwardedRef` opcional de quem consome o componente — os
  // dois precisam apontar pro MESMO nó.
  const setTriggerRef = (el: HTMLButtonElement | null) => {
    triggerRef.current = el
    if (typeof forwardedRef === "function") forwardedRef(el)
    else if (forwardedRef) forwardedRef.current = el
  }
  const inputRef = useRef<HTMLInputElement>(null)
  const portalRef = useRef<HTMLDivElement>(null)
  const generatedId = useId()
  // A11Y-01: dentro de um Modal, portar para o painel (não para document.body)
  // para que o FocusScope trapped enxergue a opção como parte do próprio Modal.
  const portalContainer = useContext(PortalContainerContext)
  // A11Y-27: type-ahead — acumula teclas digitadas em sequência (reset após
  // pausa) e foca a 1ª opção cujo label comece com o texto acumulado.
  const typeaheadBuffer = useRef("")
  const typeaheadTimer = useRef<number | null>(null)
  // R2-8.4: registro de botões de opção por VALOR (não por índice). O índice
  // de `filteredOptions` (que inclui desabilitadas) nunca bateu com o índice
  // do NodeList `button[role='option']:not(:disabled)` (só habilitadas) — a
  // partir da 1ª opção desabilitada, setas/type-ahead/foco-ao-abrir pulavam
  // pra opção errada. Indexar por valor elimina a classe inteira do bug.
  const optionButtonRefs = useRef(new Map<string | number, HTMLButtonElement>())

  const selectId = id || `select-${generatedId.replace(/:/g, "")}`
  const listboxId = `${selectId}-listbox`
  const errorId = error ? `${selectId}-error` : undefined
  const helperId = helperText ? `${selectId}-helper` : undefined
  const describedBy = [ariaDescribedBy, errorId, !error ? helperId : undefined].filter(Boolean).join(" ") || undefined

  // useMemo: `[value]` (caso escalar) nascia de novo a cada render, dando ao
  // efeito de foco abaixo uma dependência "sempre diferente" mesmo quando o
  // valor selecionado não mudou.
  const selectedValues = useMemo(
    () => (Array.isArray(value) ? value : value !== undefined && value !== "" ? [value] : []),
    [value],
  )

  const filteredOptions = useMemo(
    () => options.filter((option) => option.label.toLowerCase().includes(searchTerm.toLowerCase())),
    [options, searchTerm],
  )
  const selectableValues = useMemo(
    () => options.filter((option) => !option.disabled).map((option) => option.value),
    [options],
  )
  // R2-8.4: única fonte de verdade pra navegação por teclado — deriva da
  // MESMA lista (`filteredOptions`) que renderiza os botões, então a posição
  // aqui sempre bate com a posição real entre as opções focáveis.
  const enabledOptions = useMemo(
    () => filteredOptions.filter((option) => !option.disabled),
    [filteredOptions],
  )
  const focusOptionByValue = (value: string | number | undefined) => {
    if (value === undefined) return
    optionButtonRefs.current.get(value)?.focus()
  }
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

  // Type-ahead: só faz sentido sem a caixa de busca (options.length <= 10) —
  // com busca, o usuário já digita ali. Foca (não seleciona) a 1ª opção que
  // bate com o texto acumulado, como um <select> nativo.
  const runTypeahead = (key: string, onMatch: (option: SelectOption) => void) => {
    if (options.length > 10) return false
    if (key.length !== 1 || !/[\p{L}\p{N}]/u.test(key)) return false
    if (typeaheadTimer.current) window.clearTimeout(typeaheadTimer.current)
    typeaheadBuffer.current += key.toLowerCase()
    const buffer = typeaheadBuffer.current
    typeaheadTimer.current = window.setTimeout(() => {
      typeaheadBuffer.current = ""
    }, 500)
    const match = enabledOptions.find((option) => option.label.toLowerCase().startsWith(buffer))
    if (match) onMatch(match)
    return true
  }

  const handleTriggerKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (disabled) return
    if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault()
      setIsOpen(true)
      return
    }
    if (event.key === "Escape") {
      if (isOpen) {
        event.preventDefault()
        event.stopPropagation()
      }
      setIsOpen(false)
      return
    }
    const matchedTypeahead = runTypeahead(event.key, (option) => {
      setIsOpen(true)
      // O portal ainda não existe neste tick — espera o próximo frame.
      window.setTimeout(() => focusOptionByValue(option.value), 0)
    })
    if (matchedTypeahead) event.preventDefault()
  }

  const handleOptionKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>, option: SelectOption) => {
    if (enabledOptions.length === 0) return
    // R2-8.4: posição dentro da lista de opções FOCÁVEIS (mesma lista que
    // popula `optionButtonRefs`) — nunca da lista completa (que inclui
    // desabilitadas), que é o que causava o desalinhamento.
    const pos = enabledOptions.findIndex((o) => o.value === option.value)

    if (event.key === "ArrowDown") {
      event.preventDefault()
      focusOptionByValue(enabledOptions[Math.min(pos + 1, enabledOptions.length - 1)]?.value)
    } else if (event.key === "ArrowUp") {
      event.preventDefault()
      focusOptionByValue(enabledOptions[Math.max(pos - 1, 0)]?.value)
    } else if (event.key === "Home") {
      event.preventDefault()
      focusOptionByValue(enabledOptions[0]?.value)
    } else if (event.key === "End") {
      event.preventDefault()
      focusOptionByValue(enabledOptions[enabledOptions.length - 1]?.value)
    } else if (event.key === "Escape") {
      event.preventDefault()
      // A11Y-02: para a propagação AQUI — sem isso, o keydown nativo continua
      // subindo até o `document`, onde o Modal também escuta Escape, e o
      // dropdown fechar fecharia o Modal por baixo junto.
      event.stopPropagation()
      setIsOpen(false)
      triggerRef.current?.focus()
    } else if (event.key === "Enter" || event.key === " ") {
      // A11Y-01: explícito em vez de confiar na ativação nativa do <button>
      // por Enter/Espaço — garante seleção por teclado de forma determinística
      // (inclusive quando a opção é portada para dentro de um Modal).
      event.preventDefault()
      handleOptionClick(option.value)
    } else {
      runTypeahead(event.key, (match) => focusOptionByValue(match.value))
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

  // Foco inicial ao abrir o dropdown.
  // R3-8.2: o efeito rodava a cada mudança de `enabledOptions`/`selectedValues`
  // — no modo `multiple`, cada Espaço ALTERA `selectedValues` (toggle de
  // seleção), então o próprio ato de marcar uma opção re-disparava este
  // efeito e devolvia o foco à 1ª opção SELECIONADA, no meio da navegação por
  // teclado. Um `options` recriado inline pelo pai (nova referência a cada
  // render, mesmo conteúdo) tinha o mesmo efeito colateral via `enabledOptions`.
  // Depender só de `isOpen` — e rodar a lógica apenas na transição
  // fechado→aberto (`wasOpenRef`) — resolve as duas classes de bug: o efeito
  // não reage mais a nada que aconteça DEPOIS que o dropdown já abriu.
  const wasOpenRef = useRef(false)
  useEffect(() => {
    const justOpened = isOpen && !wasOpenRef.current
    wasOpenRef.current = isOpen
    if (!justOpened) return
    // O createPortal é síncrono mas o ref é preenchido após o commit do React,
    // então usamos um microtask (setTimeout 0) para garantir que o DOM está pronto.
    const id = window.setTimeout(() => {
      if (options.length > 10) {
        inputRef.current?.focus()
        return
      }
      if (enabledOptions.length === 0) return
      const selected = enabledOptions.find((option) => selectedValues.includes(option.value))
      focusOptionByValue((selected ?? enabledOptions[0]).value)
    }, 0)
    return () => window.clearTimeout(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen])

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
        {/* `aria-invalid` não está na lista de props ARIA "suportadas" pelo
            role implícito de <button> — mas este botão É o controle visível
            do campo, e precisa carregar o estado de erro pro operador. */}
        {/* eslint-disable-next-line jsx-a11y/role-supports-aria-props */}
        <button
          ref={setTriggerRef}
          type="button"
          id={selectId}
          name={name}
          className={cn(
            // Trigger é campo, logo é poço (`surface-tertiary`), igual a Input
            // e Textarea. A lista, essa sim, flutua: fica em `surface`.
            // focus-ring: estratégia única de foco do design system.
            "w-full flex items-center gap-2 rounded-md border bg-surface-tertiary text-left transition-colors focus-ring",
            size === "sm" ? "h-8 px-2.5 text-xs" : "h-9 px-3 text-sm",
            "disabled:opacity-50 disabled:cursor-not-allowed",
            error ? "border-danger-500" : "border-border-field hover:border-border-field-hover",
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
          <ChevronDownIcon
            size={16}
            aria-hidden="true"
            className={cn("text-text-tertiary shrink-0 transition-transform", isOpen && "rotate-180")}
          />
        </button>

        {isOpen && typeof document !== "undefined" && createPortal(
          <div
            ref={portalRef}
            style={portalStyle}
            className="bg-surface border border-border-field rounded-md shadow-lg animate-slide-down overflow-hidden"
          >
            {options.length > 10 && (
              <div className="p-2 border-b border-border-field">
                <input
                  ref={inputRef}
                  type="text"
                  placeholder={t("select.searchPlaceholder")}
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  onKeyDown={(e) => {
                    // A11Y-27: a busca era um beco sem saída de teclado — só dava
                    // pra digitar, sem jeito de navegar até uma opção ou fechar.
                    if (e.key === "ArrowDown") {
                      e.preventDefault()
                      focusOptionByValue(enabledOptions[0]?.value)
                    } else if (e.key === "Escape") {
                      e.preventDefault()
                      e.stopPropagation()
                      setIsOpen(false)
                      triggerRef.current?.focus()
                    }
                    // Tab: mantém o comportamento nativo (sai do campo); o
                    // click-outside/blur cuida de fechar o dropdown.
                  }}
                  className="w-full h-8 px-3 text-sm rounded border border-border-field bg-surface-tertiary text-text placeholder:text-text-tertiary focus-ring"
                  aria-label={t("select.searchAriaLabel")}
                />
              </div>
            )}

            {multiple && (
              <div className="flex gap-2 px-3 py-2 border-b border-border-field text-xs">
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
                <li role="presentation" className="px-3 py-2 text-sm text-text-tertiary text-center">{t("select.noOptionsFound")}</li>
              ) : (
                filteredOptions.map((option) => {
                  const isSelected = selectedValues.includes(option.value)
                  return (
                    // A11Y-27: `role="presentation"` — o padrão ARIA listbox espera
                    // `ul[role=listbox] > li[presentation] > button[role=option]`;
                    // sem isso, o `<li>` vira um nó extra na árvore de acessibilidade.
                    <li key={option.value} role="presentation">
                      <button
                        ref={(el) => {
                          if (option.disabled) return
                          if (el) optionButtonRefs.current.set(option.value, el)
                          else optionButtonRefs.current.delete(option.value)
                        }}
                        type="button"
                        className={cn(
                          // focus-ring: estratégia única; mantém bg de foco para feedback visual do item.
                          "w-full flex items-center gap-2 px-3 py-1.5 text-sm text-left transition-colors focus-ring",
                          "hover:bg-surface-hover focus-visible:bg-surface-hover",
                          isSelected && "bg-primary-50 text-primary-700 font-medium",
                          option.disabled && "opacity-50 cursor-not-allowed",
                        )}
                        onClick={() => !option.disabled && handleOptionClick(option.value)}
                        onKeyDown={(e) => handleOptionKeyDown(e, option)}
                        role="option"
                        aria-selected={isSelected}
                        disabled={option.disabled}
                      >
                        <span className="flex-1 truncate">{option.label}</span>
                        {isSelected && <CheckIcon size={16} aria-hidden="true" className="shrink-0 text-primary-600" />}
                      </button>
                    </li>
                  )
                })
              )}
            </ul>
          </div>,
          portalContainer ?? document.body,
        )}
      </div>

      {error ? (
        <div id={errorId} className="text-xs text-danger-500" role="alert">{error}</div>
      ) : helperText ? (
        <div id={helperId} className="text-xs text-text-tertiary">{helperText}</div>
      ) : null}
    </div>
  )
})

export default Select
