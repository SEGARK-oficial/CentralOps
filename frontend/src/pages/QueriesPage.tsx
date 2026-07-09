"use client"

import type React from "react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { FileTextIcon, PlusIcon, XIcon } from "lucide-react"
import { CreateQueryForm } from "@/components/queries/CreateQueryForm"
import { EditQueryModal } from "@/components/queries/EditQueryModal"
import { QueriesTable } from "@/components/queries/QueriesTable"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card/Card"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { Notice } from "@/components/ui/Notice/Notice"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { useClients } from "@/hooks/useClients"
import { useQueries } from "@/hooks/useQueries"
import { cn } from "@/lib/utils"
import type { CreateQueryRequest, Query } from "@/types"

type Feedback = {
  type: "success" | "error"
  message: string
} | null

export const QueriesPage: React.FC = () => {
  const { t } = useTranslation("schedules")
  const { queries, loading, error, createQuery, updateQuery, deleteQuery } = useQueries()
  const { clients, error: clientsError } = useClients()

  const [editingQuery, setEditingQuery] = useState<Query | null>(null)
  const [deleteCandidate, setDeleteCandidate] = useState<Query | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [showCreateForm, setShowCreateForm] = useState(false)
  const [feedback, setFeedback] = useState<Feedback>(null)

  // Sucesso some sozinho após ~5s; erros permanecem até o usuário fechar.
  useEffect(() => {
    if (feedback?.type !== "success") return
    const timer = setTimeout(() => setFeedback(null), 5000)
    return () => clearTimeout(timer)
  }, [feedback])

  const queriesWithDefaults = queries.filter((query) => (query.client_ids?.length || 0) > 0).length
  const defaultLinks = queries.reduce((total, query) => total + (query.client_ids?.length || 0), 0)

  const handleCreateQuery = async (data: CreateQueryRequest) => {
    try {
      await createQuery({ ...data, table: "xdr_index" })
      setShowCreateForm(false)
      setFeedback({ type: "success", message: t("schedules:queries.feedback.createSuccess") })
    } catch (createError) {
      const message = createError instanceof Error ? createError.message : t("schedules:queries.feedback.createError")
      setFeedback({ type: "error", message })
    }
  }

  const handleUpdateQuery = async (data: Partial<Query>) => {
    if (!editingQuery) return

    try {
      await updateQuery(editingQuery.id, { ...data, table: "xdr_index" })
      setEditingQuery(null)
      setFeedback({ type: "success", message: t("schedules:queries.feedback.updateSuccess") })
    } catch (updateError) {
      const message = updateError instanceof Error ? updateError.message : t("schedules:queries.feedback.updateError")
      setFeedback({ type: "error", message })
      throw updateError
    }
  }

  const confirmDeleteQuery = async () => {
    if (!deleteCandidate || deleting) return

    try {
      setDeleting(true)
      await deleteQuery(deleteCandidate.id)
      setDeleteCandidate(null)
      setFeedback({ type: "success", message: t("schedules:queries.feedback.deleteSuccess") })
    } catch (deleteError) {
      const message = deleteError instanceof Error ? deleteError.message : t("schedules:queries.feedback.deleteError")
      setFeedback({ type: "error", message })
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        icon={<FileTextIcon size={24} />}
        eyebrow={t("schedules:queries.pageEyebrow")}
        title={t("schedules:queries.pageTitle")}
        description={t("schedules:queries.pageDescription")}
        actions={
          <Button
            leftIcon={showCreateForm ? <XIcon size={16} /> : <PlusIcon size={16} />}
            variant={showCreateForm ? "outline" : "primary"}
            onClick={() => {
              setShowCreateForm((current) => !current)
              setFeedback(null)
            }}
          >
            {showCreateForm ? t("schedules:queries.closeForm") : t("schedules:queries.newQuery")}
          </Button>
        }
      />

      <div className="grid gap-4 sm:grid-cols-3">
        {[
          { label: t("schedules:queries.stats.savedQueries"), value: queries.length, tone: "primary" as const },
          { label: t("schedules:queries.stats.withDefaultClients"), value: queriesWithDefaults, tone: "success" as const },
          { label: t("schedules:queries.stats.totalLinks"), value: defaultLinks, tone: "default" as const },
        ].map((item) => (
          <Card key={item.label} padding="sm" className="shadow-sm">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{item.label}</div>
                <div className="mt-2 text-2xl font-bold text-text">{item.value}</div>
              </div>
              <Badge variant={item.tone} size="lg">
                {item.value}
              </Badge>
            </div>
          </Card>
        ))}
      </div>

      {error && (
        <Notice variant="danger" title={t("schedules:queries.feedback.loadError")}>
          {error}
        </Notice>
      )}

      {clientsError && (
        <Notice variant="warning" title={t("schedules:queries.feedback.clientsUnavailable")}>
          {clientsError}
        </Notice>
      )}

      {feedback && (
        <Notice
          variant={feedback.type === "success" ? "success" : "danger"}
          title={feedback.type === "success" ? t("schedules:feedback.operationCompleted") : t("schedules:feedback.operationFailed")}
          action={
            <Button
              variant="ghost"
              size="sm"
              aria-label={t("schedules:queries.closeNotice")}
              leftIcon={<XIcon size={16} />}
              onClick={() => setFeedback(null)}
            />
          }
        >
          {feedback.message}
        </Notice>
      )}

      <div className={cn("grid gap-6", showCreateForm ? "xl:grid-cols-[minmax(0,420px)_minmax(0,1fr)]" : "grid-cols-1")}>
        {showCreateForm && (
          <Card className="shadow-sm">
            <CardHeader>
              <CardTitle>{t("schedules:queries.createCard.title")}</CardTitle>
              <CardDescription>{t("schedules:queries.createCard.description")}</CardDescription>
            </CardHeader>
            <CardContent>
              <CreateQueryForm clients={clients} onSubmit={handleCreateQuery} onCancel={() => setShowCreateForm(false)} loading={loading} />
            </CardContent>
          </Card>
        )}

        <Card className="shadow-sm">
          <CardHeader>
            <CardTitle>
              {t("schedules:queries.catalogCard.title")}
              <Badge variant="primary" size="sm" className="ml-2">
                {queries.length}
              </Badge>
            </CardTitle>
            <CardDescription>{t("schedules:queries.catalogCard.description")}</CardDescription>
          </CardHeader>
          <CardContent>
            <QueriesTable
              queries={queries}
              loading={loading}
              onEdit={setEditingQuery}
              onDelete={(queryId) => setDeleteCandidate(queries.find((query) => query.id === queryId) || null)}
            />
          </CardContent>
        </Card>
      </div>

      <EditQueryModal
        query={editingQuery}
        clients={clients}
        open={!!editingQuery}
        onClose={() => setEditingQuery(null)}
        onSubmit={handleUpdateQuery}
        loading={loading}
      />

      <ConfirmDialog
        open={!!deleteCandidate}
        title={t("schedules:queries.deleteDialog.title")}
        description={
          <p>
            {t("schedules:queries.deleteDialog.confirmPrefix")} <strong>{deleteCandidate?.title}</strong>
            {t("schedules:queries.deleteDialog.confirmSuffix")}
          </p>
        }
        confirmLabel={t("schedules:queries.deleteDialog.confirmLabel")}
        loading={deleting}
        onConfirm={confirmDeleteQuery}
        onClose={() => {
          if (!deleting) setDeleteCandidate(null)
        }}
      />
    </div>
  )
}

export default QueriesPage
