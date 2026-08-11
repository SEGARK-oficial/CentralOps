import type React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  SparklesIcon,
  RefreshCcwIcon,
  TableIcon,
  NetworkIcon,
  PlusIcon,
  Trash2Icon,
  KeyRoundIcon,
} from "lucide-react"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { Badge } from "@/components/ui/Badge/Badge"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { SkeletonCard } from "@/components/ui/Skeleton"
import { ErrorState } from "@/components/ui/ErrorState"
import { Notice } from "@/components/ui/Notice/Notice"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/Tabs/Tabs"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { CreateTableModal } from "@/components/enrichment/CreateTableModal"
import { TableVersionsModal } from "@/components/enrichment/TableVersionsModal"
import { CreatePolicyModal } from "@/components/enrichment/CreatePolicyModal"
import { PolicyVersionsModal } from "@/components/enrichment/PolicyVersionsModal"
import { usePlatform } from "@/contexts/PlatformContext"
import { TileGallery } from "@/components/shared/TileGallery"
import { SourceFormModal } from "@/components/enrichment/SourceFormModal"
import {
  deleteEnrichmentSource,
  deleteEnrichmentTable,
  listEnrichers,
  listEnrichmentPolicies,
  listEnrichmentSources,
  listEnrichmentTables,
  type EnricherCatalogItem as Enricher,
  type EnrichmentPolicy as EnrichPolicy,
  type EnrichmentSource as EnrichSource,
  type EnrichmentTable as EnrichTable,
} from "@/services/api"

/**
 * Enriquecimento em stream (ADR-LOCAL-0002).
 *
 * O catálogo é 100% PLUGIN-DRIVEN: tudo o que a galeria mostra vem de
 * `GET /collectors/enrichment/enrichers`, que por sua vez lê o registry do
 * backend. Adicionar uma fonte de enriquecimento não toca esta tela.
 *
 * O único device visual próprio é o selo de EGRESSO. Ele não é decoração: diz se
 * um indicador do ambiente do cliente (um IP, um hash) SAI para um terceiro. É
 * consentimento de privacidade, e por isso aparece no card, não escondido num
 * formulário de configuração.
 *
 * Escrita (criar tabela/política, publicar versão, dry-run, habilitar,
 * rollback) é feita nos modais em `components/enrichment/` — esta página só
 * lista e abre os modais certos.
 */


