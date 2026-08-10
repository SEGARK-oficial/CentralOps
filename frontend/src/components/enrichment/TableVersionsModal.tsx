import type React from "react"
import { useCallback, useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Modal } from "@/components/ui/Modal/Modal"
import { Button } from "@/components/ui/Button/Button"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import { Input } from "@/components/ui/Input/Input"
import { Badge } from "@/components/ui/Badge/Badge"
import { Notice } from "@/components/ui/Notice/Notice"
import { SkeletonCard } from "@/components/ui/Skeleton"
import * as api from "@/services/api"
import type { EnrichmentTable, EnrichmentTableVersion } from "@/services/api"

interface TableVersionsModalProps {
  open: boolean
  table: EnrichmentTable | null
  onClose: () => void
  /** Chamado após publicar uma versão ou reverter — o pai recarrega a lista de tabelas. */
  onChanged: () => void
}

const EXAMPLE_EXACT = `{
  "10.0.5.7": { "site": "filial-sp", "criticality": "alta" }
}`

const EXAMPLE_CIDR = `{
  "10.0.0.0/16": { "site": "matriz", "criticality": "normal" },
  "10.0.5.0/24": { "site": "filial-sp", "criticality": "alta" }
}`

/**
 * Publica novas versões de uma tabela e mostra o histórico com rollback.
 *
 * O corpo é colado como JSON `{chave: {campo: valor}}` — a mesma forma que a
 * API espera — em vez de um editor de planilha: é o caminho mais direto para
 * quem já exportou a tabela de outro sistema (CMDB, NetBox, planilha → JSON).
 */
export const TableVersionsModal: React.FC<TableVersionsModalProps> = ({
  open,
  table,
  onClose,
  onChanged,
}) => {
  const { t } = useTranslation("enrichment")
  const [versions, setVersions] = useState<EnrichmentTableVersion[]>([])
  const [loadingVersions, setLoadingVersions] = useState(false)
  const [listError, setListError] = useState<string | null>(null)

  const [rowsText, setRowsText] = useState("")
  const [commitMessage, setCommitMessage] = useState("")
  const [publishing, setPublishing] = useState(false)
  const [publishError, setPublishError] = useState<string | null>(null)
  const [lastResult, setLastResult] = useState<EnrichmentTableVersion | null>(null)

  const [rollingBackId, setRollingBackId] = useState<string | null>(null)

  const loadVersions = useCallback(() => {
    if (!table) return
    setLoadingVersions(true)
    setListError(null)
    api
      .listEnrichmentTableVersions(table.id)
      .then(setVersions)
      .catch((err) => setListError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoadingVersions(false))
  }, [table])

  useEffect(() => {
    if (open && table) {
      loadVersions()
      setRowsText("")
      setCommitMessage("")
      setPublishError(null)
      setLastResult(null)
    }
  }, [open, table, loadVersions])

  async function handlePublish(e: React.FormEvent) {
    e.preventDefault()
    if (!table) return
    setPublishError(null)

    let rows: Record<string, Record<string, unknown>>
    try {
      const parsed = JSON.parse(rowsText)
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error(t("tables.versions.mustBeObject"))
      }
      rows = parsed as Record<string, Record<string, unknown>>
    } catch (err) {
      setPublishError(
        err instanceof SyntaxError ? t("tables.versions.invalidJson") : (err as Error).message,
      )
      return
    }

    if (!commitMessage.trim()) {
      setPublishError(t("tables.versions.commitMessageRequired"))
      return
    }

    setPublishing(true)
    try {
      const result = await api.commitEnrichmentTableVersion(table.id, {
        rows,
        commit_message: commitMessage.trim(),
      })
      setLastResult(result)
      setRowsText("")
      setCommitMessage("")
      loadVersions()
      onChanged()
    } catch (err) {
      setPublishError(err instanceof Error ? err.message : String(err))
    } finally {
      setPublishing(false)
    }
  }

  async function handleRollback(versionId: string) {
    if (!table) return
    setRollingBackId(versionId)
    setListError(null)
    try {
      await api.rollbackEnrichmentTable(table.id, versionId)
      loadVersions()
      onChanged()
    } catch (err) {
      setListError(err instanceof Error ? err.message : String(err))
    } finally {
      setRollingBackId(null)
    }
  }

  if (!table) return null

  return (
    <Modal open={open} onClose={onClose} title={t("tables.versions.title", { name: table.name })} size="xl">
      <div className="space-y-6">
        {/* ── Publicar nova versão ─────────────────────────────────────── */}
        <form onSubmit={handlePublish} className="space-y-3 rounded-lg border border-border p-4">
          <h3 className="text-sm font-semibold">{t("tables.versions.publishNew")}</h3>

          {publishError && <Notice variant="danger" title={publishError} />}

          {lastResult && (
            <Notice variant={lastResult.invalid_rows > 0 ? "warning" : "success"} title={t("tables.versions.published")}>
              {t("tables.versions.publishedDetail", {
                entries: lastResult.entry_count,
                invalid: lastResult.invalid_rows,
              })}
            </Notice>
          )}

          <Textarea
            label={t("tables.versions.rowsLabel")}
            value={rowsText}
            onChange={(e) => setRowsText(e.target.value)}
            rows={8}
            className="font-mono text-xs"
            placeholder={table.match_mode === "cidr" ? EXAMPLE_CIDR : EXAMPLE_EXACT}
            helperText={
              table.match_mode === "cidr"
                ? t("tables.versions.rowsHelperCidr")
                : t("tables.versions.rowsHelperExact")
            }
          />

          <Input
            label={t("tables.versions.commitMessage")}
            value={commitMessage}
            onChange={(e) => setCommitMessage(e.target.value)}
            placeholder={t("tables.versions.commitMessagePlaceholder")}
          />

          <div className="flex justify-end">
            <Button type="submit" variant="primary" loading={publishing}>
              {t("tables.versions.publish")}
            </Button>
          </div>
        </form>

        {/* ── Histórico ────────────────────────────────────────────────── */}
        <div className="space-y-2">
          <h3 className="text-sm font-semibold">{t("tables.versions.history")}</h3>

          {listError && <Notice variant="danger" title={listError} />}

          {loadingVersions ? (
            <SkeletonCard />
          ) : versions.length === 0 ? (
            <p className="text-sm text-muted">{t("tables.versions.empty")}</p>
          ) : (
            <ul className="divide-y divide-border rounded-lg border border-border">
              {versions.map((v) => (
                <li key={v.id} className="flex items-center justify-between gap-3 p-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-sm">v{v.version_number}</span>
                      {v.is_current && <Badge variant="success">{t("tables.versions.current")}</Badge>}
                      {v.invalid_rows > 0 && (
                        <Badge variant="warning">
                          {t("tables.versions.invalidCount", { count: v.invalid_rows })}
                        </Badge>
                      )}
                    </div>
                    <p className="truncate text-sm text-muted">{v.commit_message}</p>
                    <p className="text-xs text-muted">
                      {t("tables.versions.entriesShort", { count: v.entry_count })}
                      {v.created_at ? ` · ${new Date(v.created_at).toLocaleString()}` : ""}
                    </p>
                  </div>
                  {!v.is_current && (
                    <Button
                      variant="outline"
                      size="sm"
                      loading={rollingBackId === v.id}
                      onClick={() => handleRollback(v.id)}
                    >
                      {t("tables.versions.rollback")}
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Modal>
  )
}

export default TableVersionsModal
