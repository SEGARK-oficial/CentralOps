/**
 * LineageLookup — busca o trajeto de um evento específico neste destino.
 *
 * Chama getDestinationLineage(destinationId, eventId) e exibe:
 *  - Lista de entregas (destination_id / kind / status / timestamp)
 *  - retention_note honesta sobre o TTL do Redis
 *
 * Campo de busca acessível com label + aria-describedby.
 */

import type React from "react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { SearchIcon, RouteIcon } from "lucide-react"
import * as api from "@/services/api"
import { Button } from "@/components/ui/Button/Button"
import { Badge } from "@/components/ui/Badge/Badge"
import { Notice } from "@/components/ui/Notice/Notice"
import { Skeleton } from "@/components/ui/Skeleton"
import type { DestinationLineageResponse } from "@/types"

// ── Props ─────────────────────────────────────────────────────────────────────

export interface LineageLookupProps {
  destinationId: string
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtEpoch(ts: number): string {
  return new Date(ts * 1000).toLocaleString("pt-BR")
}

// ── Componente ────────────────────────────────────────────────────────────────

export const LineageLookup: React.FC<LineageLookupProps> = ({ destinationId }) => {
  const { t } = useTranslation("destinations")
  const [eventId, setEventId] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<DestinationLineageResponse | null>(null)

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault()
    const trimmed = eventId.trim()
    if (!trimmed) return

    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const data = await api.getDestinationLineage(destinationId, trimmed)
      setResult(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : t("lineageLookup.errorFallback"))
    } finally {
      setLoading(false)
    }
  }

  const helperId = "lineage-hint"

  return (
    <div className="space-y-4" data-testid="lineage-lookup">
      <div className="flex items-center gap-2">
        <RouteIcon size={16} className="text-text-tertiary" aria-hidden="true" />
        <h4 className="text-sm font-semibold text-text">{t("lineageLookup.title")}</h4>
      </div>

      <form onSubmit={handleSearch} className="flex gap-2" role="search" aria-label={t("lineageLookup.formAriaLabel")}>
        <div className="flex flex-1 flex-col gap-1">
          <label htmlFor="lineage-event-id" className="sr-only">
            {t("lineageLookup.eventIdLabelSr")}
          </label>
          <div className="relative flex-1">
            <div className="absolute left-3 top-1/2 -translate-y-1/2 text-text-tertiary pointer-events-none" aria-hidden="true">
              <SearchIcon size={14} />
            </div>
            <input
              id="lineage-event-id"
              type="text"
              value={eventId}
              onChange={(e) => setEventId(e.target.value)}
              placeholder={t("lineageLookup.eventIdPlaceholder")}
              aria-describedby={helperId}
              className="w-full h-9 pl-9 pr-3 text-sm rounded-md border border-border-field bg-surface-tertiary text-text placeholder:text-text-tertiary transition-colors hover:border-border-field-hover focus-ring"
              data-testid="lineage-event-id-input"
            />
          </div>
          <p id={helperId} className="text-xs text-text-tertiary">
            {t("lineageLookup.eventIdHelp")}
          </p>
        </div>
        <Button
          type="submit"
          variant="primary"
          size="sm"
          loading={loading}
          disabled={!eventId.trim()}
          data-testid="lineage-search-btn"
          className="self-start mt-0"
        >
          {t("lineageLookup.searchButton")}
        </Button>
      </form>

      {/* Carregando */}
      {loading && (
        <div role="status" aria-label={t("lineageLookup.loadingAriaLabel")} className="space-y-2">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      )}

      {/* Erro */}
      {error && !loading && (
        // R2-8.2: reação direta ao clique em "Buscar" — mantém assertive
        // explícito (o padrão do Notice virou polite).
        <Notice variant="danger" title={t("lineageLookup.errorTitle")} live="assertive">
          {error}
        </Notice>
      )}

      {/* Resultado */}
      {result && !loading && (
        <div className="space-y-3" data-testid="lineage-result">
          {result.entries.length === 0 ? (
            <p className="text-sm text-text-tertiary">
              {t("lineageLookup.noResultsPrefix")} <code className="font-mono">{result.event_id}</code>{" "}
              {t("lineageLookup.noResultsSuffix")}
            </p>
          ) : (
            <>
              <div className="divide-y divide-border rounded-md border border-border">
                {result.entries.map((entry, i) => (
                  <div key={i} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant="outline" size="sm">{entry.kind}</Badge>
                      <Badge variant={entry.status === "delivered" ? "success" : "warning"} size="sm">
                        {entry.status}
                      </Badge>
                      <span className="font-mono text-xs text-text-secondary">{entry.destination_id}</span>
                    </div>
                    <span className="text-xs text-text-tertiary">{fmtEpoch(entry.ts)}</span>
                  </div>
                ))}
              </div>
              <p className="text-xs text-text-tertiary" data-testid="lineage-retention-note">
                {result.retention_note}
              </p>
            </>
          )}
        </div>
      )}
    </div>
  )
}

export default LineageLookup
