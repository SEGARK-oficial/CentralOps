/**
 * intl — locale-aware formatters driven by the active i18n language.
 *
 * Use these instead of `Number.toLocaleString("pt-BR", …)` / `Date.toLocaleString(…)`
 * so numbers, dates and percentages follow the user's chosen locale (decimal
 * separator, date order, etc.) instead of a hardcoded one.
 */
import i18n from "@/i18n"

/** The active locale, with a safe fallback for pre-init / SSR-less edge cases. */
export function currentLocale(): string {
  return i18n.language || "pt-BR"
}

export function formatNumber(n: number, opts?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(currentLocale(), opts).format(n)
}

/** Percentage from a RATIO (0.42 → "42%"). Pass already-multiplied values with
 *  `{ style: "decimal" }` via formatNumber instead. */
export function formatPercent(ratio: number, fractionDigits = 1): string {
  return new Intl.NumberFormat(currentLocale(), {
    style: "percent",
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(ratio)
}

export function formatDateTime(
  value: Date | string | number,
  opts: Intl.DateTimeFormatOptions = { dateStyle: "short", timeStyle: "short" },
): string {
  const date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return new Intl.DateTimeFormat(currentLocale(), opts).format(date)
}

export function formatDate(
  value: Date | string | number,
  opts: Intl.DateTimeFormatOptions = { dateStyle: "medium" },
): string {
  return formatDateTime(value, opts)
}
