import type React from "react"
import { Suspense, useCallback, useEffect, useState } from "react"
import { matchPath, Outlet, useLocation } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { Navigation } from "./Navigation"
import { Header } from "./Header"
import { GlobalFilters } from "./GlobalFilters"
import { Breadcrumbs } from "./Breadcrumbs"
import { ErrorBoundary } from "@/components/shared/ErrorBoundary"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { cn } from "@/lib/utils"

const COLLAPSE_KEY = "centralops_sidebar_collapsed"
const LG_BREAKPOINT = 1024

/**
 * Shell global da aplicação.
 * Orquestra dois eixos de estado da sidebar e os distribui para Header/Navigation:
 *  - `sidebarOpen`: drawer mobile/tablet (<lg).
 *  - `collapsed`: rail só-ícones no desktop (>=lg), persistido.
 * Também trava o scroll do corpo enquanto o drawer está aberto e isola falhas de
 * render por rota num ErrorBoundary, preservando o chrome (sidebar/header).
 */
export const AppLayout: React.FC = () => {
  const { t } = useTranslation("nav")
  const location = useLocation()
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    if (typeof window === "undefined") return false
    return window.localStorage.getItem(COLLAPSE_KEY) === "1"
  })

  // Editores densos (ex.: /mappings/:id) ocupam largura total e colapsam a
  // sidebar por padrão para maximizar a área de trabalho — sem alterar a
  // preferência global persistida; o usuário ainda pode expandir dentro do editor.
  const isEditorRoute = matchPath("/mappings/:id", location.pathname) != null
  const [editorRailExpanded, setEditorRailExpanded] = useState(false)
  const effectiveCollapsed = isEditorRoute ? !editorRailExpanded : collapsed

  const openSidebar = useCallback(() => setSidebarOpen(true), [])
  const closeSidebar = useCallback(() => setSidebarOpen(false), [])
  const toggleCollapse = useCallback(() => {
    if (isEditorRoute) {
      setEditorRailExpanded((v) => !v)
      return
    }
    setCollapsed((prev) => {
      const next = !prev
      window.localStorage.setItem(COLLAPSE_KEY, next ? "1" : "0")
      return next
    })
  }, [isEditorRoute])

  // Ao cruzar para o desktop, garante que o drawer mobile não fique "preso" aberto.
  useEffect(() => {
    const onResize = () => {
      if (window.innerWidth >= LG_BREAKPOINT) setSidebarOpen(false)
    }
    window.addEventListener("resize", onResize)
    return () => window.removeEventListener("resize", onResize)
  }, [])

  // Trava o scroll do corpo enquanto o drawer está aberto (evita scroll do conteúdo atrás).
  useEffect(() => {
    if (!sidebarOpen) return
    const previous = document.body.style.overflow
    document.body.style.overflow = "hidden"
    return () => {
      document.body.style.overflow = previous
    }
  }, [sidebarOpen])

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-surface-secondary">
      <a href="#main-content" className="skip-link">
        {t("skipToContent")}
      </a>

      <Header
        onToggleSidebar={sidebarOpen ? closeSidebar : openSidebar}
        sidebarOpen={sidebarOpen}
        collapsed={effectiveCollapsed}
        onToggleCollapse={toggleCollapse}
      />

      <div className="flex flex-1 overflow-hidden">
        {/* Backdrop — só mobile/tablet, fecha o drawer ao clicar */}
        <div
          className={cn(
            "fixed inset-0 z-modal-backdrop bg-overlay transition-opacity duration-200 lg:hidden",
            sidebarOpen ? "opacity-100 pointer-events-auto" : "opacity-0 pointer-events-none",
          )}
          aria-hidden="true"
          onClick={closeSidebar}
        />

        <Navigation open={sidebarOpen} onClose={closeSidebar} collapsed={effectiveCollapsed} />

        {/* Coluna de conteúdo. aria-hidden quando o drawer cobre a tela (a11y do modal). */}
        <div className="flex flex-1 flex-col overflow-hidden" aria-hidden={sidebarOpen || undefined}>
          <GlobalFilters />

          <main id="main-content" className="flex-1 overflow-y-auto bg-surface-secondary" aria-label={t("mainContent")}>
            {/* Editor denso = full-bleed (sem max-w-7xl); demais rotas = container centrado de leitura. */}
            <div className={isEditorRoute ? "px-3 py-4 sm:px-4" : "mx-auto max-w-7xl px-4 py-6 sm:px-6"}>
              <Breadcrumbs />
              {/* Boundary por-rota: um erro de página não derruba o shell; reseta ao navegar.
                  Suspense cobre o carregamento dos chunks lazy das rotas, preservando o shell. */}
              <ErrorBoundary variant="inline" resetKey={location.pathname}>
                <Suspense fallback={<LoadingSpinner size="lg" text={t("appLoading.loading")} className="py-20" />}>
                  <Outlet />
                </Suspense>
              </ErrorBoundary>
            </div>
          </main>
        </div>
      </div>
    </div>
  )
}
