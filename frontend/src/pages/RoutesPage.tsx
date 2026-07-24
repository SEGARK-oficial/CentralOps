/**
 * RoutesPage — lista drag-reorder de rotas com auditoria + rollback.
 *
 * Funcionalidades:
 *   1. Drag-reorder: @dnd-kit (DndContext + SortableContext).
 *      PointerSensor + KeyboardSensor para a11y.
 *      Grip handle visível; optimistic update com rollback em erro.
 *   2. Auditoria + Rollback: painel na aba "Auditoria" do modal de atividade.
 *      Conecta routeAudit/rollbackRoute (antes código morto).
 *   3. Catch-all: badge "catch-all" em rota com condição {} + is_final.
 *      Aviso se não houver catch-all final habilitada.
 *   4. Skeleton + ErrorState no carregamento.
 *   5. BulkActionBar: seleção múltipla com ações Ativar/Desativar em massa.
 *      A rota de sistema (wazuh-default-catchall) é protegida: não-selecionável,
 *      não-arrastável e sem botão de exclusão.
 */

import type React from "react"
import { useCallback, useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  DndContext,
  closestCenter,
  PointerSensor,
  KeyboardSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core"
import {
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
  arrayMove,
} from "@dnd-kit/sortable"
import { CSS } from "@dnd-kit/utilities"
import {
  GitBranchIcon,
  PlusIcon,
  RefreshCcwIcon,
  PencilIcon,
  Trash2Icon,
  AlertTriangleIcon,
  FlaskConicalIcon,
  ActivityIcon,
  GripVerticalIcon,
  ShieldIcon,
  PowerIcon,
  PowerOffIcon,
  NetworkIcon,
} from "lucide-react"
import { useNavigate } from "react-router-dom"
import * as api from "@/services/api"
import { Sparkline } from "@/components/observability/Sparkline"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { Badge } from "@/components/ui/Badge/Badge"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { Input } from "@/components/ui/Input/Input"
import { SkeletonCard } from "@/components/ui/Skeleton"
import { ErrorState } from "@/components/ui/ErrorState"
import { Tabs, TabsList, TabsTrigger, TabsPanel } from "@/components/ui/Tabs/Tabs"
import { Checkbox } from "@/components/ui/Checkbox/Checkbox"
import { BulkActionBar } from "@/components/ui/BulkActionBar/BulkActionBar"
import { RouteForm } from "@/components/routes/RouteForm"
import { RouteAuditPanel } from "@/components/routes/RouteAuditPanel"
import { useBulkSelection } from "@/hooks/useBulkSelection"
import type { Route, RouteCreateRequest, RouteDryRunResponse } from "@/types"

type Feedback = { type: "success" | "error"; message: string }

/** ID da rota de sistema protegida — não pode ser excluída, reordenada ou selecionada em bulk. */
export const SYSTEM_ROUTE_ID = "wazuh-default-catchall"

/** Verifica se a rota é protegida (sistema). */
export function isSystemRoute(r: Route): boolean {
  return r.id === SYSTEM_ROUTE_ID
}

/** Verifica se uma rota é catch-all: condição vazia ({}) e is_final. */
function isCatchAll(r: Route): boolean {
  return r.is_final && r.enabled && Object.keys(r.condition).length === 0
}

// ── SortableRouteCard ─────────────────────────────────────────────────────

interface SortableRouteCardProps {
  route: Route
  selected: boolean
  onToggleSelect: (id: string) => void
  onEdit: (r: Route) => void
  onDelete: (r: Route) => void
  onActivity: (r: Route) => void
}

const SortableRouteCard: React.FC<SortableRouteCardProps> = ({
  route: r,
  selected,
  onToggleSelect,
  onEdit,
  onDelete,
  onActivity,
}) => {
  const { t } = useTranslation("routing")
  const system = isSystemRoute(r)

  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: r.id, disabled: system })

  const style: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
    zIndex: isDragging ? 10 : undefined,
  }

  const catchAll = isCatchAll(r)

  return (
    <div ref={setNodeRef} style={style} data-testid={`route-card-${r.id}`}>
      <Card
        padding="md"
        className={`flex flex-wrap items-start justify-between gap-3 ${r.unreachable ? "border-warning-500/40" : ""} ${system ? "border-primary-200 bg-surface-tertiary/30" : ""}`}
      >
        <div className="flex min-w-0 flex-1 items-start gap-3">
          {/* Checkbox de seleção em massa — oculto para rota de sistema */}
          {system ? (
            <span
              className="mt-0.5 flex h-5 w-5 items-center justify-center"
              aria-hidden="true"
              title={t("routesPage.systemRouteTitle")}
            >
              <ShieldIcon size={14} className="text-text-tertiary" />
            </span>
          ) : (
            <span className="mt-0.5">
              <Checkbox
                size="sm"
                checked={selected}
                onChange={() => onToggleSelect(r.id)}
                aria-label={t("routesPage.selectRouteAria", { name: r.name })}
                data-testid={`route-select-${r.id}`}
              />
            </span>
          )}

          {/* Grip handle — desabilitado para rotas de sistema */}
          <button
            type="button"
            className={`mt-0.5 touch-none rounded p-1 text-text-tertiary focus-ring ${system ? "cursor-not-allowed opacity-40" : "cursor-grab hover:text-text-secondary active:cursor-grabbing"}`}
            aria-label={
              system
                ? t("routesPage.systemRouteNotReorderable")
                : t("routesPage.reorderRouteAria", { name: r.name })
            }
            disabled={system}
            {...(system ? {} : { ...attributes, ...listeners })}
          >
            <GripVerticalIcon size={16} aria-hidden="true" />
          </button>

          <div className="min-w-0 space-y-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded bg-surface-tertiary px-1.5 py-0.5 font-mono text-xs text-text-secondary">
                #{r.priority}
              </span>
              <h2 className="truncate text-base font-semibold text-text">{r.name}</h2>
              {system && (
                <Badge variant="outline">
                  <ShieldIcon size={10} className="mr-1 inline" aria-hidden="true" />
                  {t("routesPage.systemBadge")}
                </Badge>
              )}
              <Badge variant={r.action === "drop" ? "danger" : "primary"}>{r.action}</Badge>
              {r.is_final ? <Badge variant="outline">{t("routesPage.finalBadge")}</Badge> : <Badge variant="warning">{t("routesPage.cloneBadge")}</Badge>}
              {catchAll && (
                <Badge variant="success" dot>
                  {t("routesPage.defaultBadge")}
                </Badge>
              )}
              {r.canary_percent < 100 && <Badge variant="primary">{t("routesPage.gradualBadge", { percent: r.canary_percent })}</Badge>}
              {!r.enabled && <Badge variant="default">{t("routesPage.disabledBadge")}</Badge>}
              {r.unreachable && <Badge variant="warning" dot>{t("routesPage.unreachableBadge")}</Badge>}
            </div>
            <code className="block truncate font-mono text-xs text-text-tertiary">
              {JSON.stringify(r.condition)}
            </code>
            {r.action === "route" && (
              <div className="text-xs text-text-secondary">
                {t("routesPage.destinationsPrefix")} {r.destination_ids.join(", ") || t("routesPage.noDestinations")}
              </div>
            )}
            {system && (
              <p className="text-xs text-text-tertiary">
                {t("routesPage.systemRouteDescription")}
              </p>
            )}
          </div>
        </div>

        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => onActivity(r)} leftIcon={<ActivityIcon size={14} />}>
            {t("routesPage.activity")}
          </Button>
          <Button variant="outline" size="sm" onClick={() => onEdit(r)} leftIcon={<PencilIcon size={14} />}>
            {t("common:actions.edit")}
          </Button>
          {system ? (
            <Button
              variant="ghost"
              size="sm"
              disabled
              leftIcon={<Trash2Icon size={14} />}
              title={t("routesPage.systemRouteTitle")}
              aria-label={t("routesPage.deleteDisabledAria")}
              data-testid={`route-delete-${r.id}`}
            >
              {t("common:actions.delete")}
            </Button>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => onDelete(r)}
              leftIcon={<Trash2Icon size={14} />}
              data-testid={`route-delete-${r.id}`}
            >
              {t("common:actions.delete")}
            </Button>
          )}
        </div>
      </Card>
    </div>
  )
}

