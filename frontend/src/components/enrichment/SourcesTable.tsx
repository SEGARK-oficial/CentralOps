import type React from "react"
import { useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { PlusIcon, Trash2Icon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { DataTable } from "@/components/ui/DataTable/DataTable"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { Input } from "@/components/ui/Input/Input"
import { Select } from "@/components/ui/Select/Select"
import type { TableColumn } from "@/types"
import type { EnricherCatalogItem, EnrichmentSource } from "@/services/api"

/**
 * Fontes configuradas como lista OPERACIONAL, não como galeria.
 *
 * Cards são bons para escolher entre coisas novas; péssimos para comparar o
 * estado de coisas que já existem. Com oito fontes cadastradas, descobrir qual
 * está com a credencial rejeitada exigia abrir uma a uma, porque o card só
 * mostrava se havia credencial — não se ela FUNCIONA.
 *
 * As colunas são exatamente as que decidem uma ação:
 *
 * - **Egresso**, porque é consentimento de privacidade e precisa estar visível
 *   sem abrir nada.
 * - **Último teste**, que distingue três situações que o card fundia numa só:
 *   nunca testada, testada e ok, testada e falhando — com a mensagem do
 *   provedor, que é o que permite agir sem abrir log de worker.
 * - **Quem usa**, porque uma fonte compartilhada com filhas não pode ser
 *   apagada nem ter a credencial trocada sem saber quem depende dela.
 */

interface Props {
  sources: EnrichmentSource[]
  enrichers: EnricherCatalogItem[]
  organizations?: Array<{ id: number; name: string }>
  onCreate: () => void
  onEdit: (source: EnrichmentSource) => void
  onDelete: (source: EnrichmentSource) => void
  onTest: (source: EnrichmentSource) => void
  /** Id da fonte sendo sondada agora, para o estado de carregamento do botão. */
  testingId?: string | null
}

type Filter = "all" | "problem" | "third_party"

function ago(iso: string, t: (k: string, o?: object) => string): string {
  const secs = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000))
  if (secs < 60) return t("execution.agoSeconds", { count: secs })
  if (secs < 3600) return t("execution.agoMinutes", { count: Math.floor(secs / 60) })
  if (secs < 86400) return t("execution.agoHours", { count: Math.floor(secs / 3600) })
  return t("sources.table.agoDays", { count: Math.floor(secs / 86400) })
}