function fmtBytes(n: number): string {
  if (!n) return "0 B"
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`
  return `${(n / (1024 * 1024)).toFixed(1)} MiB`
}

export function EnrichmentPage(): React.ReactElement {
  const { t } = useTranslation("enrichment")
  const { organizations } = usePlatform()
  const [tab, setTab] = useState<"catalog" | "sources" | "tables" | "policies">("catalog")
  const [enrichers, setEnrichers] = useState<Enricher[]>([])
  const [tables, setTables] = useState<EnrichTable[]>([])
  const [policies, setPolicies] = useState<EnrichPolicy[]>([])
  const [sources, setSources] = useState<EnrichSource[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // ── Modais ──────────────────────────────────────────────────────────────
  const [createTableOpen, setCreateTableOpen] = useState(false)
  const [tableVersionsFor, setTableVersionsFor] = useState<EnrichTable | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<EnrichTable | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const [catalogPick, setCatalogPick] = useState<string>("")
  const [sourcePreselect, setSourcePreselect] = useState<string | null>(null)
  const [sourceFormOpen, setSourceFormOpen] = useState(false)
  const [sourceEditing, setSourceEditing] = useState<EnrichSource | null>(null)
  const [sourceDeleteTarget, setSourceDeleteTarget] = useState<EnrichSource | null>(null)
  const [sourceDeleting, setSourceDeleting] = useState(false)
  const [sourceDeleteError, setSourceDeleteError] = useState<string | null>(null)

  const [createPolicyOpen, setCreatePolicyOpen] = useState(false)
  const [policyVersionsFor, setPolicyVersionsFor] = useState<EnrichPolicy | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [e, tb, p, src] = await Promise.all([
        listEnrichers(),
        listEnrichmentTables(),
        listEnrichmentPolicies(),
        listEnrichmentSources(),
      ])
      setEnrichers(e)
      setTables(tb)
      setPolicies(p)
      setSources(src)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // Tiles do catálogo. Continua 100% plugin-driven: o que a galeria mostra vem
  // do registry do backend, então um enricher novo não toca este arquivo.
  const catalogTiles = useMemo(
    () =>
      [...enrichers]
        .sort((a, b) => a.order - b.order || a.label.localeCompare(b.label))
        .map((e) => ({
          id: e.name,
          label: e.label,
          description: e.description,
          category: e.category,
          icon: <SparklesIcon size={18} className="text-stage-enrich" aria-hidden />,
          // O selo de egresso vira badge do tile: é consentimento de privacidade
          // e precisa estar visível na ESCOLHA, não escondido depois.
          badge:
            e.egress === "third_party"
              ? t("egress.thirdParty")
              : e.egress === "internal"
                ? t("egress.internal")
                : t("egress.none"),
          badgeTone: (e.egress === "third_party"
            ? "warning"
            : e.egress === "internal"
              ? "primary"
              : "success") as "warning" | "primary" | "success",
        })),
    [enrichers, t],
  )

  const thirdPartyCount = useMemo(
    () => enrichers.filter((e) => e.egress === "third_party").length,
    [enrichers],
  )

  async function handleDeleteConfirm() {
    if (!deleteTarget) return
    setDeleting(true)
    setDeleteError(null)
    try {
      await deleteEnrichmentTable(deleteTarget.id)
      setDeleteTarget(null)
      void load()
    } catch (err) {
      // 422 "tabela em uso" chega aqui como mensagem legível — não escondemos
      // atrás de um erro genérico, é a informação que o operador precisa para
      // agir (qual política referencia a tabela).
      setDeleteError(err instanceof Error ? err.message : String(err))
    } finally {
      setDeleting(false)
    }
  }

  // Uma ação primária por aba, no header. Antes ela vivia numa segunda linha
  // abaixo das abas, e as duas linhas de botão empilhavam sem hierarquia.
  const primaryAction =
    tab === "sources"
      ? {
          label: t("sources.form.create"),
          onClick: () => {
            setSourceEditing(null)
            setSourceFormOpen(true)
          },
        }
      : tab === "tables"
        ? { label: t("tables.form.create"), onClick: () => setCreateTableOpen(true) }
        : tab === "policies"
          ? { label: t("policies.form.create"), onClick: () => setCreatePolicyOpen(true) }
          : null

  return (
    <div className="space-y-6">
      <PageHeader
        title={t("title")}
        description={t("description")}
        icon={<SparklesIcon size={20} className="text-stage-enrich" aria-hidden />}
        actions={
          <>
            {primaryAction && (
              <Button variant="primary" onClick={primaryAction.onClick}>
                <PlusIcon size={16} aria-hidden />
                {primaryAction.label}
              </Button>
            )}
            <Button variant="secondary" onClick={() => void load()} disabled={loading}>
              <RefreshCcwIcon size={16} aria-hidden />
              {t("actions.refresh")}
            </Button>
          </>
        }
      />

      {error ? (
        <ErrorState title={t("errorTitle")} message={error} onRetry={() => void load()} />
      ) : (
        <>
          <Tabs value={tab} onValueChange={(v) => setTab(v as typeof tab)}>
            <TabsList ariaLabel={t("title")}>
              <TabsTrigger value="catalog">
                {t("tabs.catalog", { count: enrichers.length })}
              </TabsTrigger>
              <TabsTrigger value="sources">
                {t("tabs.sources", { count: sources.length })}
              </TabsTrigger>
              <TabsTrigger value="tables">
                {t("tabs.tables", { count: tables.length })}
              </TabsTrigger>
              <TabsTrigger value="policies">
                {t("tabs.policies", { count: policies.length })}
              </TabsTrigger>
            </TabsList>
          </Tabs>

          {/* Escopo: o resolver do Core é FLAT (core/tenant.py), então um token
              escopado enxerga UMA organização. Sem este aviso, um MSP olha uma
              lista curta e conclui que perdeu dado. */}
          {!loading && organizations.length <= 1 && (
            <Notice variant="info" title={t("scope.title")}>
              {t("scope.body")}
            </Notice>
          )}

          {loading ? (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <SkeletonCard />
              <SkeletonCard />
              <SkeletonCard />
            </div>
          ) : tab === "catalog" ? (
            <div className="space-y-4">
              {thirdPartyCount > 0 ? (
                <Notice variant="warning" title={t("egress.warning", { count: thirdPartyCount })} />
              ) : null}

              {/* Mesma galeria das integrações e dos destinos. O catálogo é
                  escolha de fonte, não leitura: com galeria o operador busca,
                  filtra por categoria e SELECIONA, e o card selecionado abre o
                  cadastro já com o enricher preenchido. */}
              <TileGallery
                tiles={catalogTiles}
                value={catalogPick}
                onChange={(id) => {
                  setCatalogPick(id)
                  setSourceEditing(null)
                  setSourcePreselect(id)
                  setSourceFormOpen(true)
                }}
                showSearch
                searchPlaceholder={t("catalog.searchPlaceholder")}
                ariaLabel={t("tabs.catalog", { count: enrichers.length })}
                emptyLabel={t("catalog.empty")}
                columns={3}
              />
            </div>
          ) : tab === "sources" ? (
            sources.length === 0 ? (
              <EmptyState
                icon={<KeyRoundIcon size={28} aria-hidden />}
                title={t("sources.emptyTitle")}
                description={t("sources.emptyDescription")}
                action={
                  <Button
                    variant="primary"
                    onClick={() => {
                      setSourceEditing(null)
                      setSourceFormOpen(true)
                    }}
                  >
                    <PlusIcon size={14} aria-hidden />
                    {t("sources.form.create")}
                  </Button>
                }
              />
            ) : (
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {sources.map((src) => (
                  <Card
                    key={src.id}
                    className="flex cursor-pointer flex-col gap-3 p-4 transition-colors hover:border-primary-300"
                    role="button"
                    tabIndex={0}
                    onClick={() => {
                      setSourceEditing(src)
                      setSourceFormOpen(true)
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault()
                        setSourceEditing(src)
                        setSourceFormOpen(true)
                      }
                    }}
                    data-testid={`source-card-${src.name}`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <h3 className="truncate font-medium">{src.name}</h3>
                        <p className="font-mono text-xs text-muted">{src.enricher}</p>
                        <p className="text-xs text-muted">
                          {t("tables.org", { id: src.organization_id })}
                        </p>
                      </div>
                      <Button
                        variant="outline"
                        size="xs"
                        aria-label={t("sources.deleteAction")}
                        onClick={(e) => {
                          e.stopPropagation()
                          setSourceDeleteError(null)
                          setSourceDeleteTarget(src)
                        }}
                      >
                        <Trash2Icon size={12} aria-hidden />
                      </Button>
                    </div>
                    {src.description ? (
                      <p className="text-sm text-muted">{src.description}</p>
                    ) : null}
                    <div className="mt-auto flex flex-wrap items-center gap-2">
                      <Badge variant={src.secret_configured ? "success" : "warning"}>
                        {src.secret_configured
                          ? t("sources.secretConfigured")
                          : t("sources.secretMissing")}
                      </Badge>
                      {!src.enabled && (
                        <Badge variant="default">{t("sources.disabled")}</Badge>
                      )}
                    </div>
                  </Card>
                ))}
              </div>
            )
          ) : tab === "tables" ? (
            tables.length === 0 ? (
              <EmptyState
                icon={<TableIcon size={28} aria-hidden />}
                title={t("tables.emptyTitle")}
                description={t("tables.emptyDescription")}
                action={
                  <Button variant="primary" onClick={() => setCreateTableOpen(true)}>
                    <PlusIcon size={14} aria-hidden />
                    {t("tables.form.create")}
                  </Button>
                }
              />
            ) : (
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {tables.map((tb) => (
                  <Card
                    key={tb.id}
                    className="flex cursor-pointer flex-col gap-3 p-4 transition-colors hover:border-primary-300"
                    onClick={() => setTableVersionsFor(tb)}
                    data-testid={`table-card-${tb.name}`}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <h3 className="truncate font-medium">{tb.name}</h3>
                        <p className="text-xs text-muted">
                          {t("tables.org", { id: tb.organization_id })}
                        </p>
                      </div>
                      <div className="flex items-center gap-1">
                        <Badge variant="outline" className="gap-1">
                          {tb.match_mode === "cidr" ? (
                            <NetworkIcon size={12} aria-hidden />
                          ) : (
                            <TableIcon size={12} aria-hidden />
                          )}
                          {t(`tables.mode.${tb.match_mode}`)}
                        </Badge>
                        <Button
                          variant="outline"
                          size="xs"
                          aria-label={t("tables.deleteAction")}
                          onClick={(e) => {
                            e.stopPropagation()
                            setDeleteError(null)
                            setDeleteTarget(tb)
                          }}
                        >
                          <Trash2Icon size={12} aria-hidden />
                        </Button>
                      </div>
                    </div>
                    {tb.description ? (
                      <p className="text-sm text-muted">{tb.description}</p>
                    ) : null}
                    <dl className="mt-auto grid grid-cols-2 gap-2 text-sm">
                      <div>
                        <dt className="text-xs text-muted">{t("tables.entries")}</dt>
                        <dd className="font-mono">{tb.entry_count.toLocaleString()}</dd>
                      </div>
                      <div>
                        <dt className="text-xs text-muted">{t("tables.size")}</dt>
                        <dd className="font-mono">{fmtBytes(tb.approx_bytes)}</dd>
                      </div>
                    </dl>
                    {!tb.current_version_id ? (
                      /* Tabela sem versão publicada é o caso nº1 de suporte:
                         a política a referencia e a carga falha a cada ciclo. */
                      <Badge variant="warning">{t("tables.noVersion")}</Badge>
                    ) : null}
                  </Card>
                ))}
              </div>
            )
          ) : policies.length === 0 ? (
            <EmptyState
              icon={<SparklesIcon size={28} aria-hidden />}
              title={t("policies.emptyTitle")}
              description={t("policies.emptyDescription")}
              action={
                <Button variant="primary" onClick={() => setCreatePolicyOpen(true)}>
                  <PlusIcon size={14} aria-hidden />
                  {t("policies.form.create")}
                </Button>
              }
            />
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {policies.map((p) => (
                <Card
                  key={p.id}
                  className="flex cursor-pointer flex-col gap-3 p-4 transition-colors hover:border-primary-300"
                  onClick={() => setPolicyVersionsFor(p)}
                  data-testid={`policy-card-${p.name}`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <h3 className="truncate font-medium">{p.name}</h3>
                      <p className="text-xs text-muted">
                        {t("policies.rules", { count: p.rule_count })}
                      </p>
                    </div>
                    <Badge variant={p.enabled ? "success" : "default"}>
                      {p.enabled ? t("policies.enabled") : t("policies.disabled")}
                    </Badge>
                  </div>
                  {p.description ? (
                    <p className="text-sm text-muted">{p.description}</p>
                  ) : null}
                  {!p.current_version_id ? (
                    <Badge variant="warning">{t("policies.noVersion")}</Badge>
                  ) : null}
                </Card>
              ))}
            </div>
          )}
        </>
      )}

      {/* ── Modais de tabela ────────────────────────────────────────────── */}
      <CreateTableModal
        open={createTableOpen}
        onClose={() => setCreateTableOpen(false)}
        onCreated={(table) => {
          setCreateTableOpen(false)
          void load()
          setTableVersionsFor(table)
        }}
      />
      <TableVersionsModal
        open={tableVersionsFor != null}
        table={tableVersionsFor}
        onClose={() => setTableVersionsFor(null)}
        onChanged={() => void load()}
      />
      <ConfirmDialog
        open={deleteTarget != null}
        title={t("tables.deleteConfirm.title")}
        description={
          <div className="space-y-2">
            <p>{t("tables.deleteConfirm.description", { name: deleteTarget?.name ?? "" })}</p>
            {deleteError && <Notice variant="danger" title={deleteError} />}
          </div>
        }
        confirmVariant="danger"
        confirmLabel={t("common:actions.delete")}
        loading={deleting}
        onConfirm={handleDeleteConfirm}
        onClose={() => {
          setDeleteTarget(null)
          setDeleteError(null)
        }}
      />

      {/* ── Modais de fonte configurada ─────────────────────────────────── */}
      <SourceFormModal
        open={sourceFormOpen}
        source={sourceEditing}
        preselectEnricher={sourcePreselect}
        enrichers={enrichers}
        organizations={organizations}
        onClose={() => {
          setSourceFormOpen(false)
          setSourceEditing(null)
          setSourcePreselect(null)
          setCatalogPick("")
        }}
        onSaved={() => {
          setSourceFormOpen(false)
          setSourceEditing(null)
          setSourcePreselect(null)
          setCatalogPick("")
          void load()
        }}
      />
      <ConfirmDialog
        open={sourceDeleteTarget != null}
        title={t("sources.deleteConfirm.title")}
        description={
          <div className="space-y-2">
            <p>
              {t("sources.deleteConfirm.description", {
                name: sourceDeleteTarget?.name ?? "",
              })}
            </p>
            {sourceDeleteError && <Notice variant="danger" title={sourceDeleteError} />}
          </div>
        }
        confirmVariant="danger"
        confirmLabel={t("common:actions.delete")}
        loading={sourceDeleting}
        onConfirm={async () => {
          if (!sourceDeleteTarget) return
          setSourceDeleting(true)
          setSourceDeleteError(null)
          try {
            await deleteEnrichmentSource(sourceDeleteTarget.id)
            setSourceDeleteTarget(null)
            void load()
          } catch (err) {
            setSourceDeleteError(err instanceof Error ? err.message : String(err))
          } finally {
            setSourceDeleting(false)
          }
        }}
        onClose={() => {
          setSourceDeleteTarget(null)
          setSourceDeleteError(null)
        }}
      />

      {/* ── Modais de política ──────────────────────────────────────────── */}
      <CreatePolicyModal
        open={createPolicyOpen}
        onClose={() => setCreatePolicyOpen(false)}
        onCreated={(policy) => {
          setCreatePolicyOpen(false)
          void load()
          setPolicyVersionsFor(policy)
        }}
      />
      <PolicyVersionsModal
        open={policyVersionsFor != null}
        policy={policyVersionsFor}
        enrichers={enrichers}
        tables={tables}
        sources={sources}
        onClose={() => setPolicyVersionsFor(null)}
        onChanged={() => {
          void load()
          // Reflete enabled/current_version_id atualizados no modal aberto,
          // sem esperar o usuário fechar e reabrir.
          if (policyVersionsFor) {
            listEnrichmentPolicies().then((all) => {
              const fresh = all.find((x) => x.id === policyVersionsFor.id)
              if (fresh) setPolicyVersionsFor(fresh)
            })
          }
        }}
      />
    </div>
  )
}

export default EnrichmentPage
