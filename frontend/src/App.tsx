import { lazy, useEffect, useMemo, useState } from "react"
import type React from "react"
import { BrowserRouter, Navigate, Route, Routes, useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { AppLayout } from "./components/layout/AppLayout"
import { AuthProvider, useAuth } from "./contexts/AuthContext"
import { EditionProvider } from "./contexts/EditionContext"
import { PlatformProvider } from "./contexts/PlatformContext"
import { ErrorBoundary } from "./components/shared/ErrorBoundary"
import { RequirePermission } from "./components/auth/RequirePermission"
import * as api from "./services/api"
import { CommandPalette } from "./components/ui/CommandPalette"
import type { PaletteCommand } from "./components/ui/CommandPalette"
import {
  LayoutDashboardIcon,
  PlugZapIcon,
  ServerIcon,
  RouteIcon,
  DatabaseIcon,
  ShieldAlertIcon,
  NetworkIcon,
  MapIcon,
} from "lucide-react"

// Pages — code-splitting por rota (React.lazy). O usuário que abre só o Dashboard
// não baixa mais o JS de editores/admin pesados no bundle inicial. LoginPage e
// NotFoundPage ficam eager por serem leves e estarem fora do <Suspense> do shell.
import LoginPage from "./pages/LoginPage"
import NotFoundPage from "./pages/NotFoundPage"

// Enterprise edition routes. Empty in the Community build; the Enterprise
// build overrides `@/ee/routes` via resolve.alias.
import { eeRoutes } from "@/ee/routes"
const DashboardPage = lazy(() => import("./pages/DashboardPage"))
const OrganizationsPage = lazy(() => import("./pages/OrganizationsPage"))
const IntegrationsPage = lazy(() => import("./pages/IntegrationsPage"))
const IntegrationDetailPage = lazy(() => import("./pages/IntegrationDetailPage"))
const HistoryPage = lazy(() => import("./pages/HistoryPage"))
const QueriesPage = lazy(() => import("./pages/QueriesPage"))
const SchedulesPage = lazy(() => import("./pages/SchedulesPage"))
// QueryJobsPage + CorrelationRulesPage (federated search)
// ship only in the EE overlay (lazy-loaded via @/ee/routes). DetectionsPage STAYS — the
// Community scheduler emits Detections; triage is base SOC.
const DetectionsPage = lazy(() => import("./pages/DetectionsPage"))
const ConfigPage = lazy(() => import("./pages/ConfigPage"))
const CollectorsPage = lazy(() => import("./pages/CollectorsPage"))
const DestinationsPage = lazy(() => import("./pages/DestinationsPage"))
const DestinationDetailPage = lazy(() => import("./pages/DestinationDetailPage"))
const RoutesPage = lazy(() => import("./pages/RoutesPage"))
const FlowPage = lazy(() => import("./pages/FlowPage"))
const AdminUsersPage = lazy(() => import("./pages/AdminUsersPage"))
const OcsfGovernancePage = lazy(() => import("./pages/OcsfGovernancePage"))
const MappingEditorPage = lazy(() => import("./pages/MappingEditorPage"))
const PipelineHealthPage = lazy(() => import("./pages/PipelineHealthPage"))
const DriftExplorerPage = lazy(() => import("./pages/DriftExplorerPage"))
const QuarantinePage = lazy(() => import("./pages/QuarantinePage"))
const MappingsListPage = lazy(() => import("./pages/MappingsListPage"))
const ServiceAccountsPage = lazy(() => import("./pages/ServiceAccountsPage"))
const TokensPage = lazy(() => import("./pages/TokensPage"))
const AccountSettingsPage = lazy(() => import("./pages/AccountSettingsPage"))

const AppLoadingScreen: React.FC = () => {
  // Mostra `companyName` do AuthContext quando já carregou o /api/auth/status;
  // antes disso, exibe um placeholder neutro ("Carregando..."). Evita
  // hardcode "CentralOps" que ignora a customização via APP_COMPANY_NAME.
  const { t } = useTranslation("nav")
  const { companyName, loading } = useAuth()
  return (
    <div className="flex min-h-screen items-center justify-center bg-sidebar px-6" role="status" aria-live="polite">
      <div className="w-full max-w-sm rounded-3xl border border-border bg-surface p-8 text-center shadow-xl">
        <div className="mx-auto mb-5 h-10 w-10 animate-spin rounded-full border-4 border-primary-100 border-t-primary-600" aria-hidden="true" />
        {/* <p> em vez de <h1>: tela transitória; o anúncio acessível já vem de role=status. */}
        <p className="text-lg font-semibold text-text">
          {loading ? t("appLoading.loading") : companyName}
        </p>
        <p className="mt-2 text-sm text-text-secondary">{t("appLoading.validatingSession")}</p>
      </div>
    </div>
  )
}

const HomeRedirect: React.FC = () => {
  const { user } = useAuth()

  if (!user) {
    return <Navigate to="/login" replace />
  }

  return <Navigate to="/dashboard" replace />
}

const LoginRoute: React.FC = () => {
  const { user, loading } = useAuth()

  if (loading) {
    return <AppLoadingScreen />
  }

  if (user) {
    return <HomeRedirect />
  }

  return <LoginPage />
}

const ProtectedLayout: React.FC = () => {
  const { user, loading } = useAuth()

  if (loading) {
    return <AppLoadingScreen />
  }

  if (!user) {
    return <Navigate to="/login" replace />
  }

  return (
    <PlatformProvider>
      <EditionProvider>
        <AppLayout />
      </EditionProvider>
    </PlatformProvider>
  )
}

interface RoleGuardProps {
  role: "admin"
  children: React.ReactElement
}

const ForbiddenRedirectListener: React.FC = () => {
  const navigate = useNavigate()

  useEffect(() => {
    const handleForbidden = (event: Event) => {
      const redirectTo = (event as CustomEvent<{ redirectTo?: string }>).detail?.redirectTo || "/dashboard"
      navigate(redirectTo, { replace: true })
    }

    window.addEventListener("app-api-forbidden", handleForbidden)
    return () => {
      window.removeEventListener("app-api-forbidden", handleForbidden)
    }
  }, [navigate])

  return null
}

const RoleGuard: React.FC<RoleGuardProps> = ({ role, children }) => {
  const { user } = useAuth()
  const [adminAccessState, setAdminAccessState] = useState<"idle" | "checking" | "allowed" | "blocked">("idle")

  useEffect(() => {
    if (!user || role !== "admin" || user.role !== "admin") {
      setAdminAccessState("idle")
      return
    }

    let isCancelled = false
    setAdminAccessState("checking")

    void api
      .verifyAdminAccess()
      .then(() => {
        if (!isCancelled) {
          setAdminAccessState("allowed")
        }
      })
      .catch(() => {
        if (!isCancelled) {
          setAdminAccessState("blocked")
        }
      })

    return () => {
      isCancelled = true
    }
  }, [role, user?.id, user?.role])

  if (!user) {
    return <Navigate to="/login" replace />
  }

  if (role === "admin") {
    if (user.role !== "admin") {
      return <Navigate to="/dashboard" replace />
    }

    if (adminAccessState === "idle" || adminAccessState === "checking") {
      return <AppLoadingScreen />
    }

    if (adminAccessState === "blocked") {
      return <Navigate to="/dashboard" replace />
    }

    return children
  }

  return children
}

/**
 * AppCommandPalette — monta a paleta ⌘K uma única vez no nível do app.
 * Precisa estar dentro do BrowserRouter para usar useNavigate.
 */
const AppCommandPalette: React.FC = () => {
  const { t } = useTranslation("nav")
  const navigate = useNavigate()

  const commands: PaletteCommand[] = useMemo(
    () => [
      {
        id: "nav-dashboard",
        label: t("commandPalette.items.dashboard.label"),
        group: t("commandPalette.groups.navigate"),
        keywords: t("commandPalette.items.dashboard.keywords").split(", "),
        icon: <LayoutDashboardIcon size={16} />,
        run: () => navigate("/dashboard"),
      },
      {
        id: "nav-integrations",
        label: t("commandPalette.items.integrations.label"),
        group: t("commandPalette.groups.navigate"),
        keywords: t("commandPalette.items.integrations.keywords").split(", "),
        icon: <PlugZapIcon size={16} />,
        run: () => navigate("/integrations"),
      },
      {
        id: "nav-collectors",
        label: t("commandPalette.items.collectors.label"),
        group: t("commandPalette.groups.navigate"),
        keywords: t("commandPalette.items.collectors.keywords").split(", "),
        icon: <ServerIcon size={16} />,
        run: () => navigate("/collectors"),
      },
      {
        id: "nav-destinations",
        label: t("commandPalette.items.destinations.label"),
        group: t("commandPalette.groups.navigate"),
        keywords: t("commandPalette.items.destinations.keywords").split(", "),
        icon: <DatabaseIcon size={16} />,
        run: () => navigate("/destinations"),
      },
      {
        id: "nav-routes",
        label: t("commandPalette.items.routes.label"),
        group: t("commandPalette.groups.navigate"),
        keywords: t("commandPalette.items.routes.keywords").split(", "),
        icon: <RouteIcon size={16} />,
        run: () => navigate("/routes"),
      },
      {
        id: "nav-quarantine",
        label: t("commandPalette.items.quarantine.label"),
        group: t("commandPalette.groups.navigate"),
        keywords: t("commandPalette.items.quarantine.keywords").split(", "),
        icon: <ShieldAlertIcon size={16} />,
        run: () => navigate("/quarantine"),
      },
      {
        id: "nav-mappings",
        label: t("commandPalette.items.mappings.label"),
        group: t("commandPalette.groups.navigate"),
        keywords: t("commandPalette.items.mappings.keywords").split(", "),
        icon: <MapIcon size={16} />,
        run: () => navigate("/mappings"),
      },
      {
        id: "nav-pipeline-health",
        label: t("commandPalette.items.pipelineHealth.label"),
        group: t("commandPalette.groups.navigate"),
        keywords: t("commandPalette.items.pipelineHealth.keywords").split(", "),
        icon: <NetworkIcon size={16} />,
        run: () => navigate("/pipeline-health"),
      },
    ],
    [navigate, t],
  )

  return <CommandPalette commands={commands} />
}

const AppRoutes: React.FC = () => {
  return (
    <Routes>
      <Route path="/login" element={<LoginRoute />} />

      <Route path="/" element={<ProtectedLayout />}>
        <Route index element={<HomeRedirect />} />

        {/* Overview */}
        <Route path="dashboard" element={<DashboardPage />} />
        <Route
          path="organizations"
          element={
            <RoleGuard role="admin">
              <OrganizationsPage />
            </RoleGuard>
          }
        />
        <Route path="integrations" element={<IntegrationsPage />} />
        <Route path="integrations/:id" element={<IntegrationDetailPage />} />
        <Route path="mappings" element={<MappingsListPage />} />
        <Route path="mappings/:id" element={<MappingEditorPage />} />
        <Route path="drift" element={<DriftExplorerPage />} />
        <Route path="quarantine" element={<QuarantinePage />} />
        {/* "pipeline-health" e não "health": o nginx serve um healthcheck em /health
            que sombreava a rota SPA (page-load direto retornava "healthy"). */}
        <Route path="pipeline-health" element={<PipelineHealthPage />} />

        {/* Operations */}
        <Route path="collectors" element={<CollectorsPage />} />
        <Route
          path="destinations"
          element={
            <RoleGuard role="admin">
              <DestinationsPage />
            </RoleGuard>
          }
        />
        <Route
          path="destinations/:id"
          element={
            <RoleGuard role="admin">
              <DestinationDetailPage />
            </RoleGuard>
          }
        />
        <Route
          path="routes"
          element={
            <RoleGuard role="admin">
              <RoutesPage />
            </RoleGuard>
          }
        />
        <Route
          path="flow"
          element={
            <RoleGuard role="admin">
              <FlowPage />
            </RoleGuard>
          }
        />
        {/* triagem de detecções (QUERY_RUN; PATCH exige query.run).
            query-jobs (busca federada) ship via @/ee/routes (EE overlay). */}
        <Route
          path="detections"
          element={
            <RequirePermission perm="query.run" fallback={<Navigate to="/dashboard" replace />}>
              <DetectionsPage />
            </RequirePermission>
          }
        />
        <Route path="history" element={<HistoryPage />} />

        {/* Knowledge */}
        <Route path="queries" element={<QueriesPage />} />
        {/* correlation-rules ship via @/ee/routes. */}
        <Route
          path="schedules"
          element={
            <RoleGuard role="admin">
              <SchedulesPage />
            </RoleGuard>
          }
        />

        {/* /users (legado) → /admin/users com RBAC granular. */}
        <Route path="users" element={<Navigate to="/admin/users" replace />} />
        <Route
          path="admin/users"
          element={
            <RequirePermission perm="user.manage" fallback={<Navigate to="/dashboard" replace />}>
              <AdminUsersPage />
            </RequirePermission>
          }
        />
        <Route
          path="config"
          element={
            <RoleGuard role="admin">
              <ConfigPage />
            </RoleGuard>
          }
        />
        <Route
          path="admin/ocsf"
          element={
            <RoleGuard role="admin">
              <OcsfGovernancePage />
            </RoleGuard>
          }
        />

        {/* Conta do próprio usuário (qualquer user autenticado) — self-service */}
        <Route path="settings/account" element={<AccountSettingsPage />} />

        {/* Personal Access Tokens (qualquer user autenticado) */}
        <Route path="settings/tokens" element={<TokensPage />} />

        {/* Service Accounts (admin only via user.manage) */}
        <Route
          path="admin/service-accounts"
          element={
            <RequirePermission perm="user.manage" fallback={<Navigate to="/dashboard" replace />}>
              <ServiceAccountsPage />
            </RequirePermission>
          }
        />

        {/* Enterprise edition screens — injected at build time via the
            `virtual:ee-routes` seam, inside the protected shell and BEFORE the
            catch-all so EE paths are not shadowed by the 404. Empty in Community. */}
        {eeRoutes}

        {/* 404 dentro do shell (mantém sidebar/header) — substitui o redirect mudo para "/". */}
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  )
}

export default function App() {
  return (
    // Boundary global: último recurso para erros de render (inclusive nos providers/guards),
    // exibindo um fallback de página cheia acionável em vez de tela branca.
    <ErrorBoundary variant="page">
      <AuthProvider>
        <BrowserRouter>
          <ForbiddenRedirectListener />
          {/* Paleta ⌘K — montada uma vez; funciona em qualquer rota */}
          <AppCommandPalette />
          <AppRoutes />
        </BrowserRouter>
      </AuthProvider>
    </ErrorBoundary>
  )
}
