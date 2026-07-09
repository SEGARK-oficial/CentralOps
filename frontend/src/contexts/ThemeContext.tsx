"use client"

/**
 * ThemeContext
 * Tema claro/escuro desacoplado do AuthContext (SRP). Persiste a preferência
 * ("light" | "dark" | "system") e aplica a classe `.dark` na raiz do documento,
 * que dispara o remapeamento de tokens definido em globals.css.
 *
 * - "system" segue prefers-color-scheme e reage a mudanças do SO em tempo real.
 * - A escolha explícita (light/dark) tem prioridade e fica persistida.
 */

import type React from "react"
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react"

export type ThemePreference = "light" | "dark" | "system"
export type ResolvedTheme = "light" | "dark"

const STORAGE_KEY = "centralops_theme"

interface ThemeContextValue {
  /** Preferência escolhida pelo usuário (pode ser "system"). */
  theme: ThemePreference
  /** Tema efetivamente aplicado, após resolver "system". */
  resolvedTheme: ResolvedTheme
  setTheme: (theme: ThemePreference) => void
  /** Alterna explicitamente entre claro e escuro (a partir do tema resolvido). */
  toggleTheme: () => void
}

const ThemeContext = createContext<ThemeContextValue | undefined>(undefined)

function prefersDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches
}

function readStoredTheme(): ThemePreference {
  if (typeof window === "undefined") return "system"
  const stored = window.localStorage.getItem(STORAGE_KEY)
  return stored === "light" || stored === "dark" || stored === "system" ? stored : "system"
}

function resolveTheme(theme: ThemePreference): ResolvedTheme {
  if (theme === "system") return prefersDark() ? "dark" : "light"
  return theme
}

export const ThemeProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [theme, setThemeState] = useState<ThemePreference>(() => readStoredTheme())
  const [resolvedTheme, setResolvedTheme] = useState<ResolvedTheme>(() => resolveTheme(readStoredTheme()))

  // Aplica a classe `.dark` na raiz e mantém `resolvedTheme` em sincronia.
  useEffect(() => {
    const resolved = resolveTheme(theme)
    setResolvedTheme(resolved)
    const root = document.documentElement
    root.classList.toggle("dark", resolved === "dark")
  }, [theme])

  // Quando em "system", reage a mudanças do SO sem exigir reload.
  useEffect(() => {
    if (theme !== "system" || typeof window === "undefined") return
    const media = window.matchMedia("(prefers-color-scheme: dark)")
    const handler = () => {
      const resolved = prefersDark() ? "dark" : "light"
      setResolvedTheme(resolved)
      document.documentElement.classList.toggle("dark", resolved === "dark")
    }
    media.addEventListener("change", handler)
    return () => media.removeEventListener("change", handler)
  }, [theme])

  const setTheme = useCallback((next: ThemePreference) => {
    setThemeState(next)
    if (typeof window !== "undefined") window.localStorage.setItem(STORAGE_KEY, next)
  }, [])

  const toggleTheme = useCallback(() => {
    setThemeState((prev) => {
      const next: ThemePreference = resolveTheme(prev) === "dark" ? "light" : "dark"
      if (typeof window !== "undefined") window.localStorage.setItem(STORAGE_KEY, next)
      return next
    })
  }, [])

  const value = useMemo<ThemeContextValue>(
    () => ({ theme, resolvedTheme, setTheme, toggleTheme }),
    [theme, resolvedTheme, setTheme, toggleTheme],
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error("useTheme must be used within a ThemeProvider")
  return ctx
}
