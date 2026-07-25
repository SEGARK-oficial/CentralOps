import type React from "react"
import { useTranslation } from "react-i18next"
import { GlobeIcon } from "lucide-react"
import { SUPPORTED_LOCALES, type AppLocale } from "@/i18n"
import { updateMyLocale } from "@/services/api"

/** Locale picker. `i18n.changeLanguage` persists the choice via the detector's
 *  localStorage cache, so it survives reloads. */
export const LanguageSwitcher: React.FC<{ className?: string }> = ({ className }) => {
  const { t, i18n } = useTranslation("common")
  // `resolvedLanguage`, não `language`: o detector devolve a variante do navegador
  // ("en-US"), que não está em SUPPORTED_LOCALES — e o fallback "pt-BR" também não
  // estava, então o <select> ficava com um valor sem <option> e exibia a primeira
  // da lista. Resultado: tela em inglês com o seletor marcando "Português".
  const resolved = i18n.resolvedLanguage
  const current = (
    SUPPORTED_LOCALES.includes(resolved as AppLocale) ? resolved : "pt"
  ) as AppLocale

  return (
    <label
      className={`inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-surface-tertiary px-2 text-sm text-text-secondary transition-colors hover:border-border-hover focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-primary-500 ${className ?? ""}`}
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