// ── RoutesPage ────────────────────────────────────────────────────────────

const RoutesPage: React.FC = () => {
  const { t } = useTranslation("routing")
  const navigate = useNavigate()
  const [routes, setRoutes] = useState<Route[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [feedback, setFeedback] = useState<Feedback | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [editing, setEditing] = useState<Route | null>(null)
  const [deleting, setDeleting] = useState<Route | null>(null)
  const [activityFor, setActivityFor] = useState<Route | null>(null)
  const [dryRunOpen, setDryRunOpen] = useState(false)

  // ── Bulk selection (exclui rota de sistema) ──────────────────────────
  const bulk = useBulkSelection<Route>({
    visibleItems: routes,
    getId: (r) => r.id,
    isSelectable: (r) => !isSystemRoute(r),
  })

  const [bulkPending, setBulkPending] = useState<"enable" | "disable" | null>(null)
  const [bulkLoading, setBulkLoading] = useState(false)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setLoadError(null)
      // Topologia carrega em paralelo e degrada sem quebrar a lista de rotas.
      const routesData = await api.listRoutes()
      setRoutes(routesData)
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : t("routesPage.loadError"))
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

  const hasUnreachable = routes.some((r) => r.unreachable)
  const hasCatchAll = routes.some((r) => isCatchAll(r))

  // ── Drag-and-drop reorder ────────────────────────────────────────────

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )

  const handleDragEnd = useCallback(
    async (event: DragEndEvent) => {
      const { active, over } = event
      if (!over || active.id === over.id) return

      // Bloqueia drag se qualquer extremo for a rota de sistema
      if (active.id === SYSTEM_ROUTE_ID || over.id === SYSTEM_ROUTE_ID) return

      const oldIndex = routes.findIndex((r) => r.id === active.id)
      const newIndex = routes.findIndex((r) => r.id === over.id)
      if (oldIndex === -1 || newIndex === -1) return

      // Optimistic update
      const reordered = arrayMove(routes, oldIndex, newIndex)
      setRoutes(reordered)

      try {
        await api.reorderRoutes(reordered.map((r) => r.id))
        setFeedback({ type: "success", message: t("routesPage.reorderSuccess") })
        // Recarrega para obter prioridades recalculadas pelo backend
        await load()
      } catch (err) {
        // Rollback
        setRoutes(routes)
        setFeedback({
          type: "error",
          message: err instanceof Error ? err.message : t("routesPage.reorderError"),
        })
      }
    },
    [routes, load, t],
  )

  // ── CRUD ────────────────────────────────────────────────────────────

  const handleCreate = async (payload: RouteCreateRequest) => {
    setSaving(true)
    try {
      await api.createRoute(payload)
      setCreateOpen(false)
      setFeedback({ type: "success", message: t("routesPage.createSuccess") })
      await load()
    } finally {
      setSaving(false)
    }
  }

  const handleUpdate = async (payload: RouteCreateRequest) => {
    if (!editing) return
    setSaving(true)
    try {
      await api.updateRoute(editing.id, payload)
      setEditing(null)
      setFeedback({ type: "success", message: t("routesPage.updateSuccess") })
      await load()
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async () => {
    if (!deleting) return
    setSaving(true)
    try {
      await api.deleteRoute(deleting.id)
      setFeedback({ type: "success", message: t("routesPage.deleteSuccess", { name: deleting.name }) })
      setDeleting(null)
      await load()
    } catch (err) {
      setFeedback({ type: "error", message: err instanceof Error ? err.message : t("routesPage.deleteError") })
    } finally {
      setSaving(false)
    }
  }

  // ── Bulk enable/disable ──────────────────────────────────────────────

  const handleBulkToggle = async (enable: boolean) => {
    const ids = Array.from(bulk.selected)
    if (ids.length === 0) return

    setBulkLoading(true)
    const errors: string[] = []
    let done = 0

    for (const id of ids) {
      try {
        await api.updateRoute(id, { enabled: enable })
        done++
      } catch (err) {
        errors.push(id)
      }
    }

    setBulkLoading(false)
    setBulkPending(null)
    bulk.clearSelection()

    if (errors.length === 0) {
      setFeedback({
        type: "success",
        message: enable
          ? t("routesPage.bulkToggleSuccessEnabled", { count: done })
          : t("routesPage.bulkToggleSuccessDisabled", { count: done }),
      })
    } else {
      setFeedback({
        type: "error",
        message: t("routesPage.bulkToggleErrorMixed", { done, errors: errors.length }),
      })
    }
    await load()
  }

  // ── Render ───────────────────────────────────────────────────────────

  return (
    <div className="space-y-6">
      <PageHeader
        icon={<GitBranchIcon size={24} />}
        eyebrow={t("routesPage.eyebrow")}
        title={t("routesPage.title")}
        description={t("routesPage.description")}
        actions={
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => void load()} leftIcon={<RefreshCcwIcon size={16} />} disabled={loading}>
              {t("common:actions.refresh")}
            </Button>
            <Button variant="outline" onClick={() => setDryRunOpen(true)} leftIcon={<FlaskConicalIcon size={16} />}>
              {t("routesPage.simulate")}
            </Button>
            <Button onClick={() => setCreateOpen(true)} leftIcon={<PlusIcon size={16} />}>
              {t("routesPage.newRoute")}
            </Button>
          </div>
        }
      />

      {feedback && (
        <Notice
          variant={feedback.type === "success" ? "success" : "danger"}
          title={feedback.type === "success" ? t("routesPage.feedbackOkTitle") : t("routesPage.feedbackErrorTitle")}
        >
          {feedback.message}
        </Notice>
      )}

      {hasUnreachable && (
        <Notice variant="warning" title={t("routesPage.unreachableTitle")} icon={<AlertTriangleIcon size={18} />}>
          {t("routesPage.unreachableDescription")}
        </Notice>
      )}

      {!loading && !loadError && routes.length > 0 && !hasCatchAll && (
        <Notice variant="warning" title={t("routesPage.noCatchAllTitle")} icon={<ShieldIcon size={18} />}>
          {t("routesPage.noCatchAllDescription")}
        </Notice>
      )}

      {/* BulkActionBar — aparece quando há seleção */}
      <BulkActionBar
        count={bulk.selected.size}
        onClear={bulk.clearSelection}
        contextLabel={t("routesPage.bulkContextLabel")}
        data-testid="routes-bulk-action-bar"
      >
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setBulkPending("enable")}
          leftIcon={<PowerIcon size={14} />}
          data-testid="routes-bulk-enable-btn"
        >
          {t("routesPage.bulkEnable")}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setBulkPending("disable")}
          leftIcon={<PowerOffIcon size={14} />}
          data-testid="routes-bulk-disable-btn"
        >
          {t("routesPage.bulkDisable")}
        </Button>
      </BulkActionBar>

      {/* Estado de carregamento */}
      {loading && (
        <div role="status" aria-label={t("routesPage.loadingRoutes")} className="space-y-2">
          {[1, 2, 3].map((i) => (
            <SkeletonCard key={i} lines={2} />
          ))}
        </div>
      )}

      {/* Estado de erro */}
      {!loading && loadError && (
        <ErrorState
          title={t("routesPage.loadErrorTitle")}
          message={loadError}
          onRetry={() => void load()}
        />
      )}

      {/* Lista vazia */}
      {!loading && !loadError && routes.length === 0 && (
        <EmptyState
          icon={<GitBranchIcon size={48} />}
          title={t("routesPage.emptyTitle")}
          description={t("routesPage.emptyDescription")}
          action={<Button onClick={() => setCreateOpen(true)} leftIcon={<PlusIcon size={16} />}>{t("routesPage.newRoute")}</Button>}
        />
      )}

      {/* Lista drag-reorder */}
      {!loading && !loadError && routes.length > 0 && (
        <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={(e) => void handleDragEnd(e)}>
          <SortableContext items={routes.map((r) => r.id)} strategy={verticalListSortingStrategy}>
            <div className="grid gap-2">
              {routes.map((r) => (
                <SortableRouteCard
                  key={r.id}
                  route={r}
                  selected={bulk.isSelected(r.id)}
                  onToggleSelect={bulk.toggleOne}
                  onEdit={setEditing}
                  onDelete={setDeleting}
                  onActivity={setActivityFor}
                />
              ))}
            </div>
          </SortableContext>
        </DndContext>
      )}

      {!loading && !loadError && routes.length > 0 && (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-border bg-surface-secondary px-4 py-3 text-sm">
          <span className="text-text-secondary">
            {t("routesPage.flowBannerText")}
          </span>
          <Button variant="outline" size="sm" onClick={() => navigate("/flow")} leftIcon={<NetworkIcon size={14} />}>
            {t("routesPage.viewFlow")}
          </Button>
        </div>
      )}

      {/* Modal de atividade + auditoria */}
      <RouteActivityModal
        route={activityFor}
        onClose={() => setActivityFor(null)}
        onRolledBack={() => void load()}
      />

      <Modal open={createOpen} onClose={() => setCreateOpen(false)} title={t("routesPage.createModalTitle")} size="lg">
        <RouteForm mode="create" loading={saving} onCancel={() => setCreateOpen(false)} onSubmit={handleCreate} />
      </Modal>

      <Modal open={editing !== null} onClose={() => setEditing(null)} title={t("routesPage.editModalTitle")} size="lg">
        {editing && (
          <RouteForm
            mode="edit"
            route={editing}
            loading={saving}
            onCancel={() => setEditing(null)}
            onSubmit={handleUpdate}
          />
        )}
      </Modal>

      <ConfirmDialog
        open={deleting !== null}
        title={t("routesPage.deleteDialogTitle")}
        description={deleting ? t("routesPage.deleteDialogDescription", { name: deleting.name }) : ""}
        confirmLabel={t("common:actions.delete")}
        confirmVariant="danger"
        loading={saving}
        onConfirm={() => void handleDelete()}
        onClose={() => setDeleting(null)}
      />

      {/* Dialog de confirmação bulk ativar/desativar */}
      <ConfirmDialog
        open={bulkPending !== null}
        title={
          bulkPending === "enable"
            ? t("routesPage.bulkEnableDialogTitle", { count: bulk.selected.size })
            : t("routesPage.bulkDisableDialogTitle", { count: bulk.selected.size })
        }
        description={
          bulkPending === "enable"
            ? t("routesPage.bulkEnableDialogDescription")
            : t("routesPage.bulkDisableDialogDescription")
        }
        confirmLabel={bulkPending === "enable" ? t("routesPage.bulkEnable") : t("routesPage.bulkDisable")}
        confirmVariant="primary"
        loading={bulkLoading}
        onConfirm={() => void handleBulkToggle(bulkPending === "enable")}
        onClose={() => {
          if (!bulkLoading) setBulkPending(null)
        }}
        data-testid={`routes-bulk-${bulkPending ?? "confirm"}-dialog`}
      />

      <DryRunModal open={dryRunOpen} onClose={() => setDryRunOpen(false)} />
    </div>
  )
}

