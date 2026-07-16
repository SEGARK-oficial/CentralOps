import type React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { useNavigate } from "react-router-dom"
import { Trans, useTranslation } from "react-i18next"
import {
  EyeIcon,
  PencilIcon,
  PlayIcon,
  PlugIcon,
  PlusIcon,
  RefreshCcwIcon,
  TrashIcon,
  UsersIcon,
  SparklesIcon,
} from "lucide-react"
import * as api from "@/services/api"
import { authStatusLabel, authStatusVariant } from "@/lib/labels"
import type { Integration } from "@/types"
import { IntegrationForm } from "@/components/integrations/IntegrationForm"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { Checkbox } from "@/components/ui/Checkbox/Checkbox"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { Input } from "@/components/ui/Input/Input"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { Select } from "@/components/ui/Select/Select"
import { FiltersToolbar } from "@/components/ui/FiltersToolbar/FiltersToolbar"
import { BulkActionBar } from "@/components/ui/BulkActionBar/BulkActionBar"
import { useBulkSelection } from "@/hooks/useBulkSelection"
import { useAuth } from "@/contexts/AuthContext"
import { usePlatform } from "@/contexts/PlatformContext"
import { formatDateTime } from "@/lib/intl"

type KindFilter = "all" | "tenant" | "partner" | "organization"
type StatusFilter = "active" | "inactive" | "all"

const PAGE_SIZE = 50

