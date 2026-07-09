"use client"

/**
 * CommandPalette — atalho ⌘K / Ctrl+K para navegação rápida.
 *
 * - Overlay + dialog centralizado (tokens do Modal: z-modal-backdrop, bg-overlay, bg-surface)
 * - Foco preso (FocusScope trapped+loop) e restaurado ao fechar
 * - Navegação por setas Up/Down + Enter para executar + Esc para fechar
 * - Filtragem por substring case-insensitive em label+keywords
 * - Agrupamento por `group` (ex: "Navegar" / "Ações")
 */

import type React from "react"
import { useCallback, useEffect, useId, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { SearchIcon } from "lucide-react"
import { FocusScope } from "@radix-ui/react-focus-scope"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"

// ---------------------------------------------------------------------------
// Tipos públicos
// ---------------------------------------------------------------------------

export interface PaletteCommand {
  id: string
  label: string
  group: string
  keywords?: string[]
  icon?: React.ReactNode
  run: () => void
}

export interface CommandPaletteProps {
  /** Lista de comandos disponíveis. */
  commands: PaletteCommand[]
  /** Controlado externamente — opcional; se omitido, o componente gerencia. */
  open?: boolean
  onOpenChange?: (open: boolean) => void
}

// ---------------------------------------------------------------------------
// Lógica de filtragem
// ---------------------------------------------------------------------------

function filterCommands(commands: PaletteCommand[], query: string): PaletteCommand[] {
  const q = query.trim().toLowerCase()
  if (!q) return commands
  return commands.filter((cmd) => {
    const haystack = [cmd.label, ...(cmd.keywords ?? [])].join(" ").toLowerCase()
    return haystack.includes(q)
  })
}

function groupBy<T>(items: T[], key: (item: T) => string): Map<string, T[]> {
  const map = new Map<string, T[]>()
  for (const item of items) {
    const k = key(item)
    if (!map.has(k)) map.set(k, [])
    map.get(k)!.push(item)
  }
  return map
}

// ---------------------------------------------------------------------------
// Componente principal
// ---------------------------------------------------------------------------

export const CommandPalette: React.FC<CommandPaletteProps> = ({
  commands,
  open: openProp,
  onOpenChange,
}) => {
  const { t } = useTranslation("ui")
  // Modo não-controlado: estado interno
  const [openInternal, setOpenInternal] = useState(false)
  const isControlled = openProp !== undefined
  const open = isControlled ? openProp : openInternal

  const setOpen = useCallback(
    (next: boolean) => {
      if (!isControlled) setOpenInternal(next)
      onOpenChange?.(next)
    },
    [isControlled, onOpenChange],
  )

  const [query, setQuery] = useState("")
  const [activeIndex, setActiveIndex] = useState(0)

  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLUListElement>(null)
  const previousActiveElement = useRef<HTMLElement | null>(null)

  // Ids acessíveis
  const labelId = useId()
  const listboxId = useId()
  const makeItemId = (id: string) => `cp-item-${id}`

  // Comandos filtrados e agrupados
  const filtered = filterCommands(commands, query)
  const groups = groupBy(filtered, (c) => c.group)

  // Lista plana (para cálculo de índice ativo)
  const flatFiltered = filtered

  // -----------------------------------------------------------------------
  // Listener global Cmd+K / Ctrl+K
  // -----------------------------------------------------------------------
  useEffect(() => {
    const handleKeydown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault()
        setOpen(true)
      }
    }
    window.addEventListener("keydown", handleKeydown)
    return () => window.removeEventListener("keydown", handleKeydown)
  }, [setOpen])

  // -----------------------------------------------------------------------
  // Efeitos ao abrir/fechar
  // -----------------------------------------------------------------------
  useEffect(() => {
    if (open) {
      previousActiveElement.current = document.activeElement as HTMLElement
      document.body.style.overflow = "hidden"
      setQuery("")
      setActiveIndex(0)
      // Foco no input após montagem
      requestAnimationFrame(() => inputRef.current?.focus())
      return () => {
        document.body.style.overflow = ""
        previousActiveElement.current?.focus()
      }
    }
  }, [open])

  // -----------------------------------------------------------------------
  // Scroll do item ativo para a vista
  // -----------------------------------------------------------------------
  useEffect(() => {
    if (!open) return
    const activeCmd = flatFiltered[activeIndex]
    if (!activeCmd) return
    const el = document.getElementById(makeItemId(activeCmd.id))
    el?.scrollIntoView({ block: "nearest" })
  }, [activeIndex, open, flatFiltered])

  // -----------------------------------------------------------------------
  // Handlers de teclado dentro do dialog
  // -----------------------------------------------------------------------
  const handleDialogKeydown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault()
      setOpen(false)
      return
    }
    if (e.key === "ArrowDown") {
      e.preventDefault()
      setActiveIndex((i) => Math.min(i + 1, flatFiltered.length - 1))
      return
    }
    if (e.key === "ArrowUp") {
      e.preventDefault()
      setActiveIndex((i) => Math.max(i - 1, 0))
      return
    }
    if (e.key === "Enter") {
      e.preventDefault()
      const cmd = flatFiltered[activeIndex]
      if (cmd) {
        setOpen(false)
        cmd.run()
      }
    }
  }

  const handleQueryChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setQuery(e.target.value)
    setActiveIndex(0)
  }

  const handleItemClick = (cmd: PaletteCommand) => {
    setOpen(false)
    cmd.run()
  }

  const handleOverlayClick = (e: React.MouseEvent) => {
    if (e.target === e.currentTarget) setOpen(false)
  }

  if (!open) return null

  const activeCmd = flatFiltered[activeIndex]

  return createPortal(
    <div
      className="fixed inset-0 z-modal-backdrop bg-overlay flex items-start justify-center pt-[10vh] px-4 animate-fade-in"
      onClick={handleOverlayClick}
      // Overlay não é o dialog; não recebe role
    >
      {/*
        FocusScope trapped + loop: Tab/Shift+Tab ficam dentro do palette.
        Esc é capturado por handleDialogKeydown.
      */}
      <FocusScope trapped loop>
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby={labelId}
          className={cn(
            "w-full max-w-xl bg-surface rounded-lg shadow-xl animate-slide-up",
            "flex flex-col max-h-[70vh] border border-border",
          )}
          onKeyDown={handleDialogKeydown}
          // tabIndex necessário para o FocusScope ter âncora inicial
          tabIndex={-1}
        >
          {/* Cabeçalho acessível — visualmente escondido, serve de label para o dialog */}
          <span id={labelId} className="sr-only">
            {t("commandPalette.dialogLabel")}
          </span>

          {/* Input de busca */}
          <div className="flex items-center gap-2 px-4 py-3 border-b border-border">
            <SearchIcon size={16} className="text-text-tertiary shrink-0" aria-hidden="true" />
            <input
              ref={inputRef}
              type="text"
              value={query}
              onChange={handleQueryChange}
              placeholder={t("commandPalette.searchPlaceholder")}
              className={cn(
                "flex-1 bg-transparent text-sm text-text placeholder:text-text-tertiary",
                "outline-none border-none focus:ring-0",
              )}
              aria-label={t("commandPalette.searchAriaLabel")}
              aria-autocomplete="list"
              aria-controls={listboxId}
              aria-activedescendant={activeCmd ? makeItemId(activeCmd.id) : undefined}
              role="combobox"
              aria-expanded="true"
              autoComplete="off"
              spellCheck={false}
            />
            <kbd className="hidden sm:inline-flex items-center gap-0.5 text-xs text-text-tertiary font-mono border border-border rounded px-1.5 py-0.5">
              Esc
            </kbd>
          </div>

          {/* Lista de resultados */}
          <ul
            id={listboxId}
            ref={listRef}
            role="listbox"
            aria-label={t("commandPalette.resultsAriaLabel")}
            className="flex-1 overflow-y-auto py-2"
          >
            {flatFiltered.length === 0 ? (
              <li className="px-4 py-8 text-center text-sm text-text-tertiary" role="option" aria-selected="false">
                {t("commandPalette.noResults", { query })}
              </li>
            ) : (
              Array.from(groups.entries()).map(([groupName, cmds]) => (
                <li key={groupName} role="presentation">
                  {/* Cabeçalho do grupo */}
                  <div
                    className="px-4 py-1.5 text-xs font-semibold text-text-tertiary uppercase tracking-wider"
                    aria-hidden="true"
                  >
                    {groupName}
                  </div>
                  <ul role="group" aria-label={groupName}>
                    {cmds.map((cmd) => {
                      const isActive = flatFiltered.indexOf(cmd) === activeIndex
                      return (
                        <li
                          key={cmd.id}
                          id={makeItemId(cmd.id)}
                          role="option"
                          aria-selected={isActive}
                          className={cn(
                            "flex items-center gap-3 mx-2 px-3 py-2 rounded-md text-sm cursor-pointer",
                            "text-text transition-colors",
                            isActive
                              ? "bg-primary-50 text-primary-700"
                              : "hover:bg-surface-hover",
                          )}
                          onClick={() => handleItemClick(cmd)}
                          // Suporte a mouse sem afetar navegação por teclado
                          onMouseEnter={() => setActiveIndex(flatFiltered.indexOf(cmd))}
                        >
                          {cmd.icon && (
                            <span className="shrink-0 text-text-tertiary" aria-hidden="true">
                              {cmd.icon}
                            </span>
                          )}
                          <span className="flex-1 truncate">{cmd.label}</span>
                        </li>
                      )
                    })}
                  </ul>
                </li>
              ))
            )}
          </ul>

          {/* Rodapé — dicas de teclado */}
          <div className="flex items-center gap-4 px-4 py-2 border-t border-border text-xs text-text-tertiary">
            <span className="flex items-center gap-1">
              <kbd className="font-mono border border-border rounded px-1 py-0.5">↑↓</kbd>
              {t("commandPalette.footer.navigate")}
            </span>
            <span className="flex items-center gap-1">
              <kbd className="font-mono border border-border rounded px-1 py-0.5">↵</kbd>
              {t("commandPalette.footer.execute")}
            </span>
            <span className="flex items-center gap-1">
              <kbd className="font-mono border border-border rounded px-1 py-0.5">Esc</kbd>
              {t("commandPalette.footer.close")}
            </span>
          </div>
        </div>
      </FocusScope>
    </div>,
    document.body,
  )
}

export default CommandPalette
