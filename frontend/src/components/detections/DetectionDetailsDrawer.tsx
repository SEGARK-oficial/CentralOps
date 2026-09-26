"use client"

import type React from "react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
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
import { Drawer } from "@/components/ui/Drawer/Drawer"
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

function severityLabel(t: (key: string, opts?: Record<string, unknown>) => string, severityId: number): string {
  switch (severityId) {
    case 1: return t("detailsDrawer.severity.informational")
    case 2: return t("detailsDrawer.severity.low")
    case 3: return t("detailsDrawer.severity.medium")
    case 4: return t("detailsDrawer.severity.high")
    case 5: return t("detailsDrawer.severity.critical")
    case 6: return t("detailsDrawer.severity.fatal")
    default: return t("detailsDrawer.severity.unknown", { id: severityId })
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

function statusLabel(t: (key: string) => string, status: DetectionStatus): string {
  switch (status) {
    case "open": return t("detailsDrawer.status.open")
    case "ack": return t("detailsDrawer.status.ack")
    case "closed": return t("detailsDrawer.status.closed")
    default: return status
  }
}

function sourceLabel(t: (key: string) => string, source: string): string {
  switch (source) {
    case "scheduled_query": return t("detailsDrawer.source.scheduledQuery")
    case "live_query": return t("detailsDrawer.source.liveQuery")
    case "correlation": return t("detailsDrawer.source.correlation")
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
  const [actionLoading, setActionLoading] = useState<DetectionStatus | null>(null)
  const { t } = useTranslation("detections")

  const canTriage = usePermission("query.run")

  const handleTriage = async (status: DetectionStatus) => {
    if (!detection) return
    setActionLoading(status)
    try {
      await onTriage(detection.id, status)
    } finally {
      setActionLoading(null)
    }
  }

  return (
    <Drawer open={open} onClose={onClose} size="xl" ariaLabel={t("detailsDrawer.ariaLabel")} data-testid="detection-details-drawer">
      <div className="flex h-full w-full flex-col overflow-hidden">
        {/* Header */}
        <div className="shrink-0 border-b border-border px-6 py-5">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0 space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                {detection && (
                  <>
                    <Badge variant={severityBadgeVariant(detection.severity_id)} size="sm">
                      {severityLabel(t, detection.severity_id)}
                    </Badge>
                    <Badge variant={statusBadgeVariant(detection.status)} size="sm">
                      {statusLabel(t, detection.status)}
                    </Badge>
                    <Badge variant="outline" size="sm">
                      {sourceLabel(t, detection.source)}
                    </Badge>
                  </>
                )}
              </div>
              <div className="min-w-0">
                <h2 className="break-words text-xl font-semibold text-text">
                  {detection?.rule_name || t("detailsDrawer.fallbackTitle")}
                </h2>
                {detection?.rule_id && (
                  <p className="mt-1 font-mono text-sm text-text-secondary">
                    {detection.rule_id}
                  </p>
                )}
              </div>
            </div>
            <Button variant="ghost" size="xs" onClick={onClose} aria-label={t("detailsDrawer.closeAriaLabel")}>
              <XIcon size={18} />
            </Button>
          </div>
        </div>

        {/* Body */}
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-5">
          {triageError && (
            <Notice variant="danger" title={t("detailsDrawer.triageErrorTitle")}>
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
                    <h3 className={sectionTitleCls}>{t("detailsDrawer.sections.triage")}</h3>
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
                        {t("detailsDrawer.actions.ack")}
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
                        {t("detailsDrawer.actions.close")}
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
                          {t("detailsDrawer.actions.reopen")}
                        </Button>
                      )}
                    </div>
                  </Card>
                )}

                {/* Identification */}
                <Card padding="md" className="space-y-4 shadow-sm">
                  <div className="flex items-center gap-2">
                    <ShieldAlertIcon size={16} className="text-text-tertiary" />
                    <h3 className={sectionTitleCls}>{t("detailsDrawer.sections.identification")}</h3>
                  </div>
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div>
                      <div className={labelCls}>{t("detailsDrawer.fields.id")}</div>
                      <div className={`${valueCls} font-mono text-xs`}>{detection.id}</div>
                    </div>
                    <div>
                      <div className={labelCls}>{t("detailsDrawer.fields.organization")}</div>
                      <div className={`${valueCls} font-mono text-xs`}>{detection.organization_id}</div>
                    </div>
                    <div className="sm:col-span-2">
                      <div className={labelCls}>{t("detailsDrawer.fields.dedupKey")}</div>
                      <div className={`${valueCls} break-all font-mono text-xs`}>{detection.dedup_key}</div>
                    </div>
                    <div>
                      <div className={labelCls}>{t("detailsDrawer.fields.count")}</div>
                      <div className={valueCls}>{detection.count ?? 1}</div>
                    </div>
                    {detection.suppression_window_seconds != null && (
                      <div>
                        <div className={labelCls}>{t("detailsDrawer.fields.suppressionWindow")}</div>
                        <div className={valueCls}>{detection.suppression_window_seconds}s</div>
                      </div>
                    )}
                  </div>
                </Card>

                {/* Temporal */}
                <Card padding="md" className="space-y-4 shadow-sm">
                  <div className="flex items-center gap-2">
                    <ClockIcon size={16} className="text-text-tertiary" />
                    <h3 className={sectionTitleCls}>{t("detailsDrawer.sections.temporal")}</h3>
                  </div>
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div>
                      <div className={labelCls}>{t("detailsDrawer.fields.firstSeen")}</div>
                      <div className={valueCls}>{detection.first_seen ? formatDate(detection.first_seen) : "-"}</div>
                    </div>
                    <div>
                      <div className={labelCls}>{t("detailsDrawer.fields.lastSeen")}</div>
                      <div className={valueCls}>{detection.last_seen ? formatDate(detection.last_seen) : "-"}</div>
                    </div>
                    {detection.created_at && (
                      <div>
                        <div className={labelCls}>{t("detailsDrawer.fields.createdAt")}</div>
                        <div className={valueCls}>{formatDate(detection.created_at)}</div>
                      </div>
                    )}
                  </div>
                </Card>

                {/* Context */}
                <Card padding="md" className="space-y-4 shadow-sm">
                  <div className="flex items-center gap-2">
                    <ShieldAlertIcon size={16} className="text-text-tertiary" />
                    <h3 className={sectionTitleCls}>{t("detailsDrawer.sections.context")}</h3>
                  </div>
                  <div className="grid gap-4 sm:grid-cols-2">
                    {detection.dialect && (
                      <div>
                        <div className={labelCls}>{t("detailsDrawer.fields.dialect")}</div>
                        <div className={`${valueCls} font-mono text-xs`}>{detection.dialect}</div>
                      </div>
                    )}
                    {detection.integration_id != null && (
                      <div>
                        <div className={labelCls}>{t("detailsDrawer.fields.integrationId")}</div>
                        <div className={`${valueCls} font-mono text-xs`}>{detection.integration_id}</div>
                      </div>
                    )}
                    {detection.source !== "correlation" && detection.source_query_id != null && (
                      <div>
                        <div className={labelCls}>{t("detailsDrawer.fields.queryId")}</div>
                        <div className={`${valueCls} font-mono text-xs`}>{detection.source_query_id}</div>
                      </div>
                    )}
                    {detection.search_result_id != null && (
                      <div>
                        <div className={labelCls}>{t("detailsDrawer.fields.searchResultId")}</div>
                        <div className={`${valueCls} font-mono text-xs`}>{detection.search_result_id}</div>
                      </div>
                    )}
                    {detection.ocsf_ref && (
                      <div className="sm:col-span-2">
                        <div className={labelCls}>{t("detailsDrawer.fields.ocsfRef")}</div>
                        <div className={`${valueCls} break-all font-mono text-xs`}>{detection.ocsf_ref}</div>
                      </div>
                    )}
                  </div>
                </Card>
              </>
            )}
          </div>
        </div>
    </Drawer>
  )
}

export default DetectionDetailsDrawer
