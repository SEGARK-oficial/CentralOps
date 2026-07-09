"use client"

import type React from "react"
import { MoonIcon, SunIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { useTheme } from "@/contexts/ThemeContext"
import { cn } from "@/lib/utils"

interface ThemeToggleProps {
  className?: string
}

/**
 * Botão de alternância de tema (claro/escuro). Acessível: anuncia o estado via
 * aria-label dinâmico e usa o anel de foco do DS. O ícone mostra o destino
 * (lua quando claro, sol quando escuro) seguindo a convenção de mercado.
 */
export const ThemeToggle: React.FC<ThemeToggleProps> = ({ className }) => {
  const { t } = useTranslation("ui")
  const { resolvedTheme, toggleTheme } = useTheme()
  const isDark = resolvedTheme === "dark"

  return (
    <button
      type="button"
      onClick={toggleTheme}
      aria-label={isDark ? t("themeToggle.switchToLight") : t("themeToggle.switchToDark")}
      title={isDark ? t("themeToggle.lightTheme") : t("themeToggle.darkTheme")}
      className={cn(
        "inline-flex h-9 w-9 items-center justify-center rounded-md transition-colors",
        "text-sidebar-text hover:bg-sidebar-hover hover:text-sidebar-text-active",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500",
        className,
      )}
    >
      {isDark ? <SunIcon size={18} aria-hidden="true" /> : <MoonIcon size={18} aria-hidden="true" />}
    </button>
  )
}

export default ThemeToggle
