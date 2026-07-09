import type React from "react"
import { useEffect, useState, useCallback, useMemo } from "react"
import { Trans, useTranslation } from "react-i18next"
import { BuildingIcon, PlusIcon, TrashIcon } from "lucide-react"
import * as api from "@/services/api"
import type { Organization } from "@/types"
import { usePlatform } from "@/contexts/PlatformContext"
import { useEdition } from "@/contexts/EditionContext"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Card } from "@/components/ui/Card/Card"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Notice } from "@/components/ui/Notice/Notice"
import { Badge } from "@/components/ui/Badge/Badge"
import { Checkbox } from "@/components/ui/Checkbox/Checkbox"
import { Select } from "@/components/ui/Select/Select"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { Modal } from "@/components/ui/Modal/Modal"
import { FiltersToolbar } from "@/components/ui/FiltersToolbar/FiltersToolbar"
import { BulkActionBar } from "@/components/ui/BulkActionBar/BulkActionBar"
import { useBulkSelection } from "@/hooks/useBulkSelection"

type StatusFilter = "active" | "inactive" | "all"
type AutoManagedFilter = "all" | "true" | "false"

const PAGE_SIZE = 50

const OrganizationsPage: React.FC = () => {
  const { t } = useTranslation("admin")

  const STATUS_OPTIONS = [
    { value: "active", label: t("organizations.filters.statusActive") },
    { value: "inactive", label: t("organizations.filters.statusInactive") },
    { value: "all", label: t("organizations.filters.statusAll") },
  ]

  const AUTO_MANAGED_OPTIONS = [
    { value: "all", label: t("organizations.filters.typeAll") },
    { value: "false", label: t("organizations.filters.typeManualOnly") },
    { value: "true", label: t("organizations.filters.typeAutoOnly") },
  ]

  const [organizations, setOrganizations] = useState<Organization[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [formName, setFormName] = useState("")
  const [formDescription, setFormDescription] = useState("")
  const [saving, setSaving] = useState(false)

  // Filtros (estado imediato + debounced para search).
  const [searchInput, setSearchInput] = useState("")
  const [searchQuery, setSearchQuery] = useState("")
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active")
  const [autoManagedFilter, setAutoManagedFilter] = useState<AutoManagedFilter>("all")

  // Bulk deactivate dialog state.
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [confirmText, setConfirmText] = useState("")
  const [bulkSubmitting, setBulkSubmitting] = useState(false)

  // Single delete confirmation state.
  const [deleteTarget, setDeleteTarget] = useState<Organization | null>(null)
  const [deleteSubmitting, setDeleteSubmitting] = useState(false)

  const { refreshData } = usePlatform()

  // teto de orgs do tier (Starter single-tenant). null = ilimitado
  // (Community/MSSP/Enterprise) → sem gating. activeOrgCount é a contagem REAL
  // de orgs ativas (independente de filtro/paginação da lista).
  const { maxOrganizations } = useEdition()
  const [activeOrgCount, setActiveOrgCount] = useState<number | null>(null)

  const refreshOrgCount = useCallback(async () => {
    if (maxOrganizations == null) {
      setActiveOrgCount(null)
      return
    }
    try {
      setActiveOrgCount(await api.countActiveOrganizations())
    } catch {
      // Falha na contagem não bloqueia a página (o backend ainda enforça o 403).
      setActiveOrgCount(null)
    }
  }, [maxOrganizations])

  useEffect(() => {
    void refreshOrgCount()
  }, [refreshOrgCount])

  const atOrgLimit =
    maxOrganizations != null && activeOrgCount != null && activeOrgCount >= maxOrganizations

  // Fecha o formulário se o teto for atingido enquanto ele estava aberto (ex.:
  // outra aba criou a última org). O backend ainda enforça o 403 de qualquer forma.
  useEffect(() => {
    if (atOrgLimit) setShowForm(false)
  }, [atOrgLimit])

  const loadOrganizations = useCallback(async () => {
    try {
      setLoading(true)
      const rows = await api.listOrganizations({
        name: searchQuery || undefined,
        status: statusFilter,
        autoManaged: autoManagedFilter,
        page: 1,
        size: PAGE_SIZE,
      })
      setOrganizations(rows)
    } catch (err: any) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [searchQuery, statusFilter, autoManagedFilter])

  useEffect(() => {
    loadOrganizations()
  }, [loadOrganizations])

  // Bulk selection — bloqueia auto_managed.
  const bulk = useBulkSelection<Organization>({
    visibleItems: organizations,
    getId: (org) => String(org.id),
    isSelectable: (org) => !org.auto_managed && org.is_active,
  })

  const selectedIds = useMemo(
    () => Array.from(bulk.selected).map((id) => Number.parseInt(id, 10)).filter(Number.isFinite),
    [bulk.selected],
  )

  const requiresConfirmText = selectedIds.length > 10
  const expectedConfirmText = `DESATIVAR ${selectedIds.length}`
  const confirmTextValid = !requiresConfirmText || confirmText.trim() === expectedConfirmText

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!formName.trim()) return
    try {
      setSaving(true)
      await api.createOrganization({
        name: formName.trim(),
        description: formDescription.trim() || undefined,
      })
      setFormName("")
      setFormDescription("")
      setShowForm(false)
      await refreshOrgCount()
      await loadOrganizations()
      await refreshData()
    } catch (err: any) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  const handleConfirmDelete = async () => {
    if (!deleteTarget) return
    try {
      setDeleteSubmitting(true)
      await api.deleteOrganization(deleteTarget.id)
      setDeleteTarget(null)
      await refreshOrgCount()
      await loadOrganizations()
      await refreshData()
    } catch (err: any) {
      setError(err.message)
    } finally {
      setDeleteSubmitting(false)
    }
  }

  const openBulkDialog = () => {
    setConfirmText("")
    setConfirmOpen(true)
  }

  const handleBulkDeactivate = async () => {
    if (selectedIds.length === 0) return
    if (requiresConfirmText && confirmText.trim() !== expectedConfirmText) return
    try {
      setBulkSubmitting(true)
      const result = await api.bulkDeactivateOrganizations(selectedIds)
      bulk.clearSelection()
      setConfirmOpen(false)
      setConfirmText("")
      if (result.errors.length > 0) {
        setError(
          t("organizations.errors.bulkPartial", {
            deactivated: result.deactivated,
            processed: result.processed,
            errorsCount: result.errors.length,
          }),
        )
      }
      await refreshOrgCount()
      await loadOrganizations()
      await refreshData()
    } catch (err: any) {
      setError(err.message)
    } finally {
      setBulkSubmitting(false)
    }
  }

  const resetFilters = () => {
    setSearchInput("")
    setSearchQuery("")
    setStatusFilter("active")
    setAutoManagedFilter("all")
  }

  const hasActiveFilters =
    !!searchQuery || statusFilter !== "active" || autoManagedFilter !== "all"

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-bold text-text">{t("organizations.title")}</h1>
          {maxOrganizations != null && activeOrgCount != null && (
            <Badge variant={atOrgLimit ? "warning" : "outline"} size="sm">
              {activeOrgCount} / {maxOrganizations}{" "}
              {t("organizations.badge", { count: maxOrganizations })}
            </Badge>
          )}
        </div>
        <Button
          onClick={() => setShowForm(!showForm)}
          leftIcon={<PlusIcon size={16} />}
          disabled={atOrgLimit}
          title={
            atOrgLimit
              ? t("organizations.limitReachedTitle", { max: maxOrganizations })
              : undefined
          }
        >
          {t("organizations.newOrganization")}
        </Button>
      </div>

      {atOrgLimit && (
        <Notice variant="warning" title={t("organizations.limitReachedNotice.title")}>
          {t("organizations.limitReachedNotice.description", { max: maxOrganizations, count: maxOrganizations ?? 0 })}
        </Notice>
      )}

      {error && (
        <Notice
          variant="danger"
          action={
            <button onClick={() => setError(null)} className="text-xs underline">
              {t("common:actions.close")}
            </button>
          }
        >
          {error}
        </Notice>
      )}

      {showForm && (
        <Card padding="md">
          <form onSubmit={handleCreate} className="flex flex-col gap-4">
            <Input
              label={t("organizations.form.nameLabel")}
              value={formName}
              onChange={(e) => setFormName(e.target.value)}
              placeholder={t("organizations.form.namePlaceholder")}
              required
              autoFocus
            />
            <Input
              label={t("organizations.form.descriptionLabel")}
              value={formDescription}
              onChange={(e) => setFormDescription(e.target.value)}
              placeholder={t("organizations.form.descriptionPlaceholder")}
            />
            <div className="flex gap-2">
              <Button type="submit" loading={saving}>
                {t("common:actions.create")}
              </Button>
              <Button type="button" variant="outline" onClick={() => setShowForm(false)}>
                {t("common:actions.cancel")}
              </Button>
            </div>
          </form>
        </Card>
      )}

      <FiltersToolbar
        search={{
          value: searchInput,
          onChange: setSearchInput,
          placeholder: t("organizations.filters.searchPlaceholder"),
          label: t("organizations.filters.searchLabel"),
          ariaLabel: t("organizations.filters.searchAriaLabel"),
          debounceMs: 300,
          onDebouncedChange: (v) => setSearchQuery(v),
        }}
        hasActiveFilters={hasActiveFilters}
        onReset={resetFilters}
      >
        <div className="min-w-[180px]">
          <Select
            label={t("organizations.filters.statusLabel")}
            options={STATUS_OPTIONS}
            value={statusFilter}
            onChange={(v) => setStatusFilter(v as StatusFilter)}
            data-testid="org-filter-status"
          />
        </div>
        <div className="min-w-[220px]">
          <Select
            label={t("organizations.filters.typeLabel")}
            options={AUTO_MANAGED_OPTIONS}
            value={autoManagedFilter}
            onChange={(v) => setAutoManagedFilter(v as AutoManagedFilter)}
            data-testid="org-filter-auto-managed"
          />
        </div>
      </FiltersToolbar>

      <p className="text-xs text-text-secondary" role="note">
        {t("organizations.autoManagedNote")}
      </p>

      <BulkActionBar
        count={selectedIds.length}
        onClear={bulk.clearSelection}
        contextLabel={t("organizations.bulkBar.contextLabel")}
      >
        <Button
          variant="danger"
          size="sm"
          onClick={openBulkDialog}
          data-testid="org-bulk-deactivate"
        >
          {t("organizations.bulkBar.deactivateSelected")}
        </Button>
      </BulkActionBar>

      {loading ? (
        <LoadingSpinner size="lg" text={t("common:loading")} className="py-20" />
      ) : organizations.length === 0 ? (
        <EmptyState
          icon={<BuildingIcon size={48} />}
          title={
            hasActiveFilters
              ? t("organizations.empty.filteredTitle")
              : t("organizations.empty.defaultTitle")
          }
          description={
            hasActiveFilters
              ? t("organizations.empty.filteredDescription")
              : t("organizations.empty.defaultDescription")
          }
        />
      ) : (
        <div className="grid gap-3" role="list" aria-label={t("organizations.list.ariaLabel")}>
          {/* Header com select-all */}
          <div className="flex items-center gap-3 px-3 text-xs font-medium text-text-secondary uppercase tracking-wide">
            <Checkbox
              size="sm"
              checked={bulk.headerCheckboxState === "checked"}
              indeterminate={bulk.headerCheckboxState === "indeterminate"}
              onChange={() => bulk.toggleAllVisible()}
              aria-label={t("organizations.list.selectAllAriaLabel")}
              data-testid="org-select-all"
              disabled={bulk.visibleSelectableCount === 0}
            />
            <span>{t("organizations.list.selectEligible", { count: bulk.visibleSelectableCount })}</span>
          </div>

          {organizations.map((org) => {
            const orgIdStr = String(org.id)
            const selectable = !org.auto_managed && org.is_active
            return (
              <Card
                key={org.id}
                padding="md"
                role="listitem"
                aria-label={org.name}
                className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="flex items-start gap-3 min-w-0">
                  <Checkbox
                    size="sm"
                    checked={bulk.isSelected(orgIdStr)}
                    onChange={() => bulk.toggleOne(orgIdStr)}
                    disabled={!selectable}
                    aria-label={t("organizations.list.selectOneAriaLabel", { name: org.name })}
                    data-testid={`org-row-checkbox-${org.id}`}
                  />
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2 mb-1">
                      <span className="font-semibold text-text truncate" title={org.name}>
                        {org.name}
                      </span>
                      <Badge variant={org.is_active ? "success" : "default"} size="sm">
                        {org.is_active ? t("organizations.list.statusActiveBadge") : t("organizations.list.statusInactiveBadge")}
                      </Badge>
                      {org.auto_managed && (
                        <Badge
                          variant="primary"
                          size="sm"
                          title={t("organizations.list.autoManagedTitle")}
                        >
                          {t("organizations.list.autoManagedBadge")}
                          {org.external_provider ? ` · ${org.external_provider}` : ""}
                        </Badge>
                      )}
                      {typeof org.iris_customer_id === "number" && (
                        <Badge variant="outline" size="sm" title={t("organizations.list.irisBadgeTitle")}>
                          {t("organizations.list.irisBadgeLabel", { id: org.iris_customer_id })}
                        </Badge>
                      )}
                    </div>
                    <div
                      className="text-xs text-text-secondary truncate"
                      title={
                        org.description
                          ? `${org.slug} · ${t("organizations.list.integrationCount", { count: org.integration_count })} · ${org.description}`
                          : `${org.slug} · ${t("organizations.list.integrationCount", { count: org.integration_count })}`
                      }
                    >
                      {org.slug} · {t("organizations.list.integrationCount", { count: org.integration_count })}
                      {org.description && ` · ${org.description}`}
                    </div>
                  </div>
                </div>
                <Button
                  variant="ghost"
                  size="xs"
                  onClick={() => setDeleteTarget(org)}
                  disabled={org.auto_managed}
                  title={
                    org.auto_managed
                      ? t("organizations.list.deleteAutoManagedTitle")
                      : t("common:actions.delete")
                  }
                  className="shrink-0 self-end text-danger-500 hover:text-danger-700 hover:bg-danger-50 disabled:cursor-not-allowed disabled:opacity-40 sm:self-auto"
                  aria-label={t("organizations.list.deleteAriaLabel", { name: org.name })}
                  data-testid={`org-delete-${org.id}`}
                >
                  <TrashIcon size={16} />
                </Button>
              </Card>
            )
          })}
        </div>
      )}

      {!loading && organizations.length === PAGE_SIZE && (
        <Notice variant="info">
          {t("organizations.list.moreResultsNotice", { pageSize: PAGE_SIZE })}
        </Notice>
      )}

      {/* Bulk deactivate confirmation — usa Modal direto pra controlar disable do confirm */}
      <Modal
        open={confirmOpen}
        onClose={() => {
          if (!bulkSubmitting) {
            setConfirmOpen(false)
            setConfirmText("")
          }
        }}
        title={t("organizations.bulkDeactivateModal.title", { count: selectedIds.length })}
        size="sm"
        closeOnOverlayClick={!bulkSubmitting}
        closeOnEscape={!bulkSubmitting}
      >
        <div className="flex flex-col gap-4">
          <div className="text-sm text-text-secondary leading-relaxed flex flex-col gap-3">
            <p>
              <Trans i18nKey="organizations.bulkDeactivateModal.description" t={t} components={{ strong: <strong /> }} />
            </p>
            <p className="text-xs text-text-secondary">
              {t("organizations.bulkDeactivateModal.autoIgnoredNote")}
            </p>
            {requiresConfirmText && (
              <div className="flex flex-col gap-1">
                <label htmlFor="bulk-confirm-input" className="text-xs font-medium">
                  {t("organizations.bulkDeactivateModal.confirmTextLabel", { expected: expectedConfirmText })}{" "}
                  <code className="bg-surface-secondary px-1 rounded">
                    {expectedConfirmText}
                  </code>
                </label>
                <Input
                  id="bulk-confirm-input"
                  value={confirmText}
                  onChange={(e) => setConfirmText(e.target.value)}
                  placeholder={t("organizations.bulkDeactivateModal.confirmTextPlaceholder", { expected: expectedConfirmText })}
                  autoComplete="off"
                  data-testid="org-bulk-confirm-text"
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
                setConfirmOpen(false)
                setConfirmText("")
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
              data-testid="org-bulk-confirm"
            >
              {t("organizations.bulkDeactivateModal.confirmButton")}
            </Button>
          </div>
        </div>
      </Modal>

      {/* Single delete confirmation (substitui confirm() nativo) */}
      <ConfirmDialog
        open={deleteTarget !== null}
        title={t("organizations.deleteDialog.title")}
        confirmLabel={t("common:actions.delete")}
        cancelLabel={t("common:actions.cancel")}
        confirmVariant="danger"
        loading={deleteSubmitting}
        onClose={() => {
          if (!deleteSubmitting) setDeleteTarget(null)
        }}
        onConfirm={handleConfirmDelete}
        description={
          deleteTarget ? (
            <p>
              <Trans
                i18nKey="organizations.deleteDialog.description"
                t={t}
                values={{ name: deleteTarget.name }}
                components={{ strong: <strong /> }}
              />
            </p>
          ) : (
            ""
          )
        }
      />
    </div>
  )
}

export default OrganizationsPage
