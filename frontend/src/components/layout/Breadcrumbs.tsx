import type React from "react"
import { Link, useLocation } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { ChevronRightIcon, HomeIcon } from "lucide-react"

interface BreadcrumbItem {
  /** Chave estável para render (caminho acumulado ou marcador raiz). */
  key: string
  label: string
  path?: string
}

// Mapeia cada rota para sua chave de tradução em `nav:breadcrumbs.routes`.
const routeLabelKeys: Record<string, string> = {
  "/dashboard": "dashboard",
  "/organizations": "organizations",
  "/integrations": "integrations",
  "/alerts": "alerts",
  "/collectors": "collectors",
  "/flow": "flow",
  "/history": "history",
  "/mappings": "mappings",
  "/drift": "drift",
  "/quarantine": "quarantine",
  "/pipeline-health": "pipelineHealth",
  "/queries": "queries",
  "/schedules": "schedules",
  "/clients": "clients",
  "/users": "users",
  "/config": "config",
  "/admin": "admin",
  "/admin/users": "adminUsers",
  "/admin/service-accounts": "adminServiceAccounts",
  "/settings": "settings",
  "/settings/tokens": "settingsTokens",
}

// Segmentos que agrupam rotas mas não têm página própria — não devem virar link.
const nonNavigableGroups = new Set(["/admin", "/settings"])

export const Breadcrumbs: React.FC = () => {
  const { t } = useTranslation("nav")
  const location = useLocation()
  const pathSegments = location.pathname.split("/").filter(Boolean)

  const homeLabel = t("breadcrumbs.home")
  const breadcrumbs: BreadcrumbItem[] = [{ key: "/", label: homeLabel, path: "/" }]

  let currentPath = ""
  pathSegments.forEach((segment) => {
    currentPath += `/${segment}`
    const isLast = currentPath === location.pathname
    const isNumericId = /^\d+$/.test(segment)
    // Evita expor ID cru (/integrations/123) e slug bruto de rotas sem rótulo.
    const routeKey = routeLabelKeys[currentPath]
    const label = routeKey ? t(`breadcrumbs.routes.${routeKey}`) : isNumericId ? t("breadcrumbs.detail") : segment
    breadcrumbs.push({
      key: currentPath,
      label,
      path: isLast || nonNavigableGroups.has(currentPath) ? undefined : currentPath,
    })
  })

  if (location.pathname === "/") {
    breadcrumbs.push({ key: "/home", label: homeLabel })
  }

  if (breadcrumbs.length <= 1) return null

  return (
    <nav className="mb-4 text-sm text-text-secondary" aria-label={t("breadcrumbs.ariaLabel")}>
      <ol className="flex flex-wrap items-center gap-1.5">
        {breadcrumbs.map((item, index) => {
          const isLast = index === breadcrumbs.length - 1
          return (
            <li key={item.key} className="flex items-center gap-1.5">
              {index > 0 && <ChevronRightIcon size={14} className="text-text-tertiary" aria-hidden="true" />}

              {item.path ? (
                <Link
                  to={item.path}
                  className="flex items-center gap-1 rounded transition-colors hover:text-primary-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                  aria-label={index === 0 ? t("breadcrumbs.backToHome") : t("breadcrumbs.goTo", { label: item.label })}
                >
                  {index === 0 && <HomeIcon size={14} aria-hidden="true" />}
                  <span>{item.label}</span>
                </Link>
              ) : (
                <span className="font-medium text-text" aria-current={isLast ? "page" : undefined}>
                  {item.label}
                </span>
              )}
            </li>
          )
        })}
      </ol>
    </nav>
  )
}
