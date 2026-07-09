/**
 * IntegrationDestinationsTab — aba "Destinos" da IntegrationDetailPage.
 *
 * Mostra, de forma HONESTA e clara, quais destinos podem receber eventos
 * desta integração com base nas rotas da org. Usa dryRunRoutes com um
 * evento sintético para simular o roteamento — não é log histórico.
 *
 * Estratégia:
 *   1. Carrega todas as rotas da org (listRoutes).
 *   2. Filtra rotas cujas condições referenciam esta integração
 *      (por integration_id ou vendor/platform).
 *   3. Executa dryRunRoutes com evento sintético para confirmar destinos
 *      efetivos e obter per_destination counts.
 *   4. Carrega listDestinations para cruzar IDs com nomes.
 */

import type React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { RouteIcon, ServerIcon } from "lucide-react"
import { Trans, useTranslation } from "react-i18next"
import * as api from "@/services/api"
import type { Destination, Integration, Route, RouteDryRunResponse } from "@/types"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { Notice } from "@/components/ui/Notice/Notice"
import { SkeletonCard } from "@/components/ui/Skeleton"
import { cn } from "@/lib/utils"

// ── helpers ────────────────────────────────────────────────────────────────────

/**
 * Verifica se a condição de uma rota referencia esta integração.
 * Procura chaves "integration_id" ou "vendor"/"platform" recursivamente.
 */
function routeReferencesIntegration(
  condition: Record<string, unknown>,
  integration: Integration,
): boolean {
  const condStr = JSON.stringify(condition).toLowerCase()
  // integração por id numérico
  if (condStr.includes(`"integration_id":${integration.id}`)) return true
  if (condStr.includes(`"integration_id": ${integration.id}`)) return true
  // integração por vendor/platform
  const platform = integration.platform.toLowerCase()
  if (condStr.includes(`"vendor":"${platform}"`) || condStr.includes(`"vendor": "${platform}"`)) return true
  if (condStr.includes(`"platform":"${platform}"`) || condStr.includes(`"platform": "${platform}"`)) return true
  return false
}

/**
 * Constrói evento sintético para o dryRun com base nos metadados da integração.
 * O shape deve ser suficiente para que as condições de rota casem.
 */
function buildSyntheticEvent(integration: Integration): Record<string, unknown> {
  return {
    integration_id: integration.id,
    vendor: integration.platform,
    platform: integration.platform,
    organization_id: integration.organization_id,
    // metadados adicionais que condições comuns podem verificar
    _synthetic: true,
    name: integration.name,
  }
}

// ── sub-componentes ────────────────────────────────────────────────────────────

interface RouteRowProps {
  route: Route
  destinationMap: Map<string, Destination>
  matchedDestIds: Set<string>
}

