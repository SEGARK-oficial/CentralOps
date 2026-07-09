"use client"

import type React from "react"
import { useEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import {
  CheckCircleIcon,
  ClockIcon,
  ShieldAlertIcon,
  XIcon,
} from "lucide-react"
import type { DetectionRead, DetectionStatus } from "@/types"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { Notice } from "@/components/ui/Notice/Notice"
import { usePermission } from "@/hooks/usePermission"
import { formatDate } from "@/lib/utils"

interface DetectionDetailsDrawerProps {
  open: boolean
  detection: DetectionRead | null
  triageLoading?: boolean
  triageError?: string | null
  onClose: () => void
  onTriage: (id: number, status: DetectionStatus) => Promise<void>
}

// ── Helpers ──────────────────────────────────────────────────────────────────

type BadgeVariant = "default" | "primary" | "success" | "warning" | "danger" | "outline"

function severityBadgeVariant(severityId: number): BadgeVariant {
  if (severityId <= 2) return "default"
  if (severityId === 3) return "primary"
  if (severityId === 4) return "warning"
  return "danger"
}

function severityLabel(severityId: number): string {
  switch (severityId) {
    case 1: return "Informacional"
    case 2: return "Baixa"
    case 3: return "Média"
    case 4: return "Alta"
    case 5: return "Crítica"
    case 6: return "Fatal"
    default: return `Sev ${severityId}`
  }
}

function statusBadgeVariant(status: DetectionStatus): BadgeVariant {
  switch (status) {
    case "open": return "danger"
    case "ack": return "warning"
    case "closed": return "success"
    default: return "default"
  }
}

function statusLabel(status: DetectionStatus): string {
  switch (status) {
    case "open": return "Aberta"
    case "ack": return "Reconhecida"
    case "closed": return "Fechada"
    default: return status
  }
}

function sourceLabel(source: string): string {
  switch (source) {
    case "scheduled_query": return "Query agendada"
    case "live_query": return "Query live"
    case "correlation": return "Correlação"
    default: return source
  }
}

// ── Classes ───────────────────────────────────────────────────────────────────

const sectionTitleCls = "text-sm font-semibold uppercase tracking-wider text-text-secondary"
const valueCls = "break-words text-sm text-text"
const labelCls = "text-xs font-medium text-text-secondary"

// ── Component ─────────────────────────────────────────────────────────────────

export const DetectionDetailsDrawer: React.FC<DetectionDetailsDrawerProps> = ({
  open,
  detection,
  triageLoading = false,
  triageError,
  onClose,
  onTriage,
}) => {
  const drawerRef = useRef<HTMLDivElement>(null)
  const previousActiveElement = useRef<HTMLElement | null>(null)
  const [actionLoading, setActionLoading] = useState<DetectionStatus | null>(null)

  const canTriage = usePermission("query.run")

  useEffect(() => {
    if (!open) return

    previousActiveElement.current = document.activeElement as HTMLElement
    const focusTimer = window.setTimeout(() => drawerRef.current?.focus(), 0)
    document.body.style.overflow = "hidden"

    const FOCUSABLE_SELECTOR =
      'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), summary, details, [tabindex]:not([tabindex="-1"])'

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault()
        onClose()
        return
      }

      if (event.key !== "Tab") return

      const panel = drawerRef.current
      if (!panel) return

      const focusable = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      )
      if (focusable.length === 0) {
        event.preventDefault()
        panel.focus()
        return
      }

      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = document.activeElement

      if (event.shiftKey) {
        if (active === first || active === panel || !panel.contains(active)) {
          event.preventDefault()
          last.focus()
        }
      } else if (active === last || !panel.contains(active)) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener("keydown", handleKeyDown)
    return () => {
      window.clearTimeout(focusTimer)
      document.removeEventListener("keydown", handleKeyDown)
      document.body.style.overflow = ""
      previousActiveElement.current?.focus()
    }
  }, [onClose, open])

  const handleTriage = async (status: DetectionStatus) => {
    if (!detection) return
    setActionLoading(status)
    try {
      await onTriage(detection.id, status)
    } finally {
      setActionLoading(null)
    }
  }

  if (!open) return null

  return createPortal(
    <div className="fixed inset-0 z-modal-backdrop bg-black/45" onClick={onClose}>
      <div
        ref={drawerRef}
        className="ml-auto h-full w-full sm:max-w-2xl lg:max-w-3xl"
        onClick={(event) => event.stopPropagation()}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label="Detalhes da detecção"
      >
        <div className="flex h-full w-full flex-col overflow-hidden border-l border-border bg-surface shadow-2xl">
          {/* Header */}
          <div className="shrink-0 border-b border-border px-6 py-5">
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0 space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  {detection && (
                    <>
                      <Badge variant={severityBadgeVariant(detection.severity_id)} size="sm">
                        {severityLabel(detection.severity_id)}
                      </Badge>
                      <Badge variant={statusBadgeVariant(detection.status)} size="sm">
                        {statusLabel(detection.status)}
                      </Badge>
                      <Badge variant="outline" size="sm">
                        {sourceLabel(detection.source)}
                      </Badge>
                    </>
                  )}
                </div>
                <div className="min-w-0">
                  <h2 className="break-words text-xl font-semibold text-text">
                    {detection?.rule_name || "Detecção"}
                  </h2>
                  {detection?.rule_id && (
                    <p className="mt-1 font-mono text-sm text-text-secondary">
                      {detection.rule_id}
                    </p>
                  )}
                </div>
              </div>
              <Button variant="ghost" size="xs" onClick={onClose} aria-label="Fechar detalhes">
                <XIcon size={18} />
              </Button>
            </div>
          </div>

          {/* Body */}
          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-5">
            {triageError && (
              <Notice variant="danger" title="Falha na triagem">
                {triageError}
              </Notice>
            )}

            {detection && (
              <>
                {/* Triage actions */}
                {canTriage && (
                  <Card padding="md" className="shadow-sm">
                    <div className="flex items-center gap-2">
                      <ShieldAlertIcon size={16} className="text-text-tertiary" />
                      <h3 className={sectionTitleCls}>Triagem</h3>
                    </div>
                    <div className="mt-3 flex flex-wrap gap-2">
                      {detection.status !== "ack" && (
                        <Button
                          size="sm"
                          variant="outline"
                          leftIcon={<ClockIcon size={14} />}
                          loading={actionLoading === "ack"}
                          disabled={triageLoading || actionLoading !== null}
                          onClick={() => handleTriage("ack")}
                        >
                          Reconhecer (Ack)
                        </Button>
                      )}
                      {detection.status !== "closed" && (
                        <Button
                          size="sm"
                          variant="outline"
                          leftIcon={<CheckCircleIcon size={14} />}
                          loading={actionLoading === "closed"}
                          disabled={triageLoading || actionLoading !== null}
                          onClick={() => handleTriage("closed")}
                        >
                          Fechar
                        </Button>
                      )}
                      {detection.status !== "open" && (
                        <Button
                          size="sm"
                          variant="outline"
                          leftIcon={<ShieldAlertIcon size={14} />}
                          loading={actionLoading === "open"}
                          disabled={triageLoading || actionLoading !== null}
                          onClick={() => handleTriage("open")}
                        >
                          Reabrir
                        </Button>
                      )}
                    </div>
                  </Card>
                )}

                {/* Identification */}
                <Card padding="md" className="space-y-4 shadow-sm">
                  <div className="flex items-center gap-2">
                    <ShieldAlertIcon size={16} className="text-text-tertiary" />
                    <h3 className={sectionTitleCls}>Identificação</h3>
                  </div>
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div>
                      <div className={labelCls}>ID</div>
                      <div className={`${valueCls} font-mono text-xs`}>{detection.id}</div>
                    </div>
                    <div>
                      <div className={labelCls}>Organização</div>
                      <div className={`${valueCls} font-mono text-xs`}>{detection.organization_id}</div>
                    </div>
                    <div className="sm:col-span-2">
                      <div className={labelCls}>Dedup key</div>
                      <div className={`${valueCls} break-all font-mono text-xs`}>{detection.dedup_key}</div>
                    </div>
                    <div>
                      <div className={labelCls}>Contagem</div>
                      <div className={valueCls}>{detection.count ?? 1}</div>
                    </div>
                    {detection.suppression_window_seconds != null && (
                      <div>
                        <div className={labelCls}>Janela de supressão</div>
                        <div className={valueCls}>{detection.suppression_window_seconds}s</div>
                      </div>
                    )}
                  </div>
                </Card>

                {/* Temporal */}
                <Card padding="md" className="space-y-4 shadow-sm">
                  <div className="flex items-center gap-2">
                    <ClockIcon size={16} className="text-text-tertiary" />
                    <h3 className={sectionTitleCls}>Temporal</h3>
                  </div>
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div>
                      <div className={labelCls}>Primeira vez vista</div>
                      <div className={valueCls}>{detection.first_seen ? formatDate(detection.first_seen) : "-"}</div>
                    </div>
                    <div>
                      <div className={labelCls}>Última vez vista</div>
                      <div className={valueCls}>{detection.last_seen ? formatDate(detection.last_seen) : "-"}</div>
                    </div>
                    {detection.created_at && (
                      <div>
                        <div className={labelCls}>Criado em</div>
                        <div className={valueCls}>{formatDate(detection.created_at)}</div>
                      </div>
                    )}
                  </div>
                </Card>

                {/* Context */}
                <Card padding="md" className="space-y-4 shadow-sm">
                  <div className="flex items-center gap-2">
                    <ShieldAlertIcon size={16} className="text-text-tertiary" />
                    <h3 className={sectionTitleCls}>Contexto</h3>
                  </div>
                  <div className="grid gap-4 sm:grid-cols-2">
                    {detection.dialect && (
                      <div>
                        <div className={labelCls}>Dialeto</div>
                        <div className={`${valueCls} font-mono text-xs`}>{detection.dialect}</div>
                      </div>
                    )}
                    {detection.integration_id != null && (
                      <div>
                        <div className={labelCls}>Integration ID</div>
                        <div className={`${valueCls} font-mono text-xs`}>{detection.integration_id}</div>
                      </div>
                    )}
                    {detection.source !== "correlation" && detection.source_query_id != null && (
                      <div>
                        <div className={labelCls}>Query ID</div>
                        <div className={`${valueCls} font-mono text-xs`}>{detection.source_query_id}</div>
                      </div>
                    )}
                    {detection.search_result_id != null && (
                      <div>
                        <div className={labelCls}>Search result ID</div>
                        <div className={`${valueCls} font-mono text-xs`}>{detection.search_result_id}</div>
                      </div>
                    )}
                    {detection.ocsf_ref && (
                      <div className="sm:col-span-2">
                        <div className={labelCls}>OCSF ref</div>
                        <div className={`${valueCls} break-all font-mono text-xs`}>{detection.ocsf_ref}</div>
                      </div>
                    )}
                  </div>
                </Card>
              </>
            )}
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}

export default DetectionDetailsDrawer
