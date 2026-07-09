"use client"

import type React from "react"
import { createContext, useContext, useCallback, useId, useMemo, useRef } from "react"
import { cn } from "@/lib/utils"

/**
 * Componente Tabs acessível (ARIA + navegação por teclado).
 *
 * Filosofia alinhada com o design system: sem Radix (para manter bundle
 * enxuto — já temos @radix-ui/react-slot apenas), uso de CVA/cn quando
 * agrega valor, variantes visuais compatíveis com o design system, etc.
 *
 * Uso:
 *
 *   const [tab, setTab] = useState<"a" | "b">("a")
 *   <Tabs value={tab} onValueChange={setTab}>
 *     <TabsList>
 *       <TabsTrigger value="a" icon={<MailIcon size={16} />}>Email</TabsTrigger>
 *       <TabsTrigger value="b">Collector</TabsTrigger>
 *     </TabsList>
 *     <TabsPanel value="a">…</TabsPanel>
 *     <TabsPanel value="b">…</TabsPanel>
 *   </Tabs>
 *
 * Navegação por teclado (padrão WAI-ARIA):
 * - ArrowLeft/ArrowRight movem o foco e ativam a tab.
 * - Home/End vão para primeira/última.
 * - Tabs com prop ``disabled`` são puladas.
 */

interface TabsContextValue {
  value: string
  onValueChange: (v: string) => void
  idBase: string
}

const TabsContext = createContext<TabsContextValue | null>(null)

function useTabsCtx(component: string): TabsContextValue {
  const ctx = useContext(TabsContext)
  if (!ctx) {
    throw new Error(`<${component}> precisa estar dentro de <Tabs>`)
  }
  return ctx
}

export interface TabsProps {
  value: string
  onValueChange: (value: string) => void
  children: React.ReactNode
  className?: string
}

export const Tabs: React.FC<TabsProps> = ({ value, onValueChange, children, className }) => {
  const idBase = useId().replace(/:/g, "")
  const ctx = useMemo<TabsContextValue>(
    () => ({ value, onValueChange, idBase }),
    [value, onValueChange, idBase],
  )
  return (
    <TabsContext.Provider value={ctx}>
      <div className={cn("flex flex-col gap-4", className)}>{children}</div>
    </TabsContext.Provider>
  )
}

// ── TabsList ────────────────────────────────────────────────────────

export interface TabsListProps {
  children: React.ReactNode
  className?: string
  ariaLabel?: string
}

export const TabsList: React.FC<TabsListProps> = ({ children, className, ariaLabel }) => {
  const listRef = useRef<HTMLDivElement>(null)

  /** Setinha → próxima/anterior tab **não-disabled**. */
  const handleKeyDown = useCallback((event: React.KeyboardEvent<HTMLDivElement>) => {
    const triggers = Array.from(
      listRef.current?.querySelectorAll<HTMLButtonElement>(
        '[role="tab"]:not([aria-disabled="true"])',
      ) ?? [],
    )
    if (triggers.length === 0) return

    const currentIdx = triggers.findIndex((el) => el === document.activeElement)
    let nextIdx = -1
    switch (event.key) {
      case "ArrowRight":
        nextIdx = currentIdx < 0 ? 0 : (currentIdx + 1) % triggers.length
        break
      case "ArrowLeft":
        nextIdx =
          currentIdx < 0
            ? triggers.length - 1
            : (currentIdx - 1 + triggers.length) % triggers.length
        break
      case "Home":
        nextIdx = 0
        break
      case "End":
        nextIdx = triggers.length - 1
        break
      default:
        return
    }
    event.preventDefault()
    triggers[nextIdx].focus()
    triggers[nextIdx].click()
  }, [])

  return (
    <div
      ref={listRef}
      role="tablist"
      aria-label={ariaLabel}
      onKeyDown={handleKeyDown}
      className={cn(
        "flex flex-wrap items-center gap-1 border-b border-border",
        className,
      )}
    >
      {children}
    </div>
  )
}

// ── TabsTrigger ─────────────────────────────────────────────────────

export interface TabsTriggerProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  value: string
  icon?: React.ReactNode
  /** Badge à direita (útil para contadores tipo "3 pendentes"). */
  badge?: React.ReactNode
}

export const TabsTrigger: React.FC<TabsTriggerProps> = ({
  value,
  children,
  icon,
  disabled = false,
  className,
  badge,
  ...rest
}) => {
  const ctx = useTabsCtx("TabsTrigger")
  const selected = ctx.value === value
  return (
    <button
      {...rest}
      type="button"
      role="tab"
      id={`${ctx.idBase}-tab-${value}`}
      aria-controls={`${ctx.idBase}-panel-${value}`}
      aria-selected={selected}
      aria-disabled={disabled}
      tabIndex={selected ? 0 : -1}
      disabled={disabled}
      onClick={() => !disabled && ctx.onValueChange(value)}
      className={cn(
        // focus-ring: estratégia única de foco do design system.
        "inline-flex items-center gap-2 border-b-2 px-4 py-2.5 text-sm font-medium transition-colors focus-ring",
        "-mb-px", // alinha a border-b do trigger sobre a border-b do TabsList
        selected
          ? "border-primary-600 text-primary-700"
          : "border-transparent text-text-secondary hover:text-text hover:border-border",
        disabled && "cursor-not-allowed opacity-50",
        className,
      )}
    >
      {icon && (
        <span className="shrink-0" aria-hidden="true">
          {icon}
        </span>
      )}
      <span>{children}</span>
      {badge && <span className="shrink-0">{badge}</span>}
    </button>
  )
}

// ── TabsPanel ───────────────────────────────────────────────────────

export interface TabsPanelProps extends React.HTMLAttributes<HTMLDivElement> {
  value: string
  /** Se true, mantém a árvore renderizada (apenas oculta). Útil para
   *  preservar scroll/input state ao trocar de aba. Default: false (unmount). */
  keepMounted?: boolean
}

export const TabsPanel: React.FC<TabsPanelProps> = ({
  value,
  children,
  className,
  keepMounted = false,
  ...rest
}) => {
  const ctx = useTabsCtx("TabsPanel")
  const active = ctx.value === value
  if (!active && !keepMounted) return null
  return (
    <div
      {...rest}
      role="tabpanel"
      id={`${ctx.idBase}-panel-${value}`}
      aria-labelledby={`${ctx.idBase}-tab-${value}`}
      hidden={!active}
      className={cn("focus-ring", className)}
      tabIndex={0}
    >
      {children}
    </div>
  )
}