// ── Per-route activity + audit tabs ──────────────────────────────────────

interface RouteActivityModalProps {
  route: Route | null
  onClose: () => void
  onRolledBack?: () => void
}

const RouteActivityModal: React.FC<RouteActivityModalProps> = ({ route, onClose, onRolledBack }) => {
  const { t } = useTranslation("routing")
  const [metrics, setMetrics] = useState<import("@/types").RouteMetrics | null>(null)
  const [metricsLoading, setMetricsLoading] = useState(false)
  const [activeTab, setActiveTab] = useState<"metrics" | "audit">("metrics")

  useEffect(() => {
    if (!route) {
      setMetrics(null)
      setActiveTab("metrics")
      return
    }
    setMetricsLoading(true)
    api
      .getRouteMetrics(route.id, { range_minutes: 60 })
      .then(setMetrics)
      .catch(() => setMetrics(null))
      .finally(() => setMetricsLoading(false))
  }, [route])

  const has = metrics && (metrics.series.matched?.length || metrics.series.route?.length || metrics.series.drop?.length)

  return (
    <Modal open={route !== null} onClose={onClose} title={route ? `${route.name}` : ""} size="xl">
      {route && (
        <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as "metrics" | "audit")}>
          <TabsList>
            <TabsTrigger value="metrics" icon={<ActivityIcon size={15} />}>
              {t("activityModal.metricsTab")}
            </TabsTrigger>
            <TabsTrigger value="audit" icon={<GitBranchIcon size={15} />}>
              {t("activityModal.auditTab")}
            </TabsTrigger>
          </TabsList>

          <TabsPanel value="metrics">
            {metricsLoading ? (
              <p className="text-sm text-text-tertiary">{t("activityModal.loading")}</p>
            ) : has ? (
              <div className="flex flex-wrap gap-6">
                <Sparkline points={metrics!.series.matched ?? []} label={t("activityModal.matchedPerMin")} />
                <Sparkline points={metrics!.series.route ?? []} label={t("activityModal.routedPerMin")} />
                <Sparkline points={metrics!.series.drop ?? []} label={t("activityModal.droppedPerMin")} />
              </div>
            ) : (
              <p className="text-sm text-text-tertiary">{t("activityModal.noActivity")}</p>
            )}
          </TabsPanel>

          <TabsPanel value="audit">
            <RouteAuditPanel
              routeId={route.id}
              routeName={route.name}
              onRolledBack={onRolledBack}
            />
          </TabsPanel>
        </Tabs>
      )}
    </Modal>
  )
}