const IntegrationsPage: React.FC = () => {
  const { t } = useTranslation("integrations")
  const navigate = useNavigate()
  const { user } = useAuth()
  const { organizations, refreshData, selectedOrgId, selectedPlatform } = usePlatform()
  const isAdmin = user.role === "admin"

  const [integrations, setIntegrations] = useState<Integration[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [feedback, setFeedback] = useState<{ type: "success" | "error"; message: string } | null>(null)

  useEffect(() => {
    if (!feedback) return
    const timeoutId = setTimeout(() => setFeedback(null), 5000)
    return () => clearTimeout(timeoutId)
  }, [feedback])
  const [testingId, setTestingId] = useState<number | null>(null)
  const [syncingId, setSyncingId] = useState<number | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [editingIntegration, setEditingIntegration] = useState<Integration | null>(null)
  // Partner cascade-delete confirmation: shows the list of children before
  // re-issuing the DELETE with ?force=true.
  const [cascadeDelete, setCascadeDelete] = useState<{
    integration: Integration
    children: { id: number; name: string }[]
  } | null>(null)
  // Single deactivate confirmation (substitui window.confirm — P1).
  const [deactivateCandidate, setDeactivateCandidate] = useState<Integration | null>(null)
  const [deactivating, setDeactivating] = useState(false)

  // ── Filtros (estado imediato + debounced para search) ────────────────
  const [searchInput, setSearchInput] = useState("")
  const [searchQuery, setSearchQuery] = useState("")
  const [kindFilter, setKindFilter] = useState<KindFilter>("all")
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active")

  // ── Bulk deactivate dialog state ─────────────────────────────────────
  const [bulkConfirmOpen, setBulkConfirmOpen] = useState(false)
  const [bulkConfirmText, setBulkConfirmText] = useState("")
  const [bulkSubmitting, setBulkSubmitting] = useState(false)

  const kindOptions = useMemo(
    () => [
      { value: "all", label: t("list.kindOptions.all") },
      { value: "tenant", label: t("list.kindOptions.tenant") },
      { value: "partner", label: t("list.kindOptions.partner") },
      { value: "organization", label: t("list.kindOptions.organization") },
    ],
    [t],
  )

  const statusOptions = useMemo(
    () => [
      { value: "active", label: t("list.statusOptions.active") },
      { value: "inactive", label: t("list.statusOptions.inactive") },
      { value: "all", label: t("list.statusOptions.all") },
    ],
    [t],
  )

  const loadIntegrations = useCallback(async () => {
    try {
      setLoading(true)
      const data = await api.listIntegrations({
        organizationId: selectedOrgId ?? undefined,
        platform: selectedPlatform ?? undefined,
        name: searchQuery || undefined,
        kind: kindFilter === "all" ? undefined : kindFilter,
        status: statusFilter,
        // Admin pode ver inativas via filter; não-admin é forçado a active no servidor.
        includeInactive: isAdmin && statusFilter !== "active",
        page: 1,
        size: PAGE_SIZE,
      })
      setIntegrations(data)
    } catch (loadError) {
      const message = loadError instanceof Error ? loadError.message : t("list.feedback.loadError")
      setFeedback({ type: "error", message })
    } finally {
      setLoading(false)
    }
  }, [
    isAdmin,
    selectedOrgId,
    selectedPlatform,
    searchQuery,
    kindFilter,
    statusFilter,
    t,
  ])

  useEffect(() => {
    void loadIntegrations()
  }, [loadIntegrations])

  // ── Bulk selection (Partner BLOCKED — decisão #2) ────────────────────
  const bulk = useBulkSelection<Integration>({
    visibleItems: integrations,
    getId: (integration) => String(integration.id),
    isSelectable: (integration) =>
      integration.kind !== "partner" &&
      integration.kind !== "organization" &&
      integration.is_active,
  })

  const selectedIds = useMemo(
    () =>
      Array.from(bulk.selected)
        .map((id) => Number.parseInt(id, 10))
        .filter(Number.isFinite),
    [bulk.selected],
  )

  const requiresConfirmText = selectedIds.length > 10
  const expectedConfirmText = t("list.bulkDialog.confirmTextValue", { count: selectedIds.length })
  const confirmTextValid =
    !requiresConfirmText || bulkConfirmText.trim() === expectedConfirmText

  // ── Filtro org global: badge de aviso ────────────────────────────────
  const activeOrgFilter = useMemo(() => {
    if (!selectedOrgId) return null
    const org = organizations.find((o) => o.id === selectedOrgId)
    return org ? { id: org.id, name: org.name } : null
  }, [selectedOrgId, organizations])

  const hasActiveFilters =
    !!searchQuery || kindFilter !== "all" || statusFilter !== "active"

  const resetFilters = () => {
    setSearchInput("")
    setSearchQuery("")
    setKindFilter("all")
    setStatusFilter("active")
  }

  // ── Handlers de CRUD individuais (mantidos) ──────────────────────────
  const handleCreate = async (payload: Parameters<typeof api.createIntegration>[0]) => {
    try {
      setSaving(true)
      setFeedback(null)
      await api.createIntegration(payload)
      setCreateOpen(false)
      await Promise.all([loadIntegrations(), refreshData()])
      setFeedback({ type: "success", message: t("list.feedback.createSuccess") })
    } catch (createError) {
      const message = createError instanceof Error ? createError.message : t("list.feedback.createError")
      setFeedback({ type: "error", message })
      throw createError
    } finally {
      setSaving(false)
    }
  }

  const handleUpdate = async (payload: Parameters<typeof api.updateIntegration>[1]) => {
    if (!editingIntegration) return

    try {
      setSaving(true)
      setFeedback(null)
      await api.updateIntegration(editingIntegration.id, payload)
      setEditingIntegration(null)
      await Promise.all([loadIntegrations(), refreshData()])
      setFeedback({ type: "success", message: t("list.feedback.updateSuccess") })
    } catch (updateError) {
      const message = updateError instanceof Error ? updateError.message : t("list.feedback.updateError")
      setFeedback({ type: "error", message })
      throw updateError
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async (integrationId: number) => {
    try {
      setTestingId(integrationId)
      setFeedback(null)
      const result = await api.testIntegrationConnection(integrationId)
      await Promise.all([loadIntegrations(), refreshData()])
      setFeedback({
        type: result.status === "healthy" || result.status === "degraded" ? "success" : "error",
        message: t("list.feedback.testCompleted", { status: result.status }),
      })
    } catch (testError) {
      const message = testError instanceof Error ? testError.message : t("list.feedback.testError")
      setFeedback({ type: "error", message })
    } finally {
      setTestingId(null)
    }
  }

  // Aciona o fluxo de desativação: Partner/Organization vão direto para a
  // API (que pode disparar o cascade-confirm via 409); os demais abrem o
  // ConfirmDialog do design system.
  const handleDeactivate = (integration: Integration) => {
    const isPartner = integration.kind === "partner" || integration.kind === "organization"
    if (isPartner) {
      void runDeactivate(integration)
      return
    }
    setDeactivateCandidate(integration)
  }

  const runDeactivate = async (integration: Integration) => {
    try {
      setDeactivating(true)
      setFeedback(null)
      await api.deleteIntegration(integration.id)
      setDeactivateCandidate(null)
      await Promise.all([loadIntegrations(), refreshData()])
      setFeedback({ type: "success", message: t("list.feedback.deactivateSuccess") })
    } catch (deleteError) {
      // The backend returns 409 with a structured detail when a Partner has
      // active children. Open the cascade-confirm modal in that case.
      const isApiError =
        deleteError && typeof deleteError === "object" && "statusCode" in deleteError
      if (isApiError && (deleteError as { statusCode?: number }).statusCode === 409) {
        const detail = (deleteError as { details?: Record<string, unknown> }).details
        const children = Array.isArray(detail?.children)
          ? (detail.children as Array<{ id: number; name: string }>)
          : []
        if (children.length > 0) {
          setDeactivateCandidate(null)
          setCascadeDelete({ integration, children })
          return
        }
      }
      const message = deleteError instanceof Error ? deleteError.message : t("list.feedback.deactivateError")
      setFeedback({ type: "error", message })
    } finally {
      setDeactivating(false)
    }
  }

  const confirmCascadeDelete = async () => {
    if (!cascadeDelete) return
    try {
      setFeedback(null)
      const result = await api.deleteIntegration(cascadeDelete.integration.id, { force: true })
      setCascadeDelete(null)
      await Promise.all([loadIntegrations(), refreshData()])
      setFeedback({
        type: "success",
        message: result.detail || t("list.feedback.cascadeDeactivated", { affected: result.affected ?? "?" }),
      })
    } catch (deleteError) {
      const message = deleteError instanceof Error ? deleteError.message : t("list.feedback.deactivatePartnerError")
      setFeedback({ type: "error", message })
    }
  }

  const handleSyncTenants = async (integrationId: number) => {
    try {
      setSyncingId(integrationId)
      setFeedback(null)
      const result = await api.syncPartnerTenants(integrationId)
      // O backend recusa honestamente (HTTP 200 + status) quando o import de
      // tenants não está habilitado: "enterprise_required" = artefato EE ausente
      // (Community); "license_required" = EE presente sem licença da feature.
      // Sem isto o toast de sucesso mentiria — nada será importado.
      if (result.status === "enterprise_required") {
        setFeedback({ type: "error", message: t("list.feedback.syncEnterpriseRequired") })
        return
      }
      if (result.status === "license_required") {
        setFeedback({ type: "error", message: t("list.feedback.syncLicenseRequired") })
        return
      }
      setFeedback({
        type: "success",
        message: t("list.feedback.syncStarted"),
      })
      // Quick reload to surface the partial result; the detail page is the
      // canonical place to track the full sync progress.
      void loadIntegrations()
    } catch (syncError) {
      const isApiError =
        syncError && typeof syncError === "object" && "statusCode" in syncError
      const status = isApiError ? (syncError as { statusCode?: number }).statusCode : undefined
      if (status === 429) {
        setFeedback({
          type: "error",
          message: t("list.feedback.syncInProgress"),
        })
        return
      }
      const message = syncError instanceof Error ? syncError.message : t("list.feedback.syncError")
      setFeedback({ type: "error", message })
    } finally {
      setSyncingId(null)
    }
  }

  // ── Bulk handlers ────────────────────────────────────────────────────
  const openBulkDialog = () => {
    setBulkConfirmText("")
    setBulkConfirmOpen(true)
  }

  const handleBulkDeactivate = async () => {
    if (selectedIds.length === 0) return
    if (requiresConfirmText && bulkConfirmText.trim() !== expectedConfirmText) return
    try {
      setBulkSubmitting(true)
      const result = await api.bulkDeactivateIntegrations(selectedIds)
      bulk.clearSelection()
      setBulkConfirmOpen(false)
      setBulkConfirmText("")
      if (result.errors.length > 0) {
        setFeedback({
          type: "error",
          message: t("list.feedback.bulkDeactivatedWithWarnings", {
            deactivated: result.deactivated,
            processed: result.processed,
            errorCount: result.errors.length,
          }),
        })
      } else {
        setFeedback({
          type: "success",
          message: t("list.feedback.bulkDeactivated", { count: result.deactivated }),
        })
      }
      await Promise.all([loadIntegrations(), refreshData()])
    } catch (bulkError) {
      const message = bulkError instanceof Error ? bulkError.message : t("list.feedback.bulkDeactivateError")
      setFeedback({ type: "error", message })
    } finally {
      setBulkSubmitting(false)
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        icon={<PlugIcon size={24} />}
        eyebrow={t("list.eyebrow")}
        title={t("list.title")}
        description={t("list.description")}
        actions={
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => void loadIntegrations()} leftIcon={<RefreshCcwIcon size={16} />} disabled={loading}>
              {t("common:actions.refresh")}
            </Button>
            {isAdmin && (
              <Button onClick={() => setCreateOpen(true)} leftIcon={<PlusIcon size={16} />}>
                {t("list.newIntegration")}
              </Button>
            )}
          </div>
        }
      />

      {feedback && (
        <Notice variant={feedback.type === "success" ? "success" : "danger"} title={feedback.type === "success" ? t("list.operationSuccessTitle") : t("list.operationErrorTitle")}>
          {feedback.message}
        </Notice>
      )}

      <FiltersToolbar
        search={{
          value: searchInput,
          onChange: setSearchInput,
          placeholder: t("list.searchPlaceholder"),
          label: t("list.searchLabel"),
          ariaLabel: t("list.searchAriaLabel"),
          debounceMs: 300,
          onDebouncedChange: (v) => setSearchQuery(v),
        }}
        hasActiveFilters={hasActiveFilters}
        onReset={resetFilters}
      >
        <div className="min-w-[200px]">
          <Select
            label={t("list.kindFilterLabel")}
            options={kindOptions}
            value={kindFilter}
            onChange={(v) => setKindFilter(v as KindFilter)}
            data-testid="integration-filter-kind"
          />
        </div>
        <div className="min-w-[180px]">
          <Select
            label={t("list.statusFilterLabel")}
            options={statusOptions}
            value={statusFilter}
            onChange={(v) => setStatusFilter(v as StatusFilter)}
            data-testid="integration-filter-status"
          />
        </div>
      </FiltersToolbar>

      {/* Badge "Filtro ativo: org=X" — decisão #5. */}
      {activeOrgFilter && (
        <div
          className="flex items-center gap-2 text-xs text-text-secondary"
          data-testid="integration-active-org-badge"
        >
          <Badge variant="primary" size="sm">
            {t("list.activeOrgFilterBadge", { name: activeOrgFilter.name })}
          </Badge>
          <span>
            {t("list.activeOrgFilterNote")}
          </span>
        </div>
      )}

      <p className="text-xs text-text-secondary" role="note">
        {t("list.partnerBulkNote")}
      </p>

      <BulkActionBar
        count={selectedIds.length}
        onClear={bulk.clearSelection}
        contextLabel={t("list.bulkContextLabel")}
      >
        <Button
          variant="danger"
          size="sm"
          onClick={openBulkDialog}
          data-testid="integration-bulk-deactivate"
        >
          {t("list.bulkDeactivateSelected")}
        </Button>
      </BulkActionBar>

      {loading ? (
        <Card padding="lg" className="text-center text-sm text-text-secondary">
          {t("list.loading")}
        </Card>
      ) : integrations.length === 0 ? (
        <EmptyState
          icon={<PlugIcon size={48} />}
          title={
            hasActiveFilters || activeOrgFilter
              ? t("list.emptyState.filteredTitle")
              : t("list.emptyState.title")
          }
          description={
            hasActiveFilters || activeOrgFilter
              ? t("list.emptyState.filteredDescription")
              : t("list.emptyState.description")
          }
        />
      ) : (
        <div className="grid gap-4">
          {/* Header com select-all */}
          <div className="flex items-center gap-3 px-3 text-xs font-medium text-text-secondary uppercase tracking-wide">
            <Checkbox
              size="sm"
              checked={bulk.headerCheckboxState === "checked"}
              indeterminate={bulk.headerCheckboxState === "indeterminate"}
              onChange={() => bulk.toggleAllVisible()}
              aria-label={t("list.selectAllAriaLabel")}
              data-testid="integration-select-all"
              disabled={bulk.visibleSelectableCount === 0}
            />
            <span>{t("list.selectAllEligible", { count: bulk.visibleSelectableCount })}</span>
          </div>

          {integrations.map((integration) => {
            const integrationIdStr = String(integration.id)
            const selectable =
              integration.kind !== "partner" &&
              integration.kind !== "organization" &&
              integration.is_active
            return (
              <Card key={integration.id} padding="md" className="space-y-4 shadow-sm">
                <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                  <div className="flex items-start gap-3 min-w-0">
                    <Checkbox
                      size="sm"
                      checked={bulk.isSelected(integrationIdStr)}
                      onChange={() => bulk.toggleOne(integrationIdStr)}
                      disabled={!selectable}
                      aria-label={t("list.selectRowAriaLabel", { name: integration.name })}
                      data-testid={`integration-row-checkbox-${integration.id}`}
                    />
                    <div className="space-y-3 min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <h2
                          className="max-w-full min-w-0 truncate text-lg font-semibold text-text"
                          title={integration.name}
                        >
                          {integration.name}
                        </h2>
                        <Badge variant={integration.platform === "sophos" ? "primary" : "success"} size="sm">
                          {integration.platform}
                        </Badge>
                        {(integration.kind === "partner" || integration.kind === "organization") && (
                          <Badge variant="primary" size="sm">
                            {integration.kind === "partner" ? t("list.kindOptions.partner") : t("list.kindOptions.organization")}
                            {typeof integration.children_count === "number"
                              ? ` · ${t("list.childrenCount", { count: integration.children_count })}`
                              : ""}
                          </Badge>
                        )}
                        {integration.auto_managed && (
                          <Badge variant="outline" size="sm">
                            {t("list.autoManaged")}
                          </Badge>
                        )}
                        <Badge variant={integration.is_active ? "success" : "warning"} size="sm">
                          {integration.is_active ? t("form.statusActive") : t("form.statusInactive")}
                        </Badge>
                        <Badge variant={authStatusVariant(integration.auth_status)} size="sm">
                          {authStatusLabel(integration.auth_status)}
                        </Badge>
                      </div>

                      <div className="text-sm text-text-secondary">
                        {integration.organization_name || t("list.noOrganization")}
                      </div>

                      <div className="grid gap-2 text-sm text-text-secondary md:grid-cols-2">
                        <div>
                          <span className="font-medium text-text">{t("list.primaryEndpoint")}</span>{" "}
                          {integration.platform === "sophos" ? integration.region || t("list.auto") : integration.manager_url || t("list.notConfigured")}
                        </div>
                        <div>
                          <span className="font-medium text-text">{t("list.lastChecked")}</span>{" "}
                          {integration.last_checked_at ? formatDateTime(integration.last_checked_at) : t("list.never")}
                        </div>
                        {integration.platform === "wazuh" && (
                          <>
                            <div>
                              <span className="font-medium text-text">{t("list.manager")}</span>{" "}
                              {integration.manager_api_username || t("list.notInformed")}
                            </div>
                            <div>
                              <span className="font-medium text-text">{t("list.indexer")}</span>{" "}
                              {integration.indexer_url ? integration.indexer_url : t("list.notEnabled")}
                            </div>
                          </>
                        )}
                      </div>


                      <div className="flex flex-wrap gap-2">
                        {integration.capabilities.map((capability) => (
                          <Badge key={capability} variant="outline" size="sm">
                            {capability}
                          </Badge>
                        ))}
                      </div>
                    </div>
                  </div>

                  <div className="flex flex-wrap gap-2">
                    <Button variant="outline" size="sm" onClick={() => navigate(`/integrations/${integration.id}`)} leftIcon={<EyeIcon size={14} />}>
                      {t("list.details")}
                    </Button>
                    {isAdmin && (
                      <>
                        {(integration.kind === "partner" || integration.kind === "organization") && (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => void handleSyncTenants(integration.id)}
                            disabled={syncingId === integration.id}
                            leftIcon={<SparklesIcon size={14} />}
                          >
                            {syncingId === integration.id ? t("list.syncingTenants") : t("list.syncTenants")}
                          </Button>
                        )}
                        {!integration.auto_managed && (
                          <Button variant="outline" size="sm" onClick={() => setEditingIntegration(integration)} leftIcon={<PencilIcon size={14} />}>
                            {t("common:actions.edit")}
                          </Button>
                        )}
                        <Button variant="outline" size="sm" onClick={() => void handleTest(integration.id)} disabled={testingId === integration.id} leftIcon={<PlayIcon size={14} />}>
                          {testingId === integration.id ? t("list.testing") : t("common:actions.test")}
                        </Button>
                        {integration.is_active && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => handleDeactivate(integration)}
                            className="text-danger-600 hover:bg-danger-50 hover:text-danger-700"
                            aria-label={t("list.deactivateAriaLabel", { name: integration.name })}
                          >
                            <TrashIcon size={14} />
                          </Button>
                        )}
                      </>
                    )}
                  </div>
                </div>
              </Card>
            )
          })}

          {/* Lista é array cru (sem total). Avisa truncamento quando o lote
              vem cheio (length === PAGE_SIZE) — P1. */}
          {integrations.length === PAGE_SIZE && (
            <Notice variant="info" title={t("list.partialResultsTitle")}>
              {t("list.partialResultsDescription", { count: PAGE_SIZE })}
            </Notice>
          )}
        </div>
      )}

      <Modal open={createOpen} onClose={() => setCreateOpen(false)} title={t("list.newIntegration")} size="lg">
        <IntegrationForm
          mode="create"
          organizations={organizations}
          loading={saving}
          onCancel={() => setCreateOpen(false)}
          onSubmit={handleCreate}
        />
      </Modal>

      <Modal open={!!editingIntegration} onClose={() => setEditingIntegration(null)} title={t("list.editIntegration")} size="lg">
        <IntegrationForm
          mode="edit"
          organizations={organizations}
          integration={editingIntegration}
          loading={saving}
          onCancel={() => setEditingIntegration(null)}
          onSubmit={handleUpdate}
        />
      </Modal>

      {/* Bulk deactivate confirmation — decisão #3: typing pra >10 itens. */}
      <Modal
        open={bulkConfirmOpen}
        onClose={() => {
          if (!bulkSubmitting) {
            setBulkConfirmOpen(false)
            setBulkConfirmText("")
          }
        }}
        title={t("list.bulkDialog.title", { count: selectedIds.length })}
        size="sm"
        closeOnOverlayClick={!bulkSubmitting}
        closeOnEscape={!bulkSubmitting}
      >
        <div className="flex flex-col gap-4">
          <div className="text-sm text-text-secondary leading-relaxed flex flex-col gap-3">
            <p>
              <Trans i18nKey="list.bulkDialog.description" t={t} components={{ strong: <strong /> }} />
            </p>
            <p className="text-xs text-text-secondary">
              {t("list.bulkDialog.note")}
            </p>
            {requiresConfirmText && (
              <div className="flex flex-col gap-1">
                <label htmlFor="integration-bulk-confirm-input" className="text-xs font-medium">
                  <Trans
                    i18nKey="list.bulkDialog.confirmTextLabel"
                    t={t}
                    values={{ text: expectedConfirmText }}
                    components={[
                      <code key="0" className="bg-surface-secondary px-1 rounded" />,
                    ]}
                  />
                </label>
                <Input
                  id="integration-bulk-confirm-input"
                  value={bulkConfirmText}
                  onChange={(e) => setBulkConfirmText(e.target.value)}
                  placeholder={expectedConfirmText}
                  data-testid="integration-bulk-confirm-text"
                  disabled={bulkSubmitting}
                />
              </div>
            )}
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                setBulkConfirmOpen(false)
                setBulkConfirmText("")
              }}
              disabled={bulkSubmitting}
            >
              {t("common:actions.cancel")}
            </Button>
            <Button
              type="button"
              variant="danger"
              onClick={() => void handleBulkDeactivate()}
              loading={bulkSubmitting}
              disabled={!confirmTextValid}
              data-testid="integration-bulk-confirm"
            >
              {t("common:actions.disable")}
            </Button>
          </div>
        </div>
      </Modal>

      {/* Confirmação de desativação individual — substitui window.confirm (P1). */}
      <ConfirmDialog
        open={!!deactivateCandidate}
        title={t("list.deactivateDialog.title")}
        description={
          <Trans
            i18nKey="list.deactivateDialog.description"
            t={t}
            values={{ name: deactivateCandidate?.name ?? "" }}
            components={{ strong: <strong /> }}
          />
        }
        confirmLabel={t("common:actions.disable")}
        confirmVariant="danger"
        loading={deactivating}
        data-testid="integration-deactivate-confirm"
        onConfirm={() => {
          if (deactivateCandidate) void runDeactivate(deactivateCandidate)
        }}
        onClose={() => setDeactivateCandidate(null)}
      />

      <Modal
        open={!!cascadeDelete}
        onClose={() => setCascadeDelete(null)}
        title={t("list.cascadeDialog.title")}
        size="md"
      >
        {cascadeDelete && (
          <div className="space-y-4">
            <Notice variant="warning" title={t("list.cascadeDialog.cascadeOperationTitle")}>
              <p className="text-sm">
                <Trans
                  i18nKey="list.cascadeDialog.description"
                  t={t}
                  values={{ name: cascadeDelete.integration.name, count: cascadeDelete.children.length }}
                  components={{ strong: <strong /> }}
                />
              </p>
            </Notice>
            <div className="rounded-md border border-border bg-surface-tertiary/30 p-3">
              <div className="mb-2 flex items-center gap-2 text-xs font-medium text-text-secondary">
                <UsersIcon size={14} />
                {t("list.cascadeDialog.childrenListTitle")}
              </div>
              <ul className="max-h-48 space-y-1 overflow-auto text-sm">
                {cascadeDelete.children.map((child) => (
                  <li key={child.id} className="flex items-center justify-between rounded-sm px-2 py-1 hover:bg-surface-tertiary/60">
                    <span className="text-text">{child.name}</span>
                    <span className="text-xs text-text-tertiary">#{child.id}</span>
                  </li>
                ))}
              </ul>
            </div>
            <div className="flex justify-end gap-3">
              <Button variant="outline" onClick={() => setCascadeDelete(null)}>
                {t("common:actions.cancel")}
              </Button>
              <Button variant="danger" onClick={() => void confirmCascadeDelete()}>
                {t("list.cascadeDialog.confirmAndDeactivateAll")}
              </Button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  )
}

export default IntegrationsPage
