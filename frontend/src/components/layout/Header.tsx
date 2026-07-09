import type React from "react"
import { useTranslation } from "react-i18next"
import { MenuIcon, PanelLeftCloseIcon, PanelLeftOpenIcon, ShieldCheckIcon } from "lucide-react"
import { useAuth } from "@/contexts/AuthContext"
import { ThemeToggle } from "@/components/ui/ThemeToggle/ThemeToggle"
import { UserMenu } from "./UserMenu"

interface HeaderProps {
  /** Abre/fecha o drawer mobile (<lg). */
  onToggleSidebar: () => void
  sidebarOpen?: boolean
  /** Estado e toggle do rail colapsável (desktop, >=lg). */
  collapsed?: boolean
  onToggleCollapse?: () => void
}

export const Header: React.FC<HeaderProps> = ({
  onToggleSidebar,
  sidebarOpen = false,
  collapsed = false,
  onToggleCollapse,
}) => {
  const { t } = useTranslation("nav")
  const { companyName } = useAuth()

  return (
    <header className="z-sticky flex h-14 shrink-0 items-center justify-between gap-3 bg-sidebar px-3 sm:px-4">
      <div className="flex min-w-0 items-center gap-2">
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

        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary-600" aria-hidden="true">
          <ShieldCheckIcon size={18} className="text-white" />
        </div>
        <h1 className="min-w-0 truncate text-base font-bold tracking-tight text-sidebar-text-active" title={companyName}>
          {companyName}
        </h1>
      </div>

      <div className="flex shrink-0 items-center gap-1 sm:gap-2">
        <ThemeToggle />
        <UserMenu />
      </div>
    </header>
  )
}
