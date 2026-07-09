import type React from "react"
import { useEffect, useRef } from "react"
import { NavLink } from "react-router-dom"
import { useTranslation } from "react-i18next"
import {
  LayoutDashboardIcon,
  BuildingIcon,
  PlugIcon,
  BellIcon,
  HistoryIcon,
  FileTextIcon,
  CalendarIcon,
  SettingsIcon,
  BotIcon,
  UserPlusIcon,
  ZapIcon,
  SendIcon,
  GitBranchIcon,
  ShieldAlertIcon,
  ShieldCheckIcon,
  NetworkIcon,
  ActivityIcon,
  PackageXIcon,
  HeartPulseIcon,
  KeyIcon,
  UserCogIcon,
  LayoutTemplateIcon,
  XIcon,
} from "lucide-react"
import { useAuth } from "@/contexts/AuthContext"
import { eeNavItems } from "@/ee/navItems"
import { usePermission } from "@/hooks/usePermission"
import { cn } from "@/lib/utils"

interface NavItem {
  key: string
  label: string
  path: string
  icon: React.ReactNode
}

interface NavGroup {
  label: string
  items: NavItem[]
}

interface NavigationProps {
  /** Drawer aberto (mobile/tablet, <lg). */
  open?: boolean
  onClose?: () => void
  /** Rail só-ícones no desktop (>=lg). Não afeta o drawer mobile. */
  collapsed?: boolean
}

/**
 * Item de navegação. Estado ativo sutil (fundo elevado + barra de acento à
 * esquerda no estilo Linear) em vez do pill azul sólido anterior — reduz ruído
 * visual. No modo colapsado (lg) vira só-ícone, com label acessível via
 * aria-label e tooltip nativo via title.
 */
const NavItemLink: React.FC<{ item: NavItem; collapsed: boolean; onNavigate?: () => void }> = ({
  item,
  collapsed,
  onNavigate,
}) => (
  <li>
    <NavLink
      to={item.path}
      onClick={onNavigate}
      aria-label={item.label}
      title={collapsed ? item.label : undefined}
      className={({ isActive }) =>
        cn(
          "group relative mx-2 flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500",
          collapsed && "lg:mx-2 lg:justify-center lg:gap-0 lg:px-0",
          isActive
            ? "bg-sidebar-active font-medium text-sidebar-text-active"
            : "text-sidebar-text hover:bg-sidebar-hover hover:text-sidebar-text-active",
        )
      }
    >
      {({ isActive }) => (
        <>
          {/* Barra de acento do item ativo */}
          <span
            aria-hidden="true"
            className={cn(
              "absolute left-0 top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-r-full bg-sidebar-accent transition-opacity",
              isActive ? "opacity-100" : "opacity-0",
            )}
          />
          <span className="shrink-0" aria-hidden="true">
            {item.icon}
          </span>
          <span className={cn("truncate", collapsed && "lg:hidden")}>{item.label}</span>
        </>
      )}
    </NavLink>
  </li>
)