// ── Dry-run simulator ─────────────────────────────────────────────────────

const DryRunModal: React.FC<{ open: boolean; onClose: () => void }> = ({ open, onClose }) => {
  const { t } = useTranslation("routing")
  const [severity, setSeverity] = useState("")
  const [vendor, setVendor] = useState("")
  const [result, setResult] = useState<RouteDryRunResponse | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async (useAudit: boolean) => {
    setRunning(true)
    setError(null)
    setResult(null)
    try {
      const samples = useAudit
        ? null
        : [
            {
              _centralops: {
                ...(severity.trim() !== "" ? { severity_id: Number(severity) } : {}),
                ...(vendor.trim() !== "" ? { vendor: vendor.trim() } : {}),
                event_id: "dry-run",
              },
            },
          ]
      setResult(await api.dryRunRoutes({ routes: null, samples }))
    } catch (err) {
      setError(err instanceof Error ? err.message : t("dryRun.runError"))
    } finally {
      setRunning(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose} title={t("dryRun.title")} size="lg">
      <div className="space-y-4">
        <p className="text-sm text-text-secondary">
          {t("dryRun.description")}
        </p>
        <div className="grid grid-cols-2 gap-3">
          <Input label={t("dryRun.severityLabel")} type="number" value={severity} onChange={(e) => setSeverity(e.target.value)} placeholder={t("dryRun.severityPlaceholder")} />
          <Input label={t("dryRun.vendorLabel")} value={vendor} onChange={(e) => setVendor(e.target.value)} placeholder={t("dryRun.vendorPlaceholder")} />
        </div>
        <div className="flex gap-2">
          <Button onClick={() => void run(false)} loading={running} leftIcon={<FlaskConicalIcon size={16} />}>
            {t("dryRun.testEvent")}
          </Button>
          <Button variant="outline" onClick={() => void run(true)} loading={running}>
            {t("dryRun.useRecentEvents")}
          </Button>
        </div>

        {error && <Notice variant="danger" title={t("dryRun.error")}>{error}</Notice>}

        {result && (
          <Card padding="md" className="space-y-3">
            <div className="flex flex-wrap gap-4 text-sm">
              <span>
                {t("dryRun.evaluated")} <b>{result.evaluated}</b>{" "}
                <span className="text-text-tertiary">({result.sample_source})</span>
              </span>
              <span className="text-success-700">{t("dryRun.routed")} <b>{result.routed}</b></span>
              <span className="text-danger-700">{t("dryRun.dropped")} <b>{result.dropped}</b></span>
              <span className="text-warning-700">{t("dryRun.fallback")} <b>{result.fallback}</b></span>
            </div>
            {Object.keys(result.per_destination).length > 0 && (
              <div className="text-sm">
                <div className="mb-1 text-xs uppercase tracking-wide text-text-tertiary">{t("dryRun.perDestination")}</div>
                <div className="flex flex-wrap gap-2">
                  {Object.entries(result.per_destination).map(([d, n]) => (
                    <Badge key={d} variant="outline">{d}: {n}</Badge>
                  ))}
                </div>
              </div>
            )}
            {result.unreachable_route_ids.length > 0 && (
              <Notice variant="warning" title={t("dryRun.unreachableRoutesTitle")}>
                {result.unreachable_route_ids.join(", ")}
              </Notice>
            )}
            {result.evaluated === 0 && (
              <p className="text-sm text-text-tertiary">
                {t("dryRun.noSamplesEvaluated")}
              </p>
            )}
          </Card>
        )}
      </div>
    </Modal>
  )
}

export default RoutesPage
