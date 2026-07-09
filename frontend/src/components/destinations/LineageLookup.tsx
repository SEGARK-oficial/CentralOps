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
      setError(err instanceof Error ? err.message : "Falha ao buscar lineage.")
    } finally {
      setLoading(false)
    }
  }

  const helperId = "lineage-hint"

  return (
    <div className="space-y-4" data-testid="lineage-lookup">
      <div className="flex items-center gap-2">
        <RouteIcon size={16} className="text-text-tertiary" aria-hidden="true" />
        <h4 className="text-sm font-semibold text-text">Rastreio de evento (lineage)</h4>
      </div>

      <form onSubmit={handleSearch} className="flex gap-2" role="search" aria-label="Buscar lineage do evento">
        <div className="flex flex-1 flex-col gap-1">
          <label htmlFor="lineage-event-id" className="sr-only">
            ID do evento
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
              placeholder="Cole o event_id aqui"
              aria-describedby={helperId}
              className="w-full h-9 pl-9 pr-3 text-sm rounded-md border border-border bg-surface text-text placeholder:text-text-tertiary transition-colors focus-ring"
              data-testid="lineage-event-id-input"
            />
          </div>
          <p id={helperId} className="text-xs text-text-tertiary">
            Localiza entregas registradas para este event_id neste destino.
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
          Buscar
        </Button>
      </form>

      {/* Carregando */}
      {loading && (
        <div role="status" aria-label="Buscando lineage…" className="space-y-2">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      )}

      {/* Erro */}
      {error && !loading && (
        <Notice variant="danger" title="Falha na busca">
          {error}
        </Notice>
      )}

      {/* Resultado */}
      {result && !loading && (
        <div className="space-y-3" data-testid="lineage-result">
          {result.entries.length === 0 ? (
            <p className="text-sm text-text-tertiary">
              Nenhuma entrega registrada para <code className="font-mono">{result.event_id}</code> neste destino.
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
