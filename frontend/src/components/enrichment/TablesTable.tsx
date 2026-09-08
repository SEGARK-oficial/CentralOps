import type React from "react"
import { useMemo } from "react"
import { useTranslation } from "react-i18next"
import { NetworkIcon, PlusIcon, TableIcon, Trash2Icon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { DataTable } from "@/components/ui/DataTable/DataTable"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import type { TableColumn } from "@/types"
import type { EnrichmentTable } from "@/services/api"

/**
 * Tabelas do cliente como lista, pelo mesmo motivo das fontes.
 *
 * O card mostrava nome, contagem e tamanho — e escondia as duas coisas que
 * decidem uma ação: se a tabela tem versão publicada (sem ela a busca falha a
 * cada ciclo, sem erro em tela) e se alguma regra a cita (sem isso ela é peso
 * morto, e com isso ela não pode ser apagada).
 *
 * O tamanho ganha proporção contra o teto por tabela, que vem da configuração
 * da instalação e não de um número fixo aqui: exibir "88 KiB" sozinho não diz
 * se sobra espaço, e o teto é editável em Configuração › Enriquecimento.
 */

interface Props {
  tables: EnrichmentTable[]
  /** Nomes de tabela citados pelas regras vigentes, por nome de tabela. */
  citedBy?: Record<string, string[]>
  /** Teto por tabela, vindo da configuração. */
  maxTableBytes?: number
  onCreate: () => void
  onOpen: (table: EnrichmentTable) => void
  onDelete: (table: EnrichmentTable) => void
}

const DEFAULT_MAX_BYTES = 32 * 1024 * 1024

function fmtBytes(n: number): string {
  if (!n) return "0 B"
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`
  return `${(n / (1024 * 1024)).toFixed(1)} MiB`
}

export const TablesTable: React.FC<Props> = ({
  tables,
  citedBy = {},
  maxTableBytes = DEFAULT_MAX_BYTES,
  onCreate,
  onOpen,
  onDelete,
}) => {
  const { t } = useTranslation("enrichment")

  const columns: TableColumn<EnrichmentTable>[] = useMemo(
    () => [
      {
        key: "name",
        title: t("tables.table.name"),
        dataIndex: "name",
        sortable: true,
        render: (_v, tb) => (
          <button
            type="button"
            className="text-left hover:underline"
            onClick={() => onOpen(tb)}
            data-testid={`table-row-${tb.name}`}
          >
            <span className="font-medium">{tb.name}</span>
            {tb.description ? (
              <span className="block truncate text-xs text-muted">{tb.description}</span>
            ) : null}
          </button>
        ),
      },
      {
        key: "match_mode",
        title: t("tables.table.match"),
        dataIndex: "match_mode",
        render: (_v, tb) => (
          <Badge variant="outline" className="gap-1">
            {tb.match_mode === "cidr" ? (
              <NetworkIcon size={12} aria-hidden />
            ) : (
              <TableIcon size={12} aria-hidden />
            )}
            {t(`tables.mode.${tb.match_mode}`)}
          </Badge>
        ),
      },
      {
        key: "version",
        title: t("tables.table.version"),
        dataIndex: "current_version_id",
        render: (_v, tb) =>
          tb.current_version_id ? (
            <span className="text-xs text-muted">
              {tb.entry_count.toLocaleString()} {t("tables.entries").toLowerCase()}
            </span>
          ) : (
            // Caso de suporte nº 2: a regra cita a tabela, a carga falha a cada
            // ciclo, e o evento sai sem contexto sem erro nenhum na tela.
            <Badge variant="warning">{t("tables.noVersion")}</Badge>
          ),
      },
      {
        key: "size",
        title: t("tables.table.size"),
        dataIndex: "approx_bytes",
        sortable: true,
        className: "hidden lg:table-cell",
        render: (_v, tb) => {
          const ratio = maxTableBytes > 0 ? tb.approx_bytes / maxTableBytes : 0
          return (
            <div className="min-w-[110px]">
              <span className="font-mono text-xs tabular-nums">
                {fmtBytes(tb.approx_bytes)}
              </span>
              {/* Proporção contra o teto: "88 KiB" sozinho não diz se sobra
                  espaço, e estourar é recusa no servidor. */}
              <span className="mt-1 block h-1 overflow-hidden rounded bg-surface-tertiary">
                <span
                  // `>=`, não `>`: exatamente 80% do teto já é o ponto em que
                  // vale avisar, e a comparação estrita deixava a fronteira
                  // sem aviso justamente no valor redondo que alguém escolhe.
                  className={
                    ratio >= 0.8
                      ? "block h-full bg-warning-500"
                      : "block h-full bg-stage-enrich"
                  }
                  style={{ width: `${Math.min(100, Math.max(2, ratio * 100))}%` }}
                />
              </span>
            </div>
          )
        },
      },
      {
        key: "used",
        title: t("tables.table.usedBy"),
        dataIndex: "name",
        className: "hidden xl:table-cell",
        render: (_v, tb) => {
          const users = citedBy[tb.name] ?? []
          if (users.length === 0) {
            // Não é erro: uma tabela pode existir antes da regra que a usará.
            return <span className="text-xs text-muted">{t("tables.table.unused")}</span>
          }
          return (
            <span className="text-xs text-muted" title={users.join(", ")}>
              {t("tables.table.usedByRules", { count: users.length })}
            </span>
          )
        },
      },
      {
        key: "actions",
        title: "",
        dataIndex: "id",
        align: "right",
        render: (_v, tb) => (
          <Button
            variant="outline"
            size="xs"
            aria-label={t("tables.deleteAction")}
            onClick={() => onDelete(tb)}
          >
            <Trash2Icon size={12} aria-hidden />
          </Button>
        ),
      },
    ],
    [t, citedBy, maxTableBytes, onOpen, onDelete],
  )

  if (tables.length === 0) {
    return (
      <EmptyState
        icon={<TableIcon size={28} aria-hidden />}
        title={t("tables.emptyTitle")}
        description={t("tables.emptyDescription")}
        action={
          <Button variant="primary" onClick={onCreate} leftIcon={<PlusIcon size={14} />}>
            {t("tables.form.create")}
          </Button>
        }
      />
    )
  }

  return (
    <div className="space-y-3" data-testid="tables-table">
      <div className="flex justify-end">
        <Button
          variant="outline"
          size="sm"
          onClick={onCreate}
          leftIcon={<PlusIcon size={14} />}
        >
          {t("tables.form.create")}
        </Button>
      </div>
      <DataTable<EnrichmentTable> data={tables} columns={columns} />
    </div>
  )
}

export default TablesTable