const RouteRow: React.FC<RouteRowProps> = ({ route, destinationMap, matchedDestIds }) => {
  const { t } = useTranslation("integrations")
  const destIds = route.destination_ids ?? []
  return (
    <div className="rounded-xl border border-border bg-surface p-4 space-y-2 shadow-sm">
      <div className="flex flex-wrap items-center gap-2">
        <RouteIcon size={14} className="shrink-0 text-primary-600" aria-hidden="true" />
        <span className="font-medium text-text">{route.name}</span>
        <Badge variant={route.enabled ? "success" : "outline"} size="sm">
          {route.enabled ? t("destinationsTab.enabled") : t("destinationsTab.disabled")}
        </Badge>
        {route.is_final && (
          <Badge variant="primary" size="sm">{t("destinationsTab.final")}</Badge>
        )}
        <span className="text-xs text-text-tertiary">{t("destinationsTab.priority", { value: route.priority })}</span>
      </div>

      {destIds.length === 0 ? (
        <p className="text-xs text-text-secondary">{t("destinationsTab.noDestinationsConfigured")}</p>
      ) : (
        <ul className="flex flex-wrap gap-2" aria-label={t("destinationsTab.destinationsForRouteAriaLabel", { name: route.name })}>
          {destIds.map((destId) => {
            const dest = destinationMap.get(destId)
            const matched = matchedDestIds.has(destId)
            return (
              <li key={destId} className="flex items-center gap-1.5">
                <ServerIcon size={12} className="shrink-0 text-text-secondary" aria-hidden="true" />
                <span className={cn(
                  "text-xs font-medium",
                  matched ? "text-text" : "text-text-secondary",
                )}>
                  {dest?.name ?? destId}
                </span>
                {dest?.kind && (
                  <Badge variant="default" size="sm">{dest.kind}</Badge>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

// ── componente principal ───────────────────────────────────────────────────────

export interface IntegrationDestinationsTabProps {
  integration: Integration
}

interface TabState {
  status: "idle" | "loading" | "ready" | "error"
  error: string | null
  matchingRoutes: Route[]
  destinationMap: Map<string, Destination>
  dryRunResult: RouteDryRunResponse | null
}

export const IntegrationDestinationsTab: React.FC<IntegrationDestinationsTabProps> = ({
  integration,
}) => {
  const { t } = useTranslation("integrations")
  const [state, setState] = useState<TabState>({
    status: "idle",
    error: null,
    matchingRoutes: [],
    destinationMap: new Map(),
    dryRunResult: null,
  })

  const load = useCallback(async () => {
    setState((prev) => ({ ...prev, status: "loading", error: null }))
    try {
      const [routes, destinations] = await Promise.all([
        api.listRoutes(),
        api.listDestinations({ include_disabled: true }),
      ])

      const destMap = new Map<string, Destination>()
      for (const d of destinations) {
        destMap.set(d.id, d)
      }

      // filtra rotas que referenciam esta integração
      const matching = routes.filter(
        (r) => r.enabled && routeReferencesIntegration(r.condition, integration),
      )

      // dry-run para confirmar roteamento efetivo
      let dryRun: RouteDryRunResponse | null = null
      if (matching.length > 0) {
        try {
          dryRun = await api.dryRunRoutes({
            samples: [buildSyntheticEvent(integration)],
          })
        } catch {
          // dry-run é best-effort; falha não bloqueia a aba
          dryRun = null
        }
      }

      setState({
        status: "ready",
        error: null,
        matchingRoutes: matching,
        destinationMap: destMap,
        dryRunResult: dryRun,
      })
    } catch (err) {
      setState((prev) => ({
        ...prev,
        status: "error",
        error: err instanceof Error ? err.message : t("destinationsTab.loadErrorTitle"),
      }))
    }
  }, [integration, t])

  useEffect(() => {
    void load()
  }, [load])

  // IDs de destinos que o dry-run confirmou que receberiam eventos
  const confirmedDestIds = useMemo((): Set<string> => {
    if (!state.dryRunResult) return new Set()
    return new Set(Object.keys(state.dryRunResult.per_destination ?? {}))
  }, [state.dryRunResult])

  const { status, error, matchingRoutes, destinationMap, dryRunResult } = state

  return (
    <div className="space-y-4" data-testid="integration-destinations-tab">
      <Notice variant="info">
        <Trans
          i18nKey="destinationsTab.previewDescription"
          t={t}
          components={{ strong: <strong /> }}
        />
      </Notice>

      {status === "loading" && (
        <div role="status" aria-label={t("destinationsTab.loadingAriaLabel")} className="grid gap-4 sm:grid-cols-2">
          <SkeletonCard />
          <SkeletonCard />
        </div>
      )}

      {status === "error" && (
        <Notice
          variant="danger"
          title={t("destinationsTab.loadErrorTitle")}
          action={
            <Button variant="outline" size="sm" onClick={() => void load()}>
              {t("common:actions.retry")}
            </Button>
          }
        >
          {error}
        </Notice>
      )}

      {status === "ready" && (
        <>
          {dryRunResult && (
            <Card padding="sm" className="flex flex-wrap items-center gap-3 shadow-sm">
              <span className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">
                {t("destinationsTab.simulationLabel")}
              </span>
              <Badge variant={dryRunResult.dropped ? "danger" : "success"} size="sm">
                {dryRunResult.dropped ? t("destinationsTab.dropped") : t("destinationsTab.routed")}
              </Badge>
              {!dryRunResult.dropped && confirmedDestIds.size > 0 && (
                <span className="text-xs text-text-secondary">
                  {t("destinationsTab.confirmedDestinations", { count: confirmedDestIds.size })}
                </span>
              )}
              {dryRunResult.fallback && (
                <Badge variant="warning" size="sm">{t("destinationsTab.fallback")}</Badge>
              )}
            </Card>
          )}

          {matchingRoutes.length === 0 ? (
            <div className="py-12 text-center text-text-secondary" data-testid="destinations-empty">
              <ServerIcon size={32} className="mx-auto mb-3 text-text-tertiary" aria-hidden="true" />
              <p className="font-medium text-text">{t("destinationsTab.emptyTitle")}</p>
              <p className="mt-1 text-sm">
                {t("destinationsTab.emptyDescription")}
              </p>
            </div>
          ) : (
            <ul className="space-y-3 list-none p-0" role="list" aria-label={t("destinationsTab.matchingRoutesAriaLabel")}>
              {matchingRoutes.map((route) => (
                <li key={route.id}>
                  <RouteRow
                    route={route}
                    destinationMap={destinationMap}
                    matchedDestIds={confirmedDestIds}
                  />
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  )
}

export default IntegrationDestinationsTab
