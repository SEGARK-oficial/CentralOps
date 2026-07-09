import type React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import {
  SendIcon,
  PlusIcon,
  RefreshCcwIcon,
  EyeIcon,
  PencilIcon,
  PlayIcon,
  Trash2Icon,
  MinusCircleIcon,
} from "lucide-react"
import * as api from "@/services/api"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { Badge } from "@/components/ui/Badge/Badge"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { FiltersToolbar } from "@/components/ui/FiltersToolbar/FiltersToolbar"
import { Select } from "@/components/ui/Select/Select"
import { SkeletonCard } from "@/components/ui/Skeleton"
import { ErrorState } from "@/components/ui/ErrorState"
import { DestinationForm } from "@/components/destinations/DestinationForm"
import { DestinationTypeGallery, kindToIcon } from "@/components/destinations/DestinationTypeGallery"
import { StatusBadge, healthEncoding } from "@/lib/severity"
import { fmtRate } from "@/lib/fmt"
import type {
  Destination,
  DestinationCreateRequest,
  DestinationUpdateRequest,
  DestinationType,
  DestinationHealthItem,
} from "@/types"

type Feedback = { type: "success" | "error"; message: string }

// ── Badge de status de saúde por destino ────────────────────────────────────────
//
// Reusa o encoding colorblind-safe de severity.ts (healthy=success/degraded=warning/
// unhealthy=danger/unknown=outline). O estado "disabled" do contrato de saúde é
// tratado como neutro/cinza (não é uma falha — o destino está intencionalmente off).

const DestinationStatusBadge: React.FC<{
  health: DestinationHealthItem | undefined
  destinationId: string
}> = ({ health, destinationId }) => {
  const { t } = useTranslation("routing")
  if (!health) return null

  if (health.status === "disabled") {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full bg-surface-tertiary px-2 py-0.5 text-xs font-medium text-text-secondary"
        data-testid={`destination-status-${destinationId}`}
        aria-label={`${t("common:fields.status")}: ${t("destinationsPage.statusDisabled")}`}
      >
        <MinusCircleIcon size={13} aria-hidden="true" />
        {t("destinationsPage.statusDisabled")}
      </span>
    )
  }

  const encoding = healthEncoding(health.status)
  return (
    <span data-testid={`destination-status-${destinationId}`}>
      <StatusBadge encoding={encoding} iconSize={13} />
    </span>
  )
}

// ── Ícone por kind ────────────────────────────────────────────────────────────
// Reaproveita o helper de marca da galeria (single source of truth) — sem mapa
// hardcoded duplicado aqui.

function kindIcon(kind: string): React.ReactNode {
  return (
    <span className="inline-flex h-5 w-5 items-center justify-center rounded bg-white ring-1 ring-black/5">
      {kindToIcon(kind, 14)}
    </span>
  )
}

