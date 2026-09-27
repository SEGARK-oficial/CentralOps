"use client"

import type React from "react"
import { useContext, useEffect, useId, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { CalendarIcon, XIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn, formatDateTimeLocal, roundDateToMinute } from "@/lib/utils"
import { getPortalPosition } from "@/lib/portal-positioning"
import { formatDate } from "@/lib/intl"
import { PortalContainerContext } from "@/components/ui/Modal/Modal"
import { isTopmostDialog, nextDialogOrder, registerOpenDialog, unregisterOpenDialog } from "@/components/ui/internal/dialogStack"

interface DateRange {
  from: Date | null
  to: Date | null
}

interface DateRangePickerProps {
  id?: string
  label?: string
  value?: DateRange | null
  onChange?: (range: DateRange) => void
  disabled?: boolean
  error?: string
  required?: boolean
  placeholder?: string
  className?: string
  "aria-label"?: string
}

const POPOVER_WIDTH = 320
const ESTIMATED_HEIGHT = 460

/** Meia-noite local do dia — chave estável pra comparar/indexar datas (ignora hora). */
const dayStamp = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()

const addDays = (date: Date, delta: number) => {
  const next = new Date(date)
  next.setDate(next.getDate() + delta)
  return next
}

/** Navega N meses preservando o dia-do-mês (grudado no último dia se o mês destino for mais curto). */
const addMonthsClamped = (date: Date, delta: number) => {
  const day = date.getDate()
  const next = new Date(date)
  next.setDate(1)
  next.setMonth(next.getMonth() + delta)
  const lastDayOfTarget = new Date(next.getFullYear(), next.getMonth() + 1, 0).getDate()
  next.setDate(Math.min(day, lastDayOfTarget))
  return next
}

const isSameMonth = (a: Date, b: Date) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth()

export const DateRangePicker: React.FC<DateRangePickerProps> = ({
  id,
  label,
  value = null,
  onChange,
  placeholder,
  disabled = false,
  error,
  required = false,
  className,
  "aria-label": ariaLabel,
}) => {
  const { t } = useTranslation("ui")
  const resolvedPlaceholder = placeholder ?? t("dateRangePicker.placeholder")
  const [isOpen, setIsOpen] = useState(false)
  const [currentMonth, setCurrentMonth] = useState(new Date())
  const [selectingFrom, setSelectingFrom] = useState(true)
  const [portalStyle, setPortalStyle] = useState<React.CSSProperties>({})
  const containerRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const popoverRef = useRef<HTMLDivElement>(null)
  const generatedId = useId()
  // R2-8.6: portava direto pro `document.body`, ignorando o
  // `PortalContainerContext` — dentro de um Modal, o FocusScope trapped não
  // reconhecia o popover como parte do próprio Modal (mesma classe de bug do
  // Select, A11Y-01). E o Escape daqui não participava da pilha compartilhada
  // (`dialogStack`): abrir o calendário dentro de um Modal e teclar Escape
  // fechava os dois juntos.
  const portalContainer = useContext(PortalContainerContext)
  const drpId = useId()
  const [drpOrder] = useState(nextDialogOrder)

  // A11Y-28: roving tabindex do calendário — só a célula "focada" (não
  // necessariamente selecionada) tem tabIndex=0; as outras 41 ficam -1, então
  // Tab entra/sai do grid em UMA parada, e as setas movem o foco por dentro.
  const [focusedDate, setFocusedDate] = useState<Date>(() => value?.from ?? new Date())
  const dayButtonRefs = useRef<Map<number, HTMLButtonElement>>(new Map())
  const dayGridRef = useRef<HTMLDivElement>(null)

  const normalizedValue = value ?? { from: null, to: null }
  const triggerId = id || `drp-${generatedId.replace(/:/g, "")}`
  const labelId = `${triggerId}-label`
  const errorId = error ? `${triggerId}-error` : undefined

  // Click-outside: fecha se o clique não for nem no container nem no portal.
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      const target = event.target as Node
      const inTrigger = containerRef.current?.contains(target) ?? false
      const inPortal = popoverRef.current?.contains(target) ?? false
      if (!inTrigger && !inPortal) {
        setIsOpen(false)
        setSelectingFrom(true)
      }
    }
    document.addEventListener("mousedown", handleClickOutside)
    return () => document.removeEventListener("mousedown", handleClickOutside)
  }, [])

  // Posicionamento do portal (position:fixed): escapa de qualquer ancestral com
  // overflow clipping, alinha ao trigger, faz flip e recalcula em scroll/resize.
  useEffect(() => {
    if (!isOpen || !triggerRef.current) return
    const update = () => {
      if (!triggerRef.current) return
      const pos = getPortalPosition(triggerRef.current, ESTIMATED_HEIGHT)
      const left = Math.max(8, Math.min(pos.left, window.innerWidth - POPOVER_WIDTH - 8))
      // popover (1060) > modal (1050): abre na frente quando usado dentro de Modal.
      setPortalStyle({ position: "fixed", top: pos.top, left, width: POPOVER_WIDTH, zIndex: "var(--z-index-popover)" })
    }
    update()
    const handleScroll = (event: Event) => {
      const target = event.target as Node | null
      if (target && popoverRef.current?.contains(target)) return
      setIsOpen(false)
    }
    window.addEventListener("scroll", handleScroll, { passive: true, capture: true })
    window.addEventListener("resize", update, { passive: true })
    return () => {
      window.removeEventListener("scroll", handleScroll, { capture: true })
      window.removeEventListener("resize", update)
    }
  }, [isOpen])

  // Ao abrir, o "cursor" do calendário parte do valor atual (ou hoje).
  useEffect(() => {
    if (isOpen) setFocusedDate(normalizedValue.from ?? new Date())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen])

  // Depois de uma navegação de mês disparada pelas SETAS (não pelos botões
  // ‹ ›), o grid é outro (novo mês) e o botão do dia focado é um nó NOVO —
  // precisa focar de novo. Só faz isso se o foco já estava dentro do grid
  // (senão roubaria foco de um clique nos botões ‹ ›/quick range).
  useEffect(() => {
    if (!isOpen) return
    if (!dayGridRef.current?.contains(document.activeElement)) return
    dayButtonRefs.current.get(dayStamp(focusedDate))?.focus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentMonth])

  // Popover acessível: foca o primeiro controle ao abrir, prende Tab e fecha no Escape.
  useEffect(() => {
    if (!isOpen) return
    registerOpenDialog(drpId, drpOrder)
    const node = popoverRef.current
    const focusables = () =>
      Array.from(node?.querySelectorAll<HTMLElement>("button, input, [tabindex]") ?? []).filter(
        // A11Y-28: com o roving tabindex do grid do calendário, só UMA
        // célula por vez tem tabIndex 0 — as outras 30+ são `button` mas com
        // tabIndex -1, e o seletor antigo (`button` sem filtro) as incluía
        // todas, quebrando o wrap Shift+Tab/Tab do trap (ver comentário
        // abaixo, no handler de Tab).
        (el) => !el.hasAttribute("disabled") && el.offsetParent !== null && el.tabIndex !== -1,
      )
    const id = window.setTimeout(() => focusables()[0]?.focus(), 0)
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        // R2-8.6: só o popover do TOPO fecha (pilha compartilhada) — e
        // `stopPropagation` evita que o mesmo Escape suba até o `document` e
        // feche um Modal por baixo (mesmo padrão do Select, A11Y-02).
        if (!isTopmostDialog(drpOrder)) return
        e.preventDefault()
        e.stopPropagation()
        setIsOpen(false)
        setSelectingFrom(true)
        triggerRef.current?.focus()
        return
      }
      if (e.key !== "Tab") return
      const f = focusables()
      if (f.length === 0) return
      const first = f[0]
      const last = f[f.length - 1]
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        first.focus()
      }
    }
    node?.addEventListener("keydown", onKeyDown)
    return () => {
      window.clearTimeout(id)
      node?.removeEventListener("keydown", onKeyDown)
      unregisterOpenDialog(drpId)
    }
  }, [isOpen, drpId, drpOrder])

  const formatBoundary = (date: Date | null) => (date ? formatDateTimeLocal(date) : "")

  const getDisplayValue = () => {
    if (!normalizedValue.from && !normalizedValue.to) return resolvedPlaceholder
    if (normalizedValue.from && !normalizedValue.to) return `${formatBoundary(normalizedValue.from)} - ...`
    if (!normalizedValue.from && normalizedValue.to) return `... - ${formatBoundary(normalizedValue.to)}`
    return `${formatBoundary(normalizedValue.from)} - ${formatBoundary(normalizedValue.to)}`
  }

  const getDaysInMonth = (date: Date) => {
    const y = date.getFullYear(), m = date.getMonth()
    const firstDay = new Date(y, m, 1)
    const lastDay = new Date(y, m + 1, 0)
    const days: Array<Date | null> = []
    for (let i = 0; i < firstDay.getDay(); i++) days.push(null)
    for (let d = 1; d <= lastDay.getDate(); d++) days.push(new Date(y, m, d))
    return days
  }

  const mergeDateWithTime = (selected: Date, ref: Date | null) => {
    const src = ref ?? roundDateToMinute(new Date())
    const merged = new Date(selected)
    merged.setHours(src.getHours(), src.getMinutes(), 0, 0)
    return merged
  }

  const getTimeValue = (d: Date | null) => {
    if (!d) return ""
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`
  }

  const handleTimeChange = (boundary: "from" | "to", val: string) => {
    const [h, m] = val.split(":").map(Number)
    if (isNaN(h) || isNaN(m)) return
    const target = boundary === "from" ? normalizedValue.from : normalizedValue.to
    if (!target) return
    const next = new Date(target)
    next.setHours(h, m, 0, 0)
    if (boundary === "from") {
      const nextTo = normalizedValue.to && next > normalizedValue.to ? new Date(next) : normalizedValue.to
      onChange?.({ from: next, to: nextTo })
    } else {
      const nextFrom = normalizedValue.from && next < normalizedValue.from ? new Date(next) : normalizedValue.from
      onChange?.({ from: nextFrom, to: next })
    }
  }

  const handleDateClick = (date: Date) => {
    if (selectingFrom || !normalizedValue.from) {
      onChange?.({ from: mergeDateWithTime(date, normalizedValue.from), to: null })
      setSelectingFrom(false)
      return
    }
    const nextTo = mergeDateWithTime(date, normalizedValue.to)
    if (nextTo < normalizedValue.from) {
      onChange?.({ from: mergeDateWithTime(date, normalizedValue.from), to: normalizedValue.from })
    } else {
      onChange?.({ from: normalizedValue.from, to: nextTo })
    }
    setIsOpen(false)
    setSelectingFrom(true)
  }

  const isInRange = (d: Date) => {
    if (!normalizedValue.from || !normalizedValue.to) return false
    const s = dayStamp(d)
    return s >= dayStamp(normalizedValue.from) && s <= dayStamp(normalizedValue.to)
  }

  const isSelected = (d: Date) =>
    (normalizedValue.from && dayStamp(d) === dayStamp(normalizedValue.from)) ||
    (normalizedValue.to && dayStamp(d) === dayStamp(normalizedValue.to))

  const clearSelection = (e: React.MouseEvent) => {
    e.stopPropagation()
    onChange?.({ from: null, to: null })
    setSelectingFrom(true)
  }

  const navigateMonth = (dir: "prev" | "next") => {
    setCurrentMonth((prev) => {
      const n = new Date(prev)
      n.setMonth(prev.getMonth() + (dir === "prev" ? -1 : 1))
      return n
    })
    // Mantém o "cursor" do roving tabindex no mesmo dia-do-mês (clampado),
    // pra quem navega pelos botões ‹ › e depois entra no grid por Tab.
    setFocusedDate((prev) => addMonthsClamped(prev, dir === "prev" ? -1 : 1))
  }

  // A11Y-28: roving tabindex + setas no grid do calendário (padrão APG de
  // date picker). Só o dia em `focusedDate` tem tabIndex=0; ArrowLeft/Right
  // andam 1 dia, ArrowUp/Down andam 1 semana, Home/End vão pro início/fim da
  // semana visível, PageUp/PageDown trocam de mês preservando o dia.
  const handleGridKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    let next: Date | null = null
    switch (e.key) {
      case "ArrowLeft": next = addDays(focusedDate, -1); break
      case "ArrowRight": next = addDays(focusedDate, 1); break
      case "ArrowUp": next = addDays(focusedDate, -7); break
      case "ArrowDown": next = addDays(focusedDate, 7); break
      case "Home": next = addDays(focusedDate, -focusedDate.getDay()); break
      case "End": next = addDays(focusedDate, 6 - focusedDate.getDay()); break
      case "PageUp": next = addMonthsClamped(focusedDate, e.shiftKey ? -12 : -1); break
      case "PageDown": next = addMonthsClamped(focusedDate, e.shiftKey ? 12 : 1); break
      default: return
    }
    e.preventDefault()
    setFocusedDate(next)
    if (isSameMonth(next, currentMonth)) {
      // Mesmo mês: o botão já existe no DOM, foca direto — não precisa
      // esperar o re-render (o efeito de foco pós-mês só cobre a troca de
      // mês, que desmonta/remonta os botões).
      dayButtonRefs.current.get(dayStamp(next))?.focus()
    } else {
      setCurrentMonth(new Date(next.getFullYear(), next.getMonth(), 1))
    }
  }

  const monthNames = t("dateRangePicker.months", { returnObjects: true }) as string[]
  const dayNames = t("dateRangePicker.weekdays", { returnObjects: true }) as string[]

  const quickRanges = [
    { label: t("dateRangePicker.quickRange.lastHour"), ms: 3600000 },
    { label: t("dateRangePicker.quickRange.last6Hours"), ms: 21600000 },
    { label: t("dateRangePicker.quickRange.last24Hours"), ms: 86400000 },
    { label: t("dateRangePicker.quickRange.last7Days"), ms: 604800000 },
    { label: t("dateRangePicker.quickRange.last30Days"), ms: 2592000000 },
  ]

  const handleQuickSelect = (ms: number) => {
    const end = roundDateToMinute(new Date())
    const start = new Date(end.getTime() - ms)
    start.setSeconds(0, 0)
    onChange?.({ from: start, to: end })
    setIsOpen(false)
    setSelectingFrom(true)
  }

  const hasValue = Boolean(normalizedValue.from || normalizedValue.to)

  // Grade do mês em semanas (para role="grid" > role="row" > role="gridcell").
  const monthDays = getDaysInMonth(currentMonth)
  const weeks: Array<Array<Date | null>> = []
  for (let i = 0; i < monthDays.length; i += 7) weeks.push(monthDays.slice(i, i + 7))

  // Se `focusedDate` caiu fora do mês exibido (ex.: popover reaberto num mês
  // diferente, antes de qualquer navegação), a célula roving cai pro dia de
  // hoje (se estiver no mês) ou pro dia 1 — sempre EXATAMENTE uma célula
  // tabbable, nunca zero.
  const monthDaysNonNull = monthDays.filter((d): d is Date => d !== null)
  const effectiveFocusedDate = monthDaysNonNull.some((d) => dayStamp(d) === dayStamp(focusedDate))
    ? focusedDate
    : (monthDaysNonNull.find((d) => d.toDateString() === new Date().toDateString()) ?? monthDaysNonNull[0] ?? focusedDate)

  return (
    <div className={cn("flex flex-col gap-1.5", className)} ref={containerRef}>
      {label && (
        <label id={labelId} htmlFor={triggerId} className="text-sm font-medium text-text">
          {label}
          {required && <span className="ml-0.5 text-danger-500" aria-hidden="true">*</span>}
        </label>
      )}

      {/* Trigger = <button> real; "Limpar" é IRMÃO (não aninhado) — sem nested interactive. */}
      <div className="relative">
        {/* `aria-invalid` não está na lista de props ARIA "suportadas" pelo
            role implícito de <button> (a validade formal é conceito de
            input/combobox) — mas este botão É o controle visível do campo
            (abre o picker), e é ele que precisa carregar o estado de erro
            pro operador/leitor de tela, não um <input> escondido. */}
        {/* eslint-disable-next-line jsx-a11y/role-supports-aria-props */}
        <button
          ref={triggerRef}
          type="button"
          id={triggerId}
          disabled={disabled}
          onClick={() => !disabled && setIsOpen((p) => !p)}
          aria-label={ariaLabel}
          aria-labelledby={label && !ariaLabel ? labelId : undefined}
          aria-haspopup="dialog"
          aria-expanded={isOpen}
          aria-invalid={error ? "true" : "false"}
          aria-describedby={errorId}
          className={cn(
            // Trigger é CONTROLE: limite em `border-field` (3:1, WCAG 1.4.11).
            // A hairline media 1.30:1. O hover saiu da borda porque
            // `border-hover` (16%) é mais fraco que `border-field` (34%).
            "flex h-9 w-full items-center gap-2 rounded-md border bg-surface px-3 text-left text-sm transition-colors focus-ring",
            "hover:bg-surface-hover",
            "disabled:cursor-not-allowed disabled:opacity-50",
            isOpen ? "border-primary-500 ring-2 ring-primary-500/20" : "border-border-field",
            error && "border-danger-500",
            hasValue && "pr-8",
          )}
        >
          <CalendarIcon size={16} className="shrink-0 text-text-tertiary" aria-hidden="true" />
          <span className={cn("flex-1 truncate", !hasValue && "text-text-tertiary")}>{getDisplayValue()}</span>
        </button>

        {hasValue && !disabled && (
          <button
            type="button"
            onClick={clearSelection}
            aria-label={t("dateRangePicker.clearSelection")}
            // A11Y-20: alvo de 18px (14px do ícone + 2px de padding) — abaixo
            // do mínimo de 24px. `h-6 w-6` fixo + flex-center resolve sem
            // aumentar o ícone visualmente.
            className="absolute right-2 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded text-text-tertiary transition-colors hover:text-text focus-ring"
          >
            <XIcon size={14} aria-hidden="true" />
          </button>
        )}
      </div>

      {isOpen &&
        typeof document !== "undefined" &&
        createPortal(
          <div
            ref={popoverRef}
            style={portalStyle}
            className="rounded-lg border border-border bg-surface p-4 shadow-lg animate-slide-down"
            role="dialog"
            aria-modal="true"
            aria-label={t("dateRangePicker.selectPeriod")}
          >
            {/* Quick ranges */}
            <div className="mb-3">
              <h4 className="mb-2 text-xs font-semibold uppercase text-text-secondary">{t("dateRangePicker.quickRanges")}</h4>
              <div className="flex flex-wrap gap-1">
                {quickRanges.map((r) => (
                  <button
                    type="button"
                    key={r.label}
                    onClick={() => handleQuickSelect(r.ms)}
                    className="rounded bg-surface-tertiary px-2 py-1 text-xs text-text-secondary transition-colors hover:bg-primary-50 hover:text-primary-700"
                  >
                    {r.label}
                  </button>
                ))}
              </div>
            </div>

            {/* Time inputs */}
            <div className="mb-3 grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1">
                <label htmlFor={`${triggerId}-from-time`} className="text-xs text-text-secondary">{t("dateRangePicker.startTime")}</label>
                <input
                  id={`${triggerId}-from-time`}
                  type="time"
                  step={60}
                  value={getTimeValue(normalizedValue.from)}
                  onChange={(e) => handleTimeChange("from", e.target.value)}
                  disabled={!normalizedValue.from}
                  className="h-8 rounded border border-border-field bg-surface px-2 text-xs focus-ring disabled:opacity-50"
                />
              </div>
              <div className="flex flex-col gap-1">
                <label htmlFor={`${triggerId}-to-time`} className="text-xs text-text-secondary">{t("dateRangePicker.endTime")}</label>
                <input
                  id={`${triggerId}-to-time`}
                  type="time"
                  step={60}
                  value={getTimeValue(normalizedValue.to)}
                  onChange={(e) => handleTimeChange("to", e.target.value)}
                  disabled={!normalizedValue.to}
                  className="h-8 rounded border border-border-field bg-surface px-2 text-xs focus-ring disabled:opacity-50"
                />
              </div>
            </div>

            {/* Calendar */}
            <div>
              <div className="mb-2 flex items-center justify-between">
                <button type="button" onClick={() => navigateMonth("prev")} className="flex h-7 w-7 items-center justify-center rounded text-text-secondary hover:bg-surface-tertiary" aria-label={t("dateRangePicker.previousMonth")}>‹</button>
                <span className="text-sm font-medium text-text">{monthNames[currentMonth.getMonth()]} {currentMonth.getFullYear()}</span>
                <button type="button" onClick={() => navigateMonth("next")} className="flex h-7 w-7 items-center justify-center rounded text-text-secondary hover:bg-surface-tertiary" aria-label={t("dateRangePicker.nextMonth")}>›</button>
              </div>

              {/* A11Y-28: padrão APG de date-picker grid — role="grid" com
                  linhas/células, roving tabindex (só effectiveFocusedDate tem
                  tabIndex=0) e as setas navegam por dentro via handleGridKeyDown. */}
              {/* eslint-disable-next-line jsx-a11y/interactive-supports-focus */}
              <div
                ref={dayGridRef}
                role="grid"
                aria-label={`${monthNames[currentMonth.getMonth()]} ${currentMonth.getFullYear()}`}
                onKeyDown={handleGridKeyDown}
              >
                <div role="row" className="mb-1 grid grid-cols-7 gap-0">
                  {dayNames.map((d) => (
                    <div key={d} role="columnheader" aria-label={d} className="py-1 text-center text-xs font-medium text-text-tertiary">{d}</div>
                  ))}
                </div>

                {weeks.map((week, wi) => (
                  <div role="row" key={wi} className="grid grid-cols-7 gap-0">
                    {week.map((date, di) => {
                      if (!date) {
                        return <div key={di} role="presentation" className="h-8 w-full" aria-hidden="true" />
                      }
                      const stamp = dayStamp(date)
                      const isToday = date.toDateString() === new Date().toDateString()
                      const roving = stamp === dayStamp(effectiveFocusedDate)
                      const selected = Boolean(isSelected(date))
                      return (
                        <button
                          type="button"
                          key={stamp}
                          ref={(el) => {
                            if (el) dayButtonRefs.current.set(stamp, el)
                            else dayButtonRefs.current.delete(stamp)
                          }}
                          role="gridcell"
                          tabIndex={roving ? 0 : -1}
                          aria-selected={selected}
                          aria-current={isToday ? "date" : undefined}
                          className={cn(
                            "h-8 w-full rounded text-xs transition-colors",
                            "hover:bg-primary-50 hover:text-primary-700",
                            selected && "bg-primary-600 font-semibold text-text-inverse hover:bg-primary-500 hover:text-text-inverse",
                            isInRange(date) && !selected && "bg-primary-50 text-primary-700",
                            isToday && !selected && "font-bold text-primary-600",
                          )}
                          onClick={() => {
                            setFocusedDate(date)
                            handleDateClick(date)
                          }}
                          onFocus={() => setFocusedDate(date)}
                          aria-label={t("dateRangePicker.selectDate", { date: formatDate(date) })}
                        >
                          {date.getDate()}
                        </button>
                      )
                    })}
                  </div>
                ))}
              </div>
            </div>

            <div className="mt-3 border-t border-border pt-3">
              <p className="text-xs text-text-tertiary" aria-live="polite">
                {selectingFrom ? t("dateRangePicker.selectStartDate") : t("dateRangePicker.selectEndDate")}
              </p>
            </div>
          </div>,
          // R2-8.6: porta para o painel do Modal quando aninhado (mesmo
          // padrão do Select) — `null` fora de um Modal cai no `document.body`
          // de sempre.
          portalContainer ?? document.body,
        )}

      {error && (
        <div id={errorId} className="text-xs text-danger-500" role="alert">{error}</div>
      )}
    </div>
  )
}

export default DateRangePicker
