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

// PERF-15: `Intl.DateTimeFormat`/`NumberFormat` fazem parse do locale + das
// opções na CONSTRUÇÃO (não no `.format()`) — tabelas densas chamam
// `formatDate`/`formatNumber` por linha e recriavam o formatter a cada
// chamada. O cache é por `(locale, JSON das opções)`: a combinação é finita
// (poucos locales × poucos conjuntos de opções usados no app), então não há
// risco de crescimento sem teto.
const numberFormatCache = new Map<string, Intl.NumberFormat>()
const dateTimeFormatCache = new Map<string, Intl.DateTimeFormat>()

function cacheKey(locale: string, opts?: object): string {
  return opts ? `${locale}|${JSON.stringify(opts)}` : locale
}

function getNumberFormat(locale: string, opts?: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = cacheKey(locale, opts)
  let formatter = numberFormatCache.get(key)
  if (!formatter) {
    formatter = new Intl.NumberFormat(locale, opts)
    numberFormatCache.set(key, formatter)
  }
  return formatter
}

function getDateTimeFormat(locale: string, opts?: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = cacheKey(locale, opts)
  let formatter = dateTimeFormatCache.get(key)
  if (!formatter) {
    formatter = new Intl.DateTimeFormat(locale, opts)
    dateTimeFormatCache.set(key, formatter)
  }
  return formatter
}

export function formatNumber(n: number, opts?: Intl.NumberFormatOptions): string {
  return getNumberFormat(currentLocale(), opts).format(n)
}

/** Percentage from a RATIO (0.42 → "42%"). Pass already-multiplied values with
 *  `{ style: "decimal" }` via formatNumber instead. */
export function formatPercent(ratio: number, fractionDigits = 1): string {
  return getNumberFormat(currentLocale(), {
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
  return getDateTimeFormat(currentLocale(), opts).format(date)
}

export function formatDate(
  value: Date | string | number,
  opts: Intl.DateTimeFormatOptions = { dateStyle: "medium" },
): string {
  return formatDateTime(value, opts)
}
