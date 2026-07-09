/**
 * FlowLiveFeed — painel colapsável de feed ao vivo de eventos dos destinos.
 *
 * - Poll a cada 8s para os top-N destinos por EPS
 * - Mescla, ordena por timestamp desc, exibe ~20 itens
 * - Degrada gracioso se um tap falhar (exibe os demais)
 * - Novos itens entram no topo com destaque sutil
 */
import type React from "react"
import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { ActivityIcon, ChevronDownIcon, ChevronUpIcon, RefreshCwIcon } from "lucide-react"
import * as api from "@/services/api"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { formatRelativeDate } from "@/lib/utils"
import type { TopologyDestination } from "@/types"

const POLL_MS = 8000
const MAX_DEST = 5
const MAX_ITEMS = 20

interface FeedItem {
  key: string
  destId: string
  destName: string
  timestamp: string | null
  summary: string
  redacted: boolean
  isNew: boolean
}

interface FlowLiveFeedProps {
  destinations: TopologyDestination[]
  /** External open/collapsed state */
  open: boolean
  onToggle: () => void
}

export const FlowLiveFeed: React.FC<FlowLiveFeedProps> = ({
  destinations,
  open,
  onToggle,
}) => {
  const { t } = useTranslation("dashboard")
  const [items, setItems] = useState<FeedItem[]>([])
  const [loading, setLoading] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const prevKeysRef = useRef<Set<string>>(new Set())

  // Pick top-N destinations by eps
  const topDests = [...destinations]
    .sort((a, b) => (b.eps ?? 0) - (a.eps ?? 0))
    .slice(0, MAX_DEST)

  const fetchFeed = useCallback(
    async (silent = false) => {
      if (!open || topDests.length === 0) return
      abortRef.current?.abort()
      const ctrl = new AbortController()
      abortRef.current = ctrl
      if (!silent) setLoading(true)

      const results = await Promise.allSettled(
        topDests.map((d) => api.getDestinationTap(d.id, { limit: 10 })),
      )

      if (ctrl.signal.aborted) return

      const merged: FeedItem[] = []
      results.forEach((res, idx) => {
        if (res.status !== "fulfilled") return
        const dest = topDests[idx]
        const entries = (res.value?.entries) ?? []
        entries.forEach((entry, ei) => {
          const ts = entry.timestamp as string | undefined ?? null
          const summary =
            String(
              (entry.event_type as string | undefined) ??
              (entry.type as string | undefined) ??
              (entry.action as string | undefined) ??
              t("flow.liveFeed.genericEvent"),
            )
          const key = `${dest.id}-${ts ?? ei}`
          merged.push({
            key,
            destId: dest.id,
            destName: dest.name,
            timestamp: ts,
            summary,
            redacted: (entry._redacted as boolean | undefined) ?? false,
            isNew: !prevKeysRef.current.has(key),
          })
        })
      })

      // Sort by timestamp desc (items without ts go last)
      merged.sort((a, b) => {
        if (!a.timestamp && !b.timestamp) return 0
        if (!a.timestamp) return 1
        if (!b.timestamp) return -1
        return new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()
      })

      const capped = merged.slice(0, MAX_ITEMS)
      prevKeysRef.current = new Set(capped.map((i) => i.key))
      setItems(capped)
      setLastUpdated(new Date())
      setLoading(false)

      // Clear "isNew" flag after 2s
      window.setTimeout(() => {
        setItems((prev) => prev.map((i) => ({ ...i, isNew: false })))
      }, 2000)
    },
    // topDests changes when destinations changes – need stable comparison
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [open, destinations, t],
  )

  // Poll while open
  useEffect(() => {
    if (!open) return
    void fetchFeed(false)
    const id = window.setInterval(() => void fetchFeed(true), POLL_MS)
    return () => {
      window.clearInterval(id)
      abortRef.current?.abort()
    }
  }, [open, fetchFeed])

  return (
    <div className="rounded-lg border border-border bg-surface shadow-sm">
      {/* Header / toggle */}
      <button
        type="button"
        className="flex w-full items-center justify-between px-4 py-3 text-left"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls="flow-live-feed-body"
      >
        <span className="flex items-center gap-2 text-sm font-semibold text-text">
          <ActivityIcon size={14} className="text-primary-500" aria-hidden="true" />
          {t("flow.liveFeed.title")}
          {loading && (
            <RefreshCwIcon size={12} className="animate-spin text-text-tertiary" aria-hidden="true" />
          )}
        </span>
        <span className="flex items-center gap-2 text-text-tertiary">
          {lastUpdated && !open && (
            <span className="text-xs text-text-tertiary">
              {formatRelativeDate(lastUpdated.toISOString())}
            </span>
          )}
          {open ? <ChevronUpIcon size={15} /> : <ChevronDownIcon size={15} />}
        </span>
      </button>

      {open && (
        <div
          id="flow-live-feed-body"
          className="border-t border-border"
          data-testid="flow-live-feed-body"
        >
          {items.length === 0 && !loading && (
            <p className="px-4 py-6 text-center text-sm text-text-tertiary">
              {t("flow.liveFeed.noRecentEvents")}{" "}
              {topDests.length === 0 && t("flow.liveFeed.noActiveDestinations")}
            </p>
          )}

          {items.length > 0 && (
            <ul className="divide-y divide-border">
              {items.map((item) => (
                <li
                  key={item.key}
                  className={`flex items-start gap-3 px-4 py-2.5 transition-colors ${
                    item.isNew ? "bg-primary-50/60" : ""
                  }`}
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="truncate text-xs font-medium text-text">
                        {item.summary}
                      </span>
                      {item.redacted && (
                        <Badge variant="warning" size="sm">{t("flow.liveFeed.redacted")}</Badge>
                      )}
                    </div>
                    <div className="mt-0.5 flex items-center gap-2 text-[10px] text-text-tertiary">
                      <span className="truncate">{item.destName}</span>
                      {item.timestamp && (
                        <>
                          <span aria-hidden="true">·</span>
                          <span>{formatRelativeDate(item.timestamp)}</span>
                        </>
                      )}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}

          {lastUpdated && (
            <div className="flex items-center justify-between border-t border-border px-4 py-2">
              <span className="text-[10px] text-text-tertiary">
                {t("flow.liveFeed.updated", { relative: formatRelativeDate(lastUpdated.toISOString()), seconds: POLL_MS / 1000 })}
              </span>
              <Button
                variant="ghost"
                size="xs"
                onClick={() => void fetchFeed(false)}
                disabled={loading}
              >
                {t("flow.liveFeed.refreshNow")}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