export const SourcesTable: React.FC<Props> = ({
  sources,
  enrichers,
  organizations = [],
  onCreate,
  onEdit,
  onDelete,
  onTest,
  testingId = null,
}) => {
  const { t } = useTranslation("enrichment")
  const [query, setQuery] = useState("")
  const [filter, setFilter] = useState<Filter>("all")

  const byName = useMemo(() => {
    const map = new Map<string, EnricherCatalogItem>()
    for (const e of enrichers) map.set(e.name, e)
    return map
  }, [enrichers])

  const orgName = useMemo(() => {
    const map = new Map<number, string>()
    for (const o of organizations) map.set(o.id, o.name)
    return map
  }, [organizations])

  /** Uma fonte "com problema" é a que pede ação agora. */
  function hasProblem(s: EnrichmentSource): boolean {
    const needsSecret = (byName.get(s.enricher)?.required_secrets?.length ?? 0) > 0
    if (needsSecret && !s.secret_configured) return true
    if (s.last_test_ok === false) return true
    return !s.enabled
  }

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase()
    return sources.filter((s) => {
      if (q && !`${s.name} ${s.enricher} ${s.description ?? ""}`.toLowerCase().includes(q)) {
        return false
      }
      if (filter === "problem") return hasProblem(s)
      if (filter === "third_party") return byName.get(s.enricher)?.egress === "third_party"
      return true
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sources, query, filter, byName])

  const problemCount = useMemo(
    () => sources.filter(hasProblem).length,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sources, byName],
  )

  const columns: TableColumn<EnrichmentSource>[] = [
    {
      key: "name",
      title: t("sources.table.source"),
      dataIndex: "name",
      sortable: true,
      render: (_v, s) => (
        <button
          type="button"
          className="text-left hover:underline"
          onClick={() => onEdit(s)}
          data-testid={`source-row-${s.name}`}
        >
          <span className="font-medium">{s.name}</span>
          {s.description ? (
            <span className="block truncate text-xs text-muted">{s.description}</span>
          ) : null}
        </button>
      ),
    },
    {
      key: "enricher",
      title: t("sources.table.enricher"),
      dataIndex: "enricher",
      sortable: true,
      render: (_v, s) => {
        const cat = byName.get(s.enricher)
        return (
          <div>
            <span className="text-sm">{cat?.label ?? s.enricher}</span>
            <span className="block text-xs text-muted">
              {cat ? t(`catalog.mode.${cat.mode}`) : s.enricher}
            </span>
          </div>
        )
      },
    },
    {
      key: "egress",
      title: t("sources.table.egress"),
      dataIndex: "enricher",
      className: "hidden lg:table-cell",
      render: (_v, s) => {
        const egress = byName.get(s.enricher)?.egress
        if (egress === "third_party") {
          return <Badge variant="warning">{t("egress.thirdParty")}</Badge>
        }
        if (egress === "internal") {
          return <Badge variant="primary">{t("egress.internal")}</Badge>
        }
        return <Badge variant="success">{t("egress.none")}</Badge>
      },
    },
    {
      key: "secret",
      title: t("sources.table.credential"),
      dataIndex: "secret_configured",
      render: (_v, s) => {
        const needs = (byName.get(s.enricher)?.required_secrets?.length ?? 0) > 0
        if (!needs) return <Badge variant="default">{t("sources.table.noSecretNeeded")}</Badge>
        return (
          <Badge variant={s.secret_configured ? "success" : "warning"}>
            {s.secret_configured ? t("sources.secretConfigured") : t("sources.secretMissing")}
          </Badge>
        )
      },
    },
    {
      key: "last_test",
      title: t("sources.table.lastTest"),
      dataIndex: "last_test_at",
      render: (_v, s) => {
        // Três estados distintos, e a diferença entre eles é a ação do
        // operador. Fundi-los num só booleano era o defeito do card.
        if (!s.last_test_at) {
          return <span className="text-xs text-warning-500">{t("sources.table.neverTested")}</span>
        }
        if (s.last_test_ok) {
          return (
            <span className="text-xs text-muted">
              ✓ {ago(s.last_test_at, t as never)}
            </span>
          )
        }
        return (
          <div className="min-w-0">
            <span className="text-xs text-danger-500">
              ✗ {ago(s.last_test_at, t as never)}
            </span>
            {s.last_test_message ? (
              <span
                className="block truncate font-mono text-[11px] text-danger-500"
                title={s.last_test_message}
              >
                {s.last_test_message}
              </span>
            ) : null}
          </div>
        )
      },
    },
    {
      key: "usedBy",
      title: t("sources.table.usedBy"),
      dataIndex: "shared_organization_ids",
      className: "hidden xl:table-cell",
      render: (_v, s) => {
        const shared = s.shared_organization_ids ?? []
        const owner = orgName.get(s.organization_id) ?? `#${s.organization_id}`
        if (shared.length === 0) return <span className="text-xs text-muted">{owner}</span>
        return (
          <span className="text-xs text-muted">
            {t("sources.table.ownerPlusChildren", { owner, count: shared.length })}
          </span>
        )
      },
    },
    {
      key: "actions",
      title: "",
      dataIndex: "id",
      align: "right",
      render: (_v, s) => (
        <div className="flex justify-end gap-1">
          <Button
            variant="outline"
            size="xs"
            loading={testingId === s.id}
            onClick={() => onTest(s)}
          >
            {t("sources.test.run")}
          </Button>
          <Button
            variant="outline"
            size="xs"
            aria-label={t("sources.deleteAction")}
            onClick={() => onDelete(s)}
          >
            <Trash2Icon size={12} aria-hidden />
          </Button>
        </div>
      ),
    },
  ]

  if (sources.length === 0) {
    return (
      <EmptyState
        title={t("sources.emptyTitle")}
        description={t("sources.emptyDescription")}
        action={
          <Button variant="primary" onClick={onCreate} leftIcon={<PlusIcon size={14} />}>
            {t("sources.form.create")}
          </Button>
        }
      />
    )
  }

  return (
    <div className="space-y-3" data-testid="sources-table">
      <div className="flex flex-wrap items-end gap-3">
        <div className="w-64">
          <Input
            label={t("sources.table.search")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("catalog.searchPlaceholder")}
          />
        </div>
        <div className="w-56">
          <Select
            label={t("sources.table.filter")}
            value={filter}
            onValueChange={(v) => setFilter(v as Filter)}
            options={[
              { value: "all", label: t("sources.table.filterAll", { count: sources.length }) },
              {
                value: "problem",
                label: t("sources.table.filterProblem", { count: problemCount }),
              },
              { value: "third_party", label: t("sources.table.filterThirdParty") },
            ]}
            size="sm"
          />
        </div>
        {/* A ação primária do cabeçalho é estável ("Nova política"), então
            criar fonte precisa de um botão AQUI — onde o contexto já é o de
            fontes. Sem ele, a aba com fontes cadastradas não tinha por onde
            adicionar outra: o botão só existia no estado vazio. */}
        <div className="ml-auto">
          <Button
            variant="outline"
            size="sm"
            onClick={onCreate}
            leftIcon={<PlusIcon size={14} />}
          >
            {t("sources.form.create")}
          </Button>
        </div>
      </div>

      <DataTable<EnrichmentSource>
        data={rows}
        columns={columns}
        emptyMessage={t("sources.table.noMatch")}
      />
    </div>
  )
}

export default SourcesTable
