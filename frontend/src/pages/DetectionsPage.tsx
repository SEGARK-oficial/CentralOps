"use client"

import type React from "react"
import { useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { RefreshCwIcon, ShieldAlertIcon } from "lucide-react"
import { DetectionsTable } from "@/components/detections/DetectionsTable"
import { DetectionDetailsDrawer } from "@/components/detections/DetectionDetailsDrawer"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { Notice } from "@/components/ui/Notice/Notice"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import Select, { type SelectValue } from "@/components/ui/Select/Select"
import { useDetections } from "@/hooks/useDetections"
import type { DetectionRead, DetectionStatus } from "@/types"

const DetectionsPage: React.FC = () => {
  const { t } = useTranslation("detections")
  const {
    detections,
    loading,
    error,
    statusFilter,
    setStatusFilter,
    refetch,
    triage,
    truncated,
  } = useDetections()

  const [selectedDetection, setSelectedDetection] = useState<DetectionRead | null>(null)
  const [triageError, setTriageError] = useState<string | null>(null)

  const kpis = useMemo(() => {
    return detections.reduce(
      (acc, d) => {
        acc.total += 1
        if (d.status === "open") acc.open += 1
        else if (d.status === "ack") acc.ack += 1
        else if (d.status === "closed") acc.closed += 1
        return acc
      },
      { total: 0, open: 0, ack: 0, closed: 0 },
    )
  }, [detections])

  const handleTriage = async (id: number, status: DetectionStatus) => {
    setTriageError(null)
    try {
      const updated = await triage(id, status)
      // Update selected detection with the full object returned by the backend (count, last_seen, etc.)
      setSelectedDetection((prev) => prev && prev.id === id ? updated : prev)
    } catch (err) {
      const message = err instanceof Error ? err.message : t("detections:list.feedback.triageError")
      setTriageError(message)
    }
  }

  const handleRefresh = () => {
    void refetch()
  }

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t("detections:list.pageEyebrow")}
        icon={<ShieldAlertIcon size={24} />}
        title={t("detections:list.pageTitle")}
        description={t("detections:list.pageDescription")}
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={handleRefresh}
            disabled={loading}
            leftIcon={<RefreshCwIcon size={14} />}
          >
            {t("common:actions.refresh")}
          </Button>
        }
      />

      {/* KPI cards */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {[
          { label: t("detections:list.kpis.total"), value: kpis.total, testId: "kpi-total" },
          { label: t("detections:list.kpis.open"), value: kpis.open, testId: "kpi-open" },
          { label: t("detections:list.kpis.ack"), value: kpis.ack, testId: "kpi-ack" },
          { label: t("detections:list.kpis.closed"), value: kpis.closed, testId: "kpi-closed" },
        ].map((item) => (
          <Card key={item.label} padding="sm" className="shadow-sm">
            <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">
              {item.label}
            </div>
            <div className="mt-2 text-2xl font-bold text-text" data-testid={item.testId}>
              {item.value}
            </div>
          </Card>
        ))}
      </div>

      {/* Filters toolbar */}
      <Card padding="sm" className="shadow-sm">
        <div className="flex flex-wrap items-end gap-4 p-2">
          <div className="w-48">
            <Select
              label={t("common:fields.status")}
              options={[
                { value: "", label: t("common:states.all") },
                { value: "open", label: t("detections:list.status.open") },
                { value: "ack", label: t("detections:list.status.ack") },
                { value: "closed", label: t("detections:list.status.closed") },
              ]}
              value={statusFilter}
              onValueChange={(value: SelectValue) => {
                const next = String(Array.isArray(value) ? (value[0] ?? "") : value)
                setStatusFilter(next as DetectionStatus | "")
              }}
              aria-label={t("detections:list.filterByStatusAriaLabel")}
              data-testid="detections-filter-status"
            />
          </div>
        </div>
      </Card>

      {error && (
        <Notice variant="danger" title={t("detections:list.feedback.loadError")}>
          {error}
        </Notice>
      )}

      {/* PERF-14: o endpoint não devolve total nenhum, só um array capado em
          `limit` — bater exatamente no teto é o único sinal de que pode
          haver mais detecções do que as exibidas. */}
      {!loading && !error && truncated && (
        <Notice variant="info">{t("detections:list.truncatedHint", { limit: detections.length })}</Notice>
      )}

      <DetectionsTable
        detections={detections}
        loading={loading}
        onRowClick={setSelectedDetection}
      />

      <DetectionDetailsDrawer
        open={!!selectedDetection}
        detection={selectedDetection}
        triageError={triageError}
        onClose={() => {
          setSelectedDetection(null)
          setTriageError(null)
        }}
        onTriage={handleTriage}
      />
    </div>
  )
}

export default DetectionsPage
