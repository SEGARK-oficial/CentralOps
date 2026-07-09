"use client"

import type React from "react"
import { useState } from "react"
import { PlusIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/Button/Button"
import { Modal } from "@/components/ui/Modal/Modal"
import { usePermission } from "@/hooks/usePermission"
import { useBackfillJobs } from "@/hooks/useBackfillJobs"
import type { BackfillJob } from "@/types"
import { BackfillForm } from "./BackfillForm"
import { BackfillJobsTable } from "./BackfillJobsTable"

interface IntegrationBackfillPanelProps {
  integrationId: number
  platform: string
}

export const IntegrationBackfillPanel: React.FC<IntegrationBackfillPanelProps> = ({
  integrationId,
  platform,
}) => {
  const { t } = useTranslation("config")
  const canWrite = usePermission("integration.write")
  const [formOpen, setFormOpen] = useState(false)

  const { items, total: _total, isLoading, error, refetch, createJob, cancelJob } = useBackfillJobs(
    integrationId,
    { limit: 50 },
  )

  const handleSuccess = (_job: BackfillJob) => {
    setFormOpen(false)
    refetch()
  }

  return (
    <div data-testid="backfill-panel" className="flex flex-col gap-5">
      {/* Header */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold text-text">
            {t("backfill.panel.title")}
          </h2>
          <p className="text-sm text-text-secondary">
            {t("backfill.panel.description")}
          </p>
        </div>
        {canWrite && (
          <Button
            data-testid="new-backfill-button"
            leftIcon={<PlusIcon size={16} />}
            onClick={() => setFormOpen(true)}
          >
            {t("backfill.panel.newBackfill")}
          </Button>
        )}
      </div>

      {/* Tabela de jobs */}
      <BackfillJobsTable
        items={items}
        isLoading={isLoading}
        error={error}
        onCancel={cancelJob}
      />

      {/* Modal com formulário */}
      <Modal
        open={formOpen}
        onClose={() => setFormOpen(false)}
        title={t("backfill.panel.modalTitle")}
        size="lg"
      >
        <BackfillForm
          integrationId={integrationId}
          platform={platform}
          onSuccess={handleSuccess}
          onCancel={() => setFormOpen(false)}
          onCreateJob={createJob}
        />
      </Modal>
    </div>
  )
}

export default IntegrationBackfillPanel
