import type React from "react"
import { useMemo } from "react"
import { useLocation } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { AlertTriangleIcon, BuildingIcon, FilterIcon, PlugIcon, RefreshCwIcon, XIcon } from "lucide-react"
import { usePlatform } from "@/contexts/PlatformContext"
import { Select, type SelectOption, type SelectValue } from "@/components/ui/Select/Select"
import { Button } from "@/components/ui/Button/Button"
import type { PlatformType } from "@/types"

// Páginas de administração/conta não consomem os filtros globais (org/plataforma/
// integração) — esconder evita controles inertes que confundem o usuário.
const HIDDEN_PREFIXES = ["/config", "/admin", "/settings", "/users", "/organizations"]

const PLATFORM_LABEL: Record<string, string> = {
  sophos: "Sophos",
  wazuh: "Wazuh",
}

function platformLabel(p: string): string {
  return PLATFORM_LABEL[p] ?? p.charAt(0).toUpperCase() + p.slice(1)
}

function asNumber(value: SelectValue): number | null {
  if (Array.isArray(value) || value === "" || value === undefined) return null
  return Number(value)
}

export const GlobalFilters: React.FC = () => {
  const { t } = useTranslation("nav")
  const {
    organizations,
    integrations,
    filteredIntegrations,
    selectedOrgId,
    setSelectedOrgId,
    selectedPlatform,
    setSelectedPlatform,
    selectedIntegrationId,
    setSelectedIntegrationId,
    clearFilters,
    refreshData,
    loading,
    error,
  } = usePlatform()
  const location = useLocation()

  // Plataformas reais derivadas das integrações do backend (não hardcoded).
  const platforms = useMemo(
    () => Array.from(new Set(integrations.map((i) => i.platform).filter(Boolean))).sort(),
    [integrations],
  )

  const orgOptions = useMemo<SelectOption[]>(
    () => [
      { value: "", label: t("globalFilters.allOrganizations") },
      ...organizations.map((org) => ({ value: org.id, label: org.name })),
    ],
    [organizations, t],
  )
  const platformOptions = useMemo<SelectOption[]>(
    () => [{ value: "", label: t("globalFilters.allPlatforms") }, ...platforms.map((p) => ({ value: p, label: platformLabel(p) }))],
    [platforms, t],
  )
  const integrationOptions = useMemo<SelectOption[]>(
    () => [
      { value: "", label: t("globalFilters.allIntegrations") },
      ...filteredIntegrations.map((i) => ({ value: i.id, label: `${i.name} (${platformLabel(i.platform)})` })),
    ],
    [filteredIntegrations, t],
  )

  const hasActiveFilters = selectedOrgId != null || selectedPlatform != null || selectedIntegrationId != null

  if (HIDDEN_PREFIXES.some((prefix) => location.pathname.startsWith(prefix))) return null
  if (loading) return null

  // Falha real de carregamento: avisa e oferece retry, em vez de selects vazios mudos.
  if (error) {
    return (
      <div className="flex shrink-0 items-center gap-3 border-b border-danger-200 bg-danger-50 px-4 py-2 text-sm text-danger-700">
        <AlertTriangleIcon size={16} aria-hidden="true" className="shrink-0" />
        <span className="min-w-0 flex-1 truncate">{t("globalFilters.loadFailed")}</span>
        <Button variant="ghost" size="xs" onClick={() => void refreshData()} leftIcon={<RefreshCwIcon size={14} />}>
          {t("globalFilters.retry")}
        </Button>
      </div>
    )
  }

  return (
    <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-border bg-surface px-4 py-2">
      <span className="flex items-center gap-1.5 text-text-tertiary" aria-hidden="true">
        <FilterIcon size={14} className="shrink-0" />
        <span className="hidden text-xs font-medium uppercase tracking-wide sm:inline">{t("globalFilters.label")}</span>
      </span>

      <div className="grid min-w-0 flex-1 grid-cols-1 gap-2 sm:flex sm:flex-wrap sm:items-center">
        <Select
          size="sm"
          className="min-w-0 sm:w-52"
          leftIcon={<BuildingIcon size={14} />}
          aria-label={t("globalFilters.filterByOrganization")}
          placeholder={t("globalFilters.allOrganizations")}
          options={orgOptions}
          value={selectedOrgId ?? ""}
          onChange={(value) => {
            setSelectedOrgId(asNumber(value))
            setSelectedIntegrationId(null)
          }}
        />

        {/* Só mostra o filtro de plataforma se o backend tiver mais de uma. */}
        {platforms.length > 1 && (
          <Select
            size="sm"
            className="min-w-0 sm:w-44"
            aria-label={t("globalFilters.filterByPlatform")}
            placeholder={t("globalFilters.allPlatforms")}
            options={platformOptions}
            value={selectedPlatform ?? ""}
            onChange={(value) => {
              setSelectedPlatform((Array.isArray(value) || value === "" ? null : (value as PlatformType)) || null)
              setSelectedIntegrationId(null)
            }}
          />
        )}

        <Select
          size="sm"
          className="min-w-0 sm:w-64"
          leftIcon={<PlugIcon size={14} />}
          aria-label={t("globalFilters.filterByIntegration")}
          placeholder={t("globalFilters.allIntegrations")}
          options={integrationOptions}
          value={selectedIntegrationId ?? ""}
          onChange={(value) => setSelectedIntegrationId(asNumber(value))}
        />
      </div>

      {hasActiveFilters && (
        <Button
          variant="ghost"
          size="xs"
          className="ml-auto shrink-0"
          onClick={clearFilters}
          leftIcon={<XIcon size={12} />}
        >
          {t("globalFilters.clearFilters")}
        </Button>
      )}
    </div>
  )
}
