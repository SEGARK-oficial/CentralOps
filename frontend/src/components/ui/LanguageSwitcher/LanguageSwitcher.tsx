import type React from "react"
import { useTranslation } from "react-i18next"
import { GlobeIcon } from "lucide-react"
import { SUPPORTED_LOCALES, type AppLocale } from "@/i18n"
import { updateMyLocale } from "@/services/api"

/** Locale picker. `i18n.changeLanguage` persists the choice via the detector's
 *  localStorage cache, so it survives reloads. */
export const LanguageSwitcher: React.FC<{ className?: string }> = ({ className }) => {
  const { t, i18n } = useTranslation("common")
  const current = (
    SUPPORTED_LOCALES.includes(i18n.language as AppLocale) ? i18n.language : "pt-BR"
  ) as AppLocale

  return (
    <label
      className={`inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-sm text-text-secondary focus-within:ring-2 focus-within:ring-primary-500 ${className ?? ""}`}
    >
      <GlobeIcon size={15} aria-hidden="true" />
      <span className="sr-only">{t("language")}</span>
      <select
        value={current}
        onChange={(e) => {
          const next = e.target.value
          void i18n.changeLanguage(next)
          // Persist to the profile so it follows the user across devices.
          // Best-effort: ignored (401) on the pre-login page.
          void updateMyLocale(next).catch(() => {})
        }}
        aria-label={t("language")}
        className="cursor-pointer bg-transparent pr-1 text-text focus:outline-none"
      >
        {SUPPORTED_LOCALES.map((l) => (
          <option key={l} value={l}>
            {t(`languageNames.${l}`)}
          </option>
        ))}
      </select>
    </label>
  )
}

export default LanguageSwitcher
