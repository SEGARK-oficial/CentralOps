/**
 * i18n bootstrap (react-i18next) for the CentralOps platform SPA.
 *
 * Catalogs are auto-discovered from `src/i18n/locales/<locale>/<namespace>.json`
 * via import.meta.glob — so adding a NAMESPACE (a new screen's strings) or a whole
 * new LANGUAGE is a drop-in file with ZERO change to this module. That keeps the
 * screen-by-screen string extraction free of shared-file coordination.
 *
 * Locale codes are BASE codes (`pt`/`en`/`es`), not region-suffixed: base codes
 * resolve any navigator variant (pt-BR/pt-PT → pt, en-US → en) and sidestep an
 * i18next region-code translator quirk (getResource finds "pt-BR" but t() returns
 * the key). `pt` here IS Brazilian-Portuguese content, and is the fallback — so an
 * un-migrated / un-translated key resolves to Portuguese, never a raw key.
 */
import i18n, { type Resource } from "i18next"
import { initReactI18next } from "react-i18next"
import LanguageDetector from "i18next-browser-languagedetector"

export const SUPPORTED_LOCALES = ["pt", "en", "es"] as const
export type AppLocale = (typeof SUPPORTED_LOCALES)[number]

/** Persisted here; synced to the backend user profile later so API errors return
 *  in the same language (Fase 3/4). */
export const LOCALE_STORAGE_KEY = "centralops.locale"

// Eagerly bundle every catalog. Keyed as locales/<locale>/<namespace>.json.
const catalogs = import.meta.glob<{ default: Record<string, unknown> }>(
  "./locales/*/*.json",
  { eager: true },
)

export const resources: Record<string, Record<string, unknown>> = {}
const nsSet = new Set<string>()
for (const path in catalogs) {
  const match = path.match(/\.\/locales\/([^/]+)\/(.+)\.json$/)
  if (!match) continue
  const [, locale, ns] = match
  const mod = catalogs[path]
  ;(resources[locale] ??= {})[ns] = (mod && "default" in mod ? mod.default : mod) as Record<
    string,
    unknown
  >
  nsSet.add(ns)
}
export const NAMESPACES = [...nsSet]

void i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: resources as Resource,
    fallbackLng: "pt",
    // Inline resources → initialise synchronously so the first render already has
    // translations (avoids the "raw keys flash" with useSuspense:false).
    initImmediate: false,
    supportedLngs: SUPPORTED_LOCALES as unknown as string[],
    // Map navigator "en-US" / "es-419" → "en" / "es".
    nonExplicitSupportedLngs: true,
    ns: NAMESPACES,
    defaultNS: "common",
    interpolation: { escapeValue: false },
    returnNull: false,
    react: { useSuspense: false },
    detection: {
      order: ["localStorage", "navigator", "htmlTag"],
      lookupLocalStorage: LOCALE_STORAGE_KEY,
      caches: ["localStorage"],
    },
  })

if (import.meta.env.DEV) {
  ;(window as unknown as { i18n?: typeof i18n }).i18n = i18n
}

export default i18n