const DestinationsPage: React.FC = () => {
  const { t } = useTranslation("routing")
  const navigate = useNavigate()

  // ── Opções de filtro ────────────────────────────────────────────────────
  const ENABLED_OPTIONS = useMemo(
    () => [
      { value: "all", label: t("destinationsPage.filterStatusAllOption") },
      { value: "enabled", label: t("destinationsPage.filterStatusEnabledOption") },
      { value: "disabled", label: t("destinationsPage.filterStatusDisabledOption") },
    ],
    [t],
  )
  const [destinations, setDestinations] = useState<Destination[]>([])
  // Saúde em lote (1 chamada) → mapa destination_id → item. Degrada sem badge se falhar.
  const [healthMap, setHealthMap] = useState<Map<string, DestinationHealthItem>>(new Map())
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [feedback, setFeedback] = useState<Feedback | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  // Fluxo de criação em duas etapas: galeria → formulário
  const [createStep, setCreateStep] = useState<"gallery" | "form">("gallery")
  const [createKind, setCreateKind] = useState("")
  const [destinationCatalog, setDestinationCatalog] = useState<DestinationType[]>([])
  const [editing, setEditing] = useState<Destination | null>(null)
  const [deleting, setDeleting] = useState<Destination | null>(null)
  const [testingId, setTestingId] = useState<string | null>(null)

  // Filtros
  const [searchInput, setSearchInput] = useState("")
  const [searchQuery, setSearchQuery] = useState("")
  const [kindFilter, setKindFilter] = useState("all")
  const [enabledFilter, setEnabledFilter] = useState("all")

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setLoadError(null)
      // Saúde carrega em paralelo (Promise.allSettled) — sua falha NÃO quebra a página.
      const [data, types, healthResult] = await Promise.all([
        api.listDestinations({ include_disabled: true, limit: 200 }),
        api.listDestinationTypes(),
        api.listDestinationsHealth().then(
          (r) => ({ ok: true as const, value: r }),
          (err) => ({ ok: false as const, error: err }),
        ),
      ])
      setDestinations(data)
      setDestinationCatalog(types)
      if (healthResult.ok) {
        setHealthMap(new Map(healthResult.value.items.map((it) => [it.destination_id, it])))
      } else {
        setHealthMap(new Map()) // degrade: sem badges
      }
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : t("destinationsPage.loadError"))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (!feedback) return
    const t = setTimeout(() => setFeedback(null), 6000)
    return () => clearTimeout(t)
  }, [feedback])

  // Opções de kind derivadas dos destinos carregados
  const kindOptions = useMemo(() => {
    const kinds = Array.from(new Set(destinations.map((d) => d.kind))).sort()
    return [
      { value: "all", label: t("destinationsPage.filterKindAllOption") },
      ...kinds.map((k) => ({ value: k, label: k })),
    ]
  }, [destinations, t])

  // Filtragem local
  const filtered = useMemo(() => {
    return destinations.filter((d) => {
      if (kindFilter !== "all" && d.kind !== kindFilter) return false
      if (enabledFilter === "enabled" && !d.enabled) return false
      if (enabledFilter === "disabled" && d.enabled) return false
      if (searchQuery) {
        const q = searchQuery.toLowerCase()
        if (!d.name.toLowerCase().includes(q) && !d.id.toLowerCase().includes(q) && !d.kind.toLowerCase().includes(q)) {
          return false
        }
      }
      return true
    })
  }, [destinations, kindFilter, enabledFilter, searchQuery])

  const hasActiveFilters = kindFilter !== "all" || enabledFilter !== "all" || searchQuery !== ""

  const kpis = useMemo(() => {
    const total = destinations.length
    const ativos = destinations.filter((d) => d.enabled).length
    const comCredencial = destinations.filter((d) => d.has_secret).length
    return { total, ativos, inativos: total - ativos, comCredencial }
  }, [destinations])

  const openCreateModal = () => {
    setCreateStep("gallery")
    setCreateKind("")
    setCreateOpen(true)
  }

  const closeCreateModal = () => {
    setCreateOpen(false)
    setCreateStep("gallery")
    setCreateKind("")
  }

  const handleCreate = async (payload: DestinationCreateRequest | DestinationUpdateRequest) => {
    setSaving(true)
    try {
      await api.createDestination(payload as DestinationCreateRequest)
      closeCreateModal()
      setFeedback({ type: "success", message: t("destinationsPage.createSuccess") })
      await load()
    } finally {
      setSaving(false)
    }
  }

  const handleUpdate = async (payload: DestinationCreateRequest | DestinationUpdateRequest) => {
    if (!editing) return
    setSaving(true)
    try {
      await api.updateDestination(editing.id, payload as DestinationUpdateRequest)
      setEditing(null)
      setFeedback({ type: "success", message: t("destinationsPage.updateSuccess") })
      await load()
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async (dest: Destination) => {
    setTestingId(dest.id)
    try {
      const r = await api.testDestination(dest.id)
      const detail = r.detail ? ` — ${r.detail}` : ""
      setFeedback({
        type: r.ok ? "success" : "error",
        message: r.ok
          ? t("destinationsPage.testSuccess", { name: dest.name, detail })
          : t("destinationsPage.testFailure", { name: dest.name, detail }),
      })
    } catch (err) {
      setFeedback({ type: "error", message: err instanceof Error ? err.message : t("destinationsPage.testError") })
    } finally {
      setTestingId(null)
    }
  }

  const handleDelete = async () => {
    if (!deleting) return
    setSaving(true)
    try {
      await api.deleteDestination(deleting.id)
      setFeedback({ type: "success", message: t("destinationsPage.deleteSuccess", { name: deleting.name }) })
      setDeleting(null)
      await load()
    } catch (err) {
      setFeedback({ type: "error", message: err instanceof Error ? err.message : t("destinationsPage.deleteError") })
    } finally {
      setSaving(false)
    }
  }

  const handleReset = () => {
    setSearchInput("")
    setSearchQuery("")
    setKindFilter("all")
    setEnabledFilter("all")
  }

  return (
    <div className="space-y-6">
      <PageHeader
        icon={<SendIcon size={24} />}
        eyebrow={t("destinationsPage.eyebrow")}
        title={t("destinationsPage.title")}
        description={t("destinationsPage.description")}
        actions={
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => void load()} leftIcon={<RefreshCcwIcon size={16} />} disabled={loading}>
              {t("common:actions.refresh")}
            </Button>
            <Button onClick={openCreateModal} leftIcon={<PlusIcon size={16} />}>
              {t("destinationsPage.newDestination")}
            </Button>
          </div>
        }
      />

      {feedback && (
        <Notice
          variant={feedback.type === "success" ? "success" : "danger"}
          title={feedback.type === "success" ? t("destinationsPage.feedbackSuccessTitle") : t("destinationsPage.feedbackErrorTitle")}
        >
          {feedback.message}
        </Notice>
      )}

      {/* KPIs */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          { label: t("destinationsPage.kpiTotal"), value: kpis.total },
          { label: t("destinationsPage.kpiActive"), value: kpis.ativos },
          { label: t("destinationsPage.kpiInactive"), value: kpis.inativos },
          { label: t("destinationsPage.kpiWithCredential"), value: kpis.comCredencial },
        ].map((k) => (
          <Card key={k.label} padding="md">
            <div className="text-2xl font-semibold text-text">{k.value}</div>
            <div className="text-xs uppercase tracking-wide text-text-tertiary">{k.label}</div>
          </Card>
        ))}
      </div>

      {/* Filtros */}
      <FiltersToolbar
        search={{
          value: searchInput,
          onChange: setSearchInput,
          placeholder: t("destinationsPage.searchPlaceholder"),
          ariaLabel: t("destinationsPage.searchAriaLabel"),
          debounceMs: 300,
          onDebouncedChange: setSearchQuery,
        }}
        hasActiveFilters={hasActiveFilters}
        onReset={handleReset}
        data-testid="destinations-filters"
      >
        <Select
          label={t("destinationsPage.filterKindLabel")}
          options={kindOptions}
          value={kindFilter}
          onValueChange={(v) => setKindFilter(String(v))}
          size="sm"
          aria-label={t("destinationsPage.filterKindAriaLabel")}
          data-testid="destinations-filter-kind"
        />
        <Select
          label={t("destinationsPage.filterStatusLabel")}
          options={ENABLED_OPTIONS}
          value={enabledFilter}
          onValueChange={(v) => setEnabledFilter(String(v))}
          size="sm"
          aria-label={t("destinationsPage.filterStatusAriaLabel")}
          data-testid="destinations-filter-enabled"
        />
      </FiltersToolbar>

      {/* Conteúdo principal */}
      {loading ? (
        <div role="status" aria-label={t("destinationsPage.loadingDestinations")} className="grid gap-3">
          <SkeletonCard lines={2} />
          <SkeletonCard lines={2} />
          <SkeletonCard lines={2} />
        </div>
      ) : loadError ? (
        <ErrorState
          title={t("destinationsPage.loadErrorTitle")}
          message={loadError}
          onRetry={() => void load()}
        />
      ) : destinations.length === 0 ? (
        <EmptyState
          icon={<SendIcon size={48} />}
          title={t("destinationsPage.emptyTitle")}
          description={t("destinationsPage.emptyDescription")}
          action={
            <Button onClick={openCreateModal} leftIcon={<PlusIcon size={16} />}>
              {t("destinationsPage.newDestination")}
            </Button>
          }
        />
      ) : filtered.length === 0 ? (
        <EmptyState
          icon={<SendIcon size={48} />}
          title={t("destinationsPage.noResultsTitle")}
          description={t("destinationsPage.noResultsDescription")}
          action={
            <Button variant="outline" onClick={handleReset}>
              {t("destinationsPage.clearFilters")}
            </Button>
          }
        />
      ) : (
        <div className="grid gap-3">
          {filtered.map((dest) => (
            <Card key={dest.id} padding="md" className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0 space-y-1.5">
                <div className="flex flex-wrap items-center gap-2">
                  {/* Ícone por kind */}
                  <span className="text-text-tertiary">{kindIcon(dest.kind)}</span>
                  <h2 className="truncate text-base font-semibold text-text">{dest.name}</h2>
                  {/* Badge de saúde — degrada para nada se health falhar */}
                  <DestinationStatusBadge health={healthMap.get(dest.id)} destinationId={dest.id} />
                  <Badge variant={dest.enabled ? "success" : "default"}>
                    {dest.enabled ? t("destinationsPage.statusEnabled") : t("destinationsPage.statusInactive")}
                  </Badge>
                  <Badge variant="outline">{dest.kind}</Badge>
                  {dest.has_secret && <Badge variant="primary">{t("destinationsPage.hasCredentialBadge")}</Badge>}
                </div>
                <div className="flex flex-wrap items-center gap-3">
                  <p className="font-mono text-xs text-text-tertiary">{dest.id}</p>
                  {(() => {
                    const eps = healthMap.get(dest.id)?.eps
                    return eps !== null && eps !== undefined ? (
                      <span
                        className="text-xs text-text-secondary"
                        data-testid={`destination-eps-${dest.id}`}
                      >
                        <span className="font-medium text-text">{fmtRate(eps)}</span> {t("destinationsPage.epsSuffix")}
                      </span>
                    ) : null
                  })()}
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => navigate(`/destinations/${dest.id}`)}
                  leftIcon={<EyeIcon size={14} />}
                >
                  {t("destinationsPage.detailsAction")}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void handleTest(dest)}
                  leftIcon={<PlayIcon size={14} />}
                  loading={testingId === dest.id}
                >
                  {t("destinationsPage.testAction")}
                </Button>
                <Button variant="outline" size="sm" onClick={() => setEditing(dest)} leftIcon={<PencilIcon size={14} />}>
                  {t("common:actions.edit")}
                </Button>
                <Button variant="ghost" size="sm" onClick={() => setDeleting(dest)} leftIcon={<Trash2Icon size={14} />}>
                  {t("common:actions.delete")}
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* Modal de criação — duas etapas: galeria de tipos → formulário */}
      <Modal
        open={createOpen}
        onClose={closeCreateModal}
        title={createStep === "gallery" ? t("destinationsPage.gallerySelectTitle") : t("destinationsPage.createModalTitle")}
        size={createStep === "gallery" ? "xl" : "lg"}
      >
        {createStep === "gallery" ? (
          <div className="space-y-5" data-testid="create-step-gallery">
            <DestinationTypeGallery
              catalog={destinationCatalog}
              selectedKind={createKind}
              onSelect={(kind) => {
                setCreateKind(kind)
              }}
              disabled={saving}
            />
            <div className="flex justify-end gap-3 border-t border-border pt-4">
              <Button type="button" variant="outline" onClick={closeCreateModal} disabled={saving}>
                {t("common:actions.cancel")}
              </Button>
              <Button
                type="button"
                disabled={!createKind || saving}
                onClick={() => setCreateStep("form")}
                data-testid="gallery-next-btn"
              >
                {t("destinationsPage.continue")}
              </Button>
            </div>
          </div>
        ) : (
          <div data-testid="create-step-form">
            <DestinationForm
              mode="create"
              initialKind={createKind}
              loading={saving}
              onCancel={() => setCreateStep("gallery")}
              onSubmit={handleCreate}
            />
          </div>
        )}
      </Modal>

      <Modal open={editing !== null} onClose={() => setEditing(null)} title={t("destinationsPage.editModalTitle")} size="lg">
        {editing && (
          <DestinationForm
            mode="edit"
            destination={editing}
            loading={saving}
            onCancel={() => setEditing(null)}
            onSubmit={handleUpdate}
          />
        )}
      </Modal>

      <ConfirmDialog
        open={deleting !== null}
        title={t("destinationsPage.deleteDialogTitle")}
        description={
          deleting
            ? t("destinationsPage.deleteDialogDescription", { name: deleting.name })
            : ""
        }
        confirmLabel={t("common:actions.delete")}
        confirmVariant="danger"
        loading={saving}
        onConfirm={() => void handleDelete()}
        onClose={() => setDeleting(null)}
      />
    </div>
  )
}

export default DestinationsPage
