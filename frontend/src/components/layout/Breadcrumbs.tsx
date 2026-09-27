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
  "/collectors": "collectors",
  "/destinations": "destinations",
  "/routes": "routes",
  "/flow": "flow",
  "/detections": "detections",
  "/history": "history",
  "/mappings": "mappings",
  "/drift": "drift",
  "/quarantine": "quarantine",
  "/pipeline-health": "pipelineHealth",
  "/queries": "queries",
  "/schedules": "schedules",
  "/users": "users",
  "/config": "config",
  "/admin": "admin",
  "/admin/users": "adminUsers",
  "/admin/service-accounts": "adminServiceAccounts",
  "/admin/ocsf": "adminOcsf",
  "/settings": "settings",
  "/settings/account": "settingsAccount",
  "/settings/tokens": "settingsTokens",
  "/enrichment": "enrichment",
  "/enrichment/policies": "enrichmentPolicies",
  "/enrichment/sources": "enrichmentSources",
  "/enrichment/tables": "enrichmentTables",
  "/enrichment/catalog": "enrichmentCatalog",
  "/enrichment/execution": "enrichmentExecution",
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

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
    const isId = /^\d+$/.test(segment) || UUID_RE.test(segment)
    // Evita expor ID cru (/integrations/123, /enrichment/policies/<uuid>) e slug
    // bruto de rotas sem rótulo.
    const routeKey = routeLabelKeys[currentPath]
    const label = routeKey ? t(`breadcrumbs.routes.${routeKey}`) : isId ? t("breadcrumbs.detail") : segment
    // Segmento intermediário só vira link se for uma PÁGINA conhecida. Montar o
    // link a partir do caminho acumulado, sem saber se há rota ali, foi o que
    // mandava `/enrichment/policies/<id>` para `/enrichment/policies` — um 404
    // oferecido pela própria navegação.
    const navigable = !isLast && routeKey !== undefined && !nonNavigableGroups.has(currentPath)
    breadcrumbs.push({
      key: currentPath,
      label,
      path: navigable ? currentPath : undefined,
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
            <li key={item.key} className="flex min-w-0 items-center gap-1.5">
              {index > 0 && <ChevronRightIcon size={14} className="text-text-tertiary" aria-hidden="true" />}

              {item.path ? (
                <Link
                  to={item.path}
                  className="flex min-w-0 items-center gap-1 rounded transition-colors hover:text-primary-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                  aria-label={index === 0 ? t("breadcrumbs.backToHome") : t("breadcrumbs.goTo", { label: item.label })}
                >
                  {index === 0 && <HomeIcon size={14} aria-hidden="true" className="shrink-0" />}
                  {/* LAY-26: segmento de rota pode ser um slug/id longo — trunca em
                      vez de estourar a linha, e o texto completo continua acessível
                      via `title` no hover. */}
                  <span className="max-w-[12rem] truncate" title={item.label}>
                    {item.label}
                  </span>
                </Link>
              ) : (
                <span
                  className="max-w-[16rem] truncate font-medium text-text"
                  title={item.label}
                  aria-current={isLast ? "page" : undefined}
                >
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