export const Navigation: React.FC<NavigationProps> = ({ open = false, onClose, collapsed = false }) => {
  const { t } = useTranslation("nav")
  const { user } = useAuth()
  const isAdmin = user?.role === "admin"
  const canManageUsers = usePermission("user.manage")
  const canRunQuery = usePermission("query.run")
  const canSaveQuery = usePermission("query.save")
  const navRef = useRef<HTMLElement>(null)
  const previousActive = useRef<HTMLElement | null>(null)

  // No drawer mobile: ao abrir, move o foco para dentro e o prende (Tab/Shift+Tab
  // circulam); ESC fecha; ao fechar, RESTAURA o foco ao elemento que abriu
  // (gatilho hambúrguer) — convenção de dialog modal (WCAG 2.4.3).
  useEffect(() => {
    if (!open) return
    const node = navRef.current
    previousActive.current = document.activeElement as HTMLElement | null

    const getFocusables = () =>
      Array.from(
        node?.querySelectorAll<HTMLElement>('a[href], button, [tabindex]:not([tabindex="-1"])') ?? [],
      ).filter((el) => !el.hasAttribute("disabled") && el.offsetParent !== null)

    getFocusables()[0]?.focus()

    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose?.()
        return
      }
      if (e.key !== "Tab") return
      const focusables = getFocusables()
      if (focusables.length === 0) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener("keydown", handler)

    return () => {
      document.removeEventListener("keydown", handler)
      const previous = previousActive.current
      if (previous && typeof previous.focus === "function" && previous.offsetParent !== null) {
        previous.focus()
      }
    }
  }, [open, onClose])

  const groups: NavGroup[] = [
    {
      label: t("navigation.groups.overview"),
      items: [
        { key: "dashboard", label: t("navigation.items.dashboard"), path: "/dashboard", icon: <LayoutDashboardIcon size={18} /> },
        ...(isAdmin
          ? [{ key: "organizations", label: t("navigation.items.organizations"), path: "/organizations", icon: <BuildingIcon size={18} /> }]
          : []),
        { key: "integrations", label: t("navigation.items.integrations"), path: "/integrations", icon: <PlugIcon size={18} /> },
      ],
    },
    {
      label: t("navigation.groups.operations"),
      items: [
        { key: "alerts", label: t("navigation.items.alerts"), path: "/alerts", icon: <BellIcon size={18} /> },
        { key: "collectors", label: t("navigation.items.collectors"), path: "/collectors", icon: <ZapIcon size={18} /> },
        ...(isAdmin
          ? [
              { key: "destinations", label: t("navigation.items.destinations"), path: "/destinations", icon: <SendIcon size={18} /> },
              { key: "routes", label: t("navigation.items.routes"), path: "/routes", icon: <GitBranchIcon size={18} /> },
              { key: "flow", label: t("navigation.items.flow"), path: "/flow", icon: <NetworkIcon size={18} /> },
            ]
          : []),
        ...(canRunQuery
          ? [{ key: "detections", label: t("navigation.items.detections"), path: "/detections", icon: <ShieldAlertIcon size={18} /> }]
          : []),
        { key: "history", label: t("navigation.items.history"), path: "/history", icon: <HistoryIcon size={18} /> },
      ],
    },
    {
      label: t("navigation.groups.normalization"),
      items: [
        { key: "mappings", label: t("navigation.items.mappings"), path: "/mappings", icon: <LayoutTemplateIcon size={18} /> },
        { key: "drift", label: t("navigation.items.drift"), path: "/drift", icon: <ActivityIcon size={18} /> },
        { key: "quarantine", label: t("navigation.items.quarantine"), path: "/quarantine", icon: <PackageXIcon size={18} /> },
        { key: "health", label: t("navigation.items.health"), path: "/pipeline-health", icon: <HeartPulseIcon size={18} /> },
      ],
    },
    {
      label: t("navigation.groups.knowledge"),
      items: [
        { key: "queries", label: t("navigation.items.queries"), path: "/queries", icon: <FileTextIcon size={18} /> },
        ...(isAdmin
          ? [{ key: "schedules", label: t("navigation.items.schedules"), path: "/schedules", icon: <CalendarIcon size={18} /> }]
          : []),
      ],
    },
    {
      label: t("navigation.groups.administration"),
      items: [
        ...(canManageUsers
          ? [
              { key: "admin-users", label: t("navigation.items.adminUsers"), path: "/admin/users", icon: <UserPlusIcon size={18} /> },
              {
                key: "service-accounts",
                label: t("navigation.items.serviceAccounts"),
                path: "/admin/service-accounts",
                icon: <BotIcon size={18} />,
              },
            ]
          : []),
        ...(isAdmin
          ? [{ key: "ocsf", label: t("navigation.items.ocsf"), path: "/admin/ocsf", icon: <ShieldCheckIcon size={18} /> }]
          : []),
        ...(isAdmin ? [{ key: "config", label: t("navigation.items.config"), path: "/config", icon: <SettingsIcon size={18} /> }] : []),
      ],
    },
    {
      label: t("navigation.groups.account"),
      items: [
        { key: "account", label: t("navigation.items.account"), path: "/settings/account", icon: <UserCogIcon size={18} /> },
        { key: "tokens", label: t("navigation.items.tokens"), path: "/settings/tokens", icon: <KeyIcon size={18} /> },
      ],
    },
  ]

  // Inject the Enterprise federated-search links (Busca federada,
  // Correlação) into their groups. Empty in Community (the @/ee/navItems stub) → no
  // sidebar entry for routes the Community bundle doesn't ship.
  const eeExtra = eeNavItems({ canRunQuery, canSaveQuery, isAdmin })
  for (const g of groups) {
    const extra = eeExtra[g.label]
    if (extra?.length) g.items.push(...extra)
  }

  const visibleGroups = groups.filter((g) => g.items.length > 0)

  return (
    <nav
      ref={navRef}
      id="primary-navigation"
      className={cn(
        "flex w-56 shrink-0 flex-col overflow-y-auto overflow-x-hidden bg-sidebar py-4 scrollbar-thin",
        // Comportamento de drawer abaixo de lg
        "fixed inset-y-0 left-0 z-modal transition-[transform,width] duration-200 ease-out",
        "lg:static lg:z-auto lg:translate-x-0",
        collapsed ? "lg:w-16" : "lg:w-56",
        open ? "translate-x-0 shadow-xl lg:shadow-none" : "-translate-x-full",
      )}
      role={open ? "dialog" : undefined}
      aria-label={t("navigation.ariaLabel")}
      aria-modal={open ? "true" : undefined}
    >
      {/* Botão de fechar — só no drawer mobile */}
      <div className="mb-2 flex justify-end px-4 lg:hidden">
        <button
          type="button"
          onClick={onClose}
          aria-label={t("header.closeMenu")}
          className="rounded-md p-1.5 text-sidebar-text transition-colors hover:bg-sidebar-hover hover:text-sidebar-text-active focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
        >
          <XIcon size={20} />
        </button>
      </div>

      {visibleGroups.map((group) => (
        <div key={group.label} className="mb-4">
          <div
            className={cn(
              "mb-1 px-5 text-[10px] font-semibold uppercase tracking-widest text-sidebar-group",
              collapsed && "lg:hidden",
            )}
          >
            {group.label}
          </div>
          <ul className="flex flex-col gap-0.5">
            {group.items.map((item) => (
              <NavItemLink key={item.key} item={item} collapsed={collapsed} onNavigate={onClose} />
            ))}
          </ul>
        </div>
      ))}
    </nav>
  )
}
