import type React from "react"
import { useTranslation } from "react-i18next"
import { MenuIcon, PanelLeftCloseIcon, PanelLeftOpenIcon, SearchIcon } from "lucide-react"
import { useAuth } from "@/contexts/AuthContext"
import { UserMenu } from "./UserMenu"

interface HeaderProps {
  /** Abre/fecha o drawer mobile (<lg). */
  onToggleSidebar: () => void
  sidebarOpen?: boolean
  /** Estado e toggle do rail colapsável (desktop, >=lg). */
  collapsed?: boolean
  onToggleCollapse?: () => void
}

function isApplePlatform(): boolean {
  if (typeof navigator === "undefined") return false
  return /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent)
}

/**
 * Abre a paleta de comandos reemitindo o atalho que ela já escuta na window.
 * A paleta é montada uma única vez no App (fora do shell), então este é o
 * caminho que evita arrastar estado de abertura por toda a árvore do layout.
 */
function openCommandPalette(): void {
  window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true, ctrlKey: true, bubbles: true }))
}

/**
 * Header do console: onde estou (nome da instalação), busca/paleta de comandos e
 * quem sou. Os dois toggles de sidebar ficam porque controlam a navegação; o
 * resto saiu. Sem alternador de tema — o tema é único.
 */
export const Header: React.FC<HeaderProps> = ({
  onToggleSidebar,
  sidebarOpen = false,
  collapsed = false,
  onToggleCollapse,
}) => {
  const { t } = useTranslation("nav")
  const { companyName } = useAuth()
  const shortcut = isApplePlatform() ? "⌘K" : "Ctrl K"

  return (
    <header className="z-sticky flex h-14 shrink-0 items-center gap-2 bg-sidebar px-3 sm:gap-3 sm:px-4">
      <div className="flex min-w-0 items-center gap-1">
        {/* Hambúrguer — só mobile/tablet */}
        <button
          type="button"
          onClick={onToggleSidebar}
          aria-label={sidebarOpen ? t("header.closeMenu") : t("header.openMenu")}
          aria-expanded={sidebarOpen}
          aria-controls="primary-navigation"
          className="inline-flex h-9 w-9 items-center justify-center rounded-md text-sidebar-text transition-colors hover:bg-sidebar-hover hover:text-sidebar-text-active focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 lg:hidden"
        >
          <MenuIcon size={20} aria-hidden="true" />
        </button>

        {/* Colapsar/expandir rail — só desktop */}
        <button
          type="button"
          onClick={onToggleCollapse}
          aria-label={collapsed ? t("header.expandSidebar") : t("header.collapseSidebar")}
          aria-expanded={!collapsed}
          aria-controls="primary-navigation"
          className="hidden h-9 w-9 items-center justify-center rounded-md text-sidebar-text transition-colors hover:bg-sidebar-hover hover:text-sidebar-text-active focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 lg:inline-flex"
        >
          {collapsed ? <PanelLeftOpenIcon size={20} aria-hidden="true" /> : <PanelLeftCloseIcon size={20} aria-hidden="true" />}
        </button>

        <h1
          className="ml-1 min-w-0 truncate font-display text-base font-semibold tracking-tight text-sidebar-text-active"
          title={companyName}
        >
          {companyName}
        </h1>
      </div>

      {/* Busca / paleta de comandos */}
      <button
        type="button"
        onClick={openCommandPalette}
        aria-label={t("header.search")}
        aria-keyshortcuts="Meta+K Control+K"
        className="ml-auto flex h-9 shrink-0 items-center gap-2 rounded-md border border-border px-2.5 text-sm text-sidebar-text transition-colors hover:border-border-hover hover:text-sidebar-text-active focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 sm:w-64"
      >
        <SearchIcon size={16} aria-hidden="true" className="shrink-0" />
        <span className="hidden flex-1 text-left sm:inline">{t("header.search")}</span>
        <kbd className="hidden shrink-0 rounded border border-border px-1.5 py-0.5 font-mono text-[11px] text-sidebar-text sm:inline">
          {shortcut}
        </kbd>
      </button>

      <UserMenu />
    </header>
  )
}
