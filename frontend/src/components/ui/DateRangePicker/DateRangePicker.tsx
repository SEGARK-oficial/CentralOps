"use client"

import type React from "react"
import { useEffect, useId, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { CalendarIcon, XIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn, formatDateTimeLocal, roundDateToMinute } from "@/lib/utils"
import { getPortalPosition } from "@/lib/portal-positioning"
import { formatDate } from "@/lib/intl"

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

  // Popover acessível: foca o primeiro controle ao abrir, prende Tab e fecha no Escape.
  useEffect(() => {
    if (!isOpen) return
    const node = popoverRef.current
    const focusables = () =>
      Array.from(node?.querySelectorAll<HTMLElement>('button, input, [tabindex]:not([tabindex="-1"])') ?? []).filter(
        (el) => !el.hasAttribute("disabled") && el.offsetParent !== null,
      )
    const id = window.setTimeout(() => focusables()[0]?.focus(), 0)
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault()
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
    }
  }, [isOpen])

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

  const dayStamp = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()

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

  return (
    <div className={cn("flex flex-col gap-1.5", className)} ref={containerRef}>
      {label && (
        <label id={labelId} htmlFor={triggerId} className="text-sm font-medium text-text">
          {label}
          {required && <span className="ml-0.5 text-danger-500">*</span>}
        </label>
      )}

      {/* Trigger = <button> real; "Limpar" é IRMÃO (não aninhado) — sem nested interactive. */}
      <div className="relative">
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
            "flex h-9 w-full items-center gap-2 rounded-md border bg-surface px-3 text-left text-sm transition-colors",
            "hover:border-border-hover focus:outline-none focus-visible:border-primary-500 focus-visible:ring-2 focus-visible:ring-primary-500/40",
            "disabled:cursor-not-allowed disabled:opacity-50",
            isOpen ? "border-primary-500 ring-2 ring-primary-500/20" : "border-border",
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
            className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-0.5 text-text-tertiary transition-colors hover:text-text focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
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
                  className="h-8 rounded border border-border bg-surface px-2 text-xs focus:border-primary-500 focus:outline-none disabled:opacity-50"
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
                  className="h-8 rounded border border-border bg-surface px-2 text-xs focus:border-primary-500 focus:outline-none disabled:opacity-50"
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

              <div className="mb-1 grid grid-cols-7 gap-0">
                {dayNames.map((d) => (
                  <div key={d} className="py-1 text-center text-xs font-medium text-text-tertiary">{d}</div>
                ))}
              </div>

              <div className="grid grid-cols-7 gap-0">
                {getDaysInMonth(currentMonth).map((date, idx) => (
                  <button
                    type="button"
                    key={`${date ? date.toISOString() : "e"}-${idx}`}
                    className={cn(
                      "h-8 w-full rounded text-xs transition-colors",
                      !date && "invisible",
                      date && "hover:bg-primary-50 hover:text-primary-700",
                      date && isSelected(date) && "bg-primary-600 font-semibold text-white hover:bg-primary-700 hover:text-white",
                      date && isInRange(date) && !isSelected(date) && "bg-primary-50 text-primary-700",
                      date && date.toDateString() === new Date().toDateString() && !isSelected(date) && "font-bold text-primary-600",
                    )}
                    onClick={() => date && handleDateClick(date)}
                    disabled={!date}
                    aria-label={date ? t("dateRangePicker.selectDate", { date: formatDate(date) }) : undefined}
                  >
                    {date?.getDate()}
                  </button>
                ))}
              </div>
            </div>

            <div className="mt-3 border-t border-border pt-3">
              <p className="text-xs text-text-tertiary" aria-live="polite">
                {selectingFrom ? t("dateRangePicker.selectStartDate") : t("dateRangePicker.selectEndDate")}
              </p>
            </div>
          </div>,
          document.body,
        )}

      {error && (
        <div id={errorId} className="text-xs text-danger-500" role="alert">{error}</div>
      )}
    </div>
  )
}

export default DateRangePicker
