import type React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  SparklesIcon,
  RefreshCcwIcon,
  TableIcon,
  NetworkIcon,
  ShieldAlertIcon,
  GlobeIcon,
  LockIcon,
  PlusIcon,
  Trash2Icon,
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
import {
  deleteEnrichmentTable,
  listEnrichers,
  listEnrichmentPolicies,
  listEnrichmentTables,
  type EnricherCatalogItem as Enricher,
  type EnrichmentPolicy as EnrichPolicy,
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

type Egress = "none" | "internal" | "third_party"

/** Selo de egresso. Cores seguem a semântica já usada no produto para risco. */
function EgressBadge({ egress }: { egress: Egress }): React.ReactElement {
  const { t } = useTranslation("enrichment")
  if (egress === "third_party") {
    return (
      <Badge variant="danger" className="gap-1">
        <GlobeIcon size={12} aria-hidden />
        {t("egress.thirdParty")}
      </Badge>
    )
  }
  if (egress === "internal") {
    return (
      <Badge variant="primary" className="gap-1">
        <LockIcon size={12} aria-hidden />
        {t("egress.internal")}
      </Badge>
    )
  }
  return (
    <Badge variant="success" className="gap-1">
      <LockIcon size={12} aria-hidden />
      {t("egress.none")}
    </Badge>
  )
}

function fmtBytes(n: number): string {
  if (!n) return "0 B"
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`
  return `${(n / (1024 * 1024)).toFixed(1)} MiB`
}

export function EnrichmentPage(): React.ReactElement {
  const { t } = useTranslation("enrichment")
  const [tab, setTab] = useState<"catalog" | "tables" | "policies">("catalog")
  const [enrichers, setEnrichers] = useState<Enricher[]>([])
  const [tables, setTables] = useState<EnrichTable[]>([])
  const [policies, setPolicies] = useState<EnrichPolicy[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // ── Modais ──────────────────────────────────────────────────────────────
  const [createTableOpen, setCreateTableOpen] = useState(false)
  const [tableVersionsFor, setTableVersionsFor] = useState<EnrichTable | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<EnrichTable | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const [createPolicyOpen, setCreatePolicyOpen] = useState(false)
  const [policyVersionsFor, setPolicyVersionsFor] = useState<EnrichPolicy | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [e, tb, p] = await Promise.all([
        listEnrichers(),
        listEnrichmentTables(),
        listEnrichmentPolicies(),
      ])
      setEnrichers(e)
      setTables(tb)
      setPolicies(p)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  /** Agrupa por categoria preservando a ordem que o backend definiu. */
  const byCategory = useMemo(() => {
    const groups = new Map<string, Enricher[]>()
    for (const e of [...enrichers].sort((a, b) => a.order - b.order || a.label.localeCompare(b.label))) {
      const list = groups.get(e.category) ?? []
      list.push(e)
      groups.set(e.category, list)
    }
    return [...groups.entries()]
  }, [enrichers])

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

  return (
    <div className="space-y-6">
      <PageHeader
        title={t("title")}
        description={t("description")}
        icon={<SparklesIcon size={20} className="text-stage-enrich" aria-hidden />}
        actions={
          <Button variant="secondary" onClick={() => void load()} disabled={loading}>
            <RefreshCcwIcon size={16} aria-hidden />
            {t("actions.refresh")}
          </Button>
        }
      />

      {error ? (
        <ErrorState title={t("errorTitle")} message={error} onRetry={() => void load()} />
      ) : (
        <>
          <div className="flex items-center justify-between gap-3">
            <Tabs value={tab} onValueChange={(v) => setTab(v as typeof tab)}>
              <TabsList ariaLabel={t("title")}>
                <TabsTrigger value="catalog">
                  {t("tabs.catalog", { count: enrichers.length })}
                </TabsTrigger>
                <TabsTrigger value="tables">
                  {t("tabs.tables", { count: tables.length })}
                </TabsTrigger>
                <TabsTrigger value="policies">
                  {t("tabs.policies", { count: policies.length })}
                </TabsTrigger>
              </TabsList>
            </Tabs>

            {tab === "tables" && (
              <Button variant="primary" size="sm" onClick={() => setCreateTableOpen(true)}>
                <PlusIcon size={14} aria-hidden />
                {t("tables.form.create")}
              </Button>
            )}
            {tab === "policies" && (
              <Button variant="primary" size="sm" onClick={() => setCreatePolicyOpen(true)}>
                <PlusIcon size={14} aria-hidden />
                {t("policies.form.create")}
              </Button>
            )}
          </div>

          {loading ? (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <SkeletonCard />
              <SkeletonCard />
              <SkeletonCard />
            </div>
          ) : tab === "catalog" ? (
            <div className="space-y-6">
              {thirdPartyCount > 0 ? (
                <p className="flex items-start gap-2 text-sm text-muted">
                  <ShieldAlertIcon size={16} className="mt-0.5 shrink-0" aria-hidden />
                  {t("egress.warning", { count: thirdPartyCount })}
                </p>
              ) : null}

              {byCategory.map(([category, items]) => (
                <section key={category} className="space-y-3">
                  <h2 className="text-sm font-medium text-muted">{category}</h2>
                  <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                    {items.map((e) => (
                      <Card key={e.name} className="flex flex-col gap-3 p-4">
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0">
                            <h3 className="truncate font-medium">{e.label}</h3>
                            <p className="font-mono text-xs text-muted">{e.name}</p>
                          </div>
                          <Badge variant={e.tier === "stable" ? "success" : "default"}>
                            {e.tier}
                          </Badge>
                        </div>

                        <p className="text-sm text-muted">{e.description}</p>

                        <div className="mt-auto flex flex-wrap items-center gap-2">
                          <EgressBadge egress={e.egress} />
                          {/* local = roda POR EVENTO e alimenta a detecção em voo;
                              remote = resolve no fim do lote. A diferença muda o
                              que o operador pode fazer com o campo. */}
                          <Badge variant={e.mode === "local" ? "primary" : "warning"}>
                            {t(`mode.${e.mode}`)}
                          </Badge>
                          {e.key_kinds.map((k) => (
                            <Badge key={k} variant="outline" className="font-mono text-[11px]">
                              {k}
                            </Badge>
                          ))}
                        </div>
                      </Card>
                    ))}
                  </div>
                </section>
              ))}
            </div>
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
