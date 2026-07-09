"use client"

import type React from "react"
import { useEffect, useId, useRef, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { ChevronDownIcon, KeyIcon, LogOutIcon, ShieldCheckIcon, UserCogIcon } from "lucide-react"
import { useAuth } from "@/contexts/AuthContext"
import { cn } from "@/lib/utils"

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (parts.length === 0) return "?"
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
}

/**
 * Menu de usuário (avatar + dropdown) — padrão de SaaS (Linear/Vercel) que
 * substitui os controles soltos de sessão/logout no Header.
 * Acessível: aria-haspopup/expanded, role=menu/menuitem, navegação por setas,
 * ESC/click-fora fecham e o foco retorna ao gatilho.
 */
export const UserMenu: React.FC = () => {
  const { t } = useTranslation("nav")
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const menuId = useId()

  // Fecha em click-fora e ESC; ESC devolve o foco ao gatilho.
  useEffect(() => {
    if (!open) return
    const onPointer = (e: MouseEvent) => {
      if (!containerRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false)
        triggerRef.current?.focus()
      }
    }
    document.addEventListener("mousedown", onPointer)
    document.addEventListener("keydown", onKey)
    return () => {
      document.removeEventListener("mousedown", onPointer)
      document.removeEventListener("keydown", onKey)
    }
  }, [open])

  // Foca o primeiro item ao abrir.
  useEffect(() => {
    if (!open) return
    const id = window.setTimeout(() => {
      menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus()
    }, 0)
    return () => window.clearTimeout(id)
  }, [open])

  const onMenuKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])
    if (items.length === 0) return
    const index = items.indexOf(document.activeElement as HTMLElement)
    if (e.key === "ArrowDown") {
      e.preventDefault()
      items[Math.min(index + 1, items.length - 1)]?.focus()
    } else if (e.key === "ArrowUp") {
      e.preventDefault()
      items[Math.max(index - 1, 0)]?.focus()
    } else if (e.key === "Home") {
      e.preventDefault()
      items[0]?.focus()
    } else if (e.key === "End") {
      e.preventDefault()
      items[items.length - 1]?.focus()
    }
  }

  const itemClass =
    "flex w-full items-center gap-2.5 px-3 py-2 text-sm text-text transition-colors hover:bg-surface-tertiary focus:bg-surface-tertiary focus:outline-none"

  // UserMenu só renderiza dentro do shell autenticado; guarda para o TS e robustez.
  if (!user) return null
  const displayName = user.display_name || user.username || t("userMenu.defaultName")

  return (
    <div ref={containerRef} className="relative">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        className={cn(
          "flex max-w-[12rem] items-center gap-2 rounded-md py-1.5 pl-1.5 pr-2 transition-colors",
          "hover:bg-sidebar-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500",
        )}
      >
        <span
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary-600 text-xs font-semibold text-white"
          aria-hidden="true"
        >
          {initials(displayName)}
        </span>
        <span className="hidden min-w-0 flex-col items-start leading-tight sm:flex">
          <span className="max-w-[8rem] truncate text-sm font-medium text-sidebar-text-active">{displayName}</span>
          <span className="text-[11px] capitalize text-sidebar-text">{user.role || t("userMenu.defaultRole")}</span>
        </span>
        <ChevronDownIcon
          size={16}
          aria-hidden="true"
          className={cn("shrink-0 text-sidebar-text transition-transform", open && "rotate-180")}
        />
      </button>

      {open && (
        <div
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label={t("userMenu.ariaLabel")}
          onKeyDown={onMenuKeyDown}
          className="absolute right-0 top-full z-dropdown mt-2 w-60 origin-top-right overflow-hidden rounded-lg border border-border bg-surface py-1 shadow-lg animate-slide-down"
        >
          <div className="border-b border-border px-3 py-2.5">
            <div className="truncate text-sm font-semibold text-text">{displayName}</div>
            <div className="flex items-center gap-1.5 text-xs text-text-secondary">
              <ShieldCheckIcon size={12} aria-hidden="true" />
              <span className="capitalize">{user.role || t("userMenu.defaultRole")}</span>
              {user.username && <span className="truncate">· {user.username}</span>}
            </div>
          </div>

          <button
            type="button"
            role="menuitem"
            className={itemClass}
            onClick={() => {
              setOpen(false)
              navigate("/settings/account")
            }}
          >
            <UserCogIcon size={16} aria-hidden="true" className="text-text-tertiary" />
            {t("userMenu.account")}
          </button>

          <button
            type="button"
            role="menuitem"
            className={itemClass}
            onClick={() => {
              setOpen(false)
              navigate("/settings/tokens")
            }}
          >
            <KeyIcon size={16} aria-hidden="true" className="text-text-tertiary" />
            {t("userMenu.tokens")}
          </button>

          <div className="my-1 border-t border-border" role="separator" />

          <button
            type="button"
            role="menuitem"
            className={cn(itemClass, "text-danger-600 hover:bg-danger-50 focus:bg-danger-50")}
            onClick={() => {
              setOpen(false)
              void logout()
            }}
          >
            <LogOutIcon size={16} aria-hidden="true" />
            {t("userMenu.signOut")}
          </button>
        </div>
      )}
    </div>
  )
}

export default UserMenu
