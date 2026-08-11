import type React from "react"
import { useCallback, useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { PlayIcon } from "lucide-react"
import { Modal } from "@/components/ui/Modal/Modal"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import { Badge } from "@/components/ui/Badge/Badge"
import { Notice } from "@/components/ui/Notice/Notice"
import { SkeletonCard } from "@/components/ui/Skeleton"
import { JsonViewer } from "@/components/shared/JsonViewer"
import { PolicyRuleEditor } from "./PolicyRuleEditor"
import * as api from "@/services/api"
import type {
  EnricherCatalogItem,
  EnrichmentDryRunResponse,
  EnrichmentPolicy,
  EnrichmentPolicyVersion,
  EnrichmentRule,
  EnrichmentSource,
  EnrichmentTable,
} from "@/services/api"

interface PolicyVersionsModalProps {
  open: boolean
  policy: EnrichmentPolicy | null
  enrichers: EnricherCatalogItem[]
  tables: EnrichmentTable[]
  sources?: EnrichmentSource[]
  onClose: () => void
  onChanged: () => void
}

const SAMPLE_PLACEHOLDER = `{
  "_centralops": { "organization_id": 1 },
  "normalized": { "src_endpoint": { "ip": "10.0.5.7" } },
  "raw": {}
}`

const TABLES_PLACEHOLDER = `{
  "regra-1": { "10.0.5.7": { "site": "filial-sp" } }
}`

/**
 * Publica novas versões da política (editor estruturado + dry-run embutido),
 * mostra o histórico com rollback, e liga/desliga a política.
 *
 * O dry-run roda ANTES de qualquer publicação — é a mesma disciplina de
 * "testar antes de publicar" do roteamento (`routing-dry-run.md`), aplicada
 * aqui à política de enriquecimento. Ele testa as regras JÁ NO EDITOR, então
 * o operador pode iterar sem publicar nada.
 */
export const PolicyVersionsModal: React.FC<PolicyVersionsModalProps> = ({
  open,
  policy,
  enrichers,
  tables,
  sources = [],
  onClose,
  onChanged,
}) => {
  const { t } = useTranslation("enrichment")

  const [versions, setVersions] = useState<EnrichmentPolicyVersion[]>([])
  const [loadingVersions, setLoadingVersions] = useState(false)
  const [listError, setListError] = useState<string | null>(null)
  const [rollingBackId, setRollingBackId] = useState<string | null>(null)
  const [togglingEnabled, setTogglingEnabled] = useState(false)
  const [toggleError, setToggleError] = useState<string | null>(null)

  const [rules, setRules] = useState<EnrichmentRule[]>([])
  const [commitMessage, setCommitMessage] = useState("")
  const [publishing, setPublishing] = useState(false)
  const [publishError, setPublishError] = useState<string | null>(null)
  const [publishSummary, setPublishSummary] = useState<EnrichmentPolicyVersion["summary"] | null>(null)

  const [sampleText, setSampleText] = useState(SAMPLE_PLACEHOLDER)
  const [tablesText, setTablesText] = useState("")
  const [dryRunning, setDryRunning] = useState(false)
  const [dryRunError, setDryRunError] = useState<string | null>(null)
  const [dryRunResult, setDryRunResult] = useState<EnrichmentDryRunResponse | null>(null)

  const loadVersions = useCallback(() => {
    if (!policy) return
    setLoadingVersions(true)
    setListError(null)
    api
      .listEnrichmentPolicyVersions(policy.id)
      .then(setVersions)
      .catch((err) => setListError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoadingVersions(false))
  }, [policy])

  useEffect(() => {
    if (open && policy) {
      loadVersions()
      setRules([])
      setCommitMessage("")
      setPublishError(null)
      setPublishSummary(null)
      setDryRunResult(null)
      setDryRunError(null)
      setToggleError(null)
    }
  }, [open, policy, loadVersions])

  async function handleDryRun() {
    setDryRunError(null)
    setDryRunResult(null)

    if (rules.length === 0) {
      setDryRunError(t("policies.versions.dryRun.needsRules"))
      return
    }

    let sample: Record<string, unknown>
    try {
      sample = JSON.parse(sampleText)
    } catch {
      setDryRunError(t("policies.versions.dryRun.invalidSample"))
      return
    }

    let simulatedTables: Record<string, Record<string, unknown>> | undefined
    if (tablesText.trim()) {
      try {
        simulatedTables = JSON.parse(tablesText)
      } catch {
        setDryRunError(t("policies.versions.dryRun.invalidTables"))
        return
      }
    }

    setDryRunning(true)
    try {
      const result = await api.dryRunEnrichment({ rules, sample, tables: simulatedTables })
      setDryRunResult(result)
    } catch (err) {
      setDryRunError(err instanceof Error ? err.message : String(err))
    } finally {
      setDryRunning(false)
    }
  }

  async function handlePublish(e: React.FormEvent) {
    e.preventDefault()
    if (!policy) return
    setPublishError(null)

    if (rules.length === 0) {
      setPublishError(t("policies.versions.rulesEmpty"))
      return
    }
    if (!commitMessage.trim()) {
      setPublishError(t("tables.versions.commitMessageRequired"))
      return
    }

    setPublishing(true)
    try {
      const result = await api.commitEnrichmentPolicyVersion(policy.id, {
        rules,
        commit_message: commitMessage.trim(),
      })
      setPublishSummary(result.summary)
      setRules([])
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
    if (!policy) return
    setRollingBackId(versionId)
    setListError(null)
    try {
      await api.rollbackEnrichmentPolicy(policy.id, versionId)
      loadVersions()
      onChanged()
    } catch (err) {
      setListError(err instanceof Error ? err.message : String(err))
    } finally {
      setRollingBackId(null)
    }
  }

  async function handleToggleEnabled() {
    if (!policy) return
    setTogglingEnabled(true)
    setToggleError(null)
    try {
      await api.setEnrichmentPolicyEnabled(policy.id, !policy.enabled)
      onChanged()
    } catch (err) {
      setToggleError(err instanceof Error ? err.message : String(err))
    } finally {
      setTogglingEnabled(false)
    }
  }

  if (!policy) return null

  return (
    <Modal open={open} onClose={onClose} title={t("policies.versions.title", { name: policy.name })} size="xl">
      <div className="space-y-6">
        {/* ── Habilitar/desabilitar ────────────────────────────────────── */}
        <div className="flex items-center justify-between rounded-lg border border-border p-4">
          <div>
            <p className="text-sm font-medium">
              {policy.enabled ? t("policies.versions.enabledNow") : t("policies.versions.disabledNow")}
            </p>
            {!policy.current_version_id && (
              <p className="text-xs text-muted">{t("policies.versions.needsVersionToEnable")}</p>
            )}
          </div>
          <Button
            variant={policy.enabled ? "outline" : "primary"}
            loading={togglingEnabled}
            disabled={!policy.enabled && !policy.current_version_id}
            onClick={handleToggleEnabled}
          >
            {policy.enabled ? t("policies.versions.disable") : t("policies.versions.enable")}
          </Button>
        </div>
        {toggleError && <Notice variant="danger" title={toggleError} />}

        {/* ── Editor de regras + dry-run ───────────────────────────────── */}
        <form onSubmit={handlePublish} className="space-y-4 rounded-lg border border-border p-4">
          <h3 className="text-sm font-semibold">{t("policies.versions.publishNew")}</h3>

          {publishError && <Notice variant="danger" title={publishError} />}
          {publishSummary && (
            <Notice variant="success" title={t("policies.versions.published")}>
              {t("policies.versions.publishedDetail", { count: publishSummary?.rule_count ?? 0 })}
            </Notice>
          )}

          <PolicyRuleEditor
            rules={rules}
            enrichers={enrichers}
            tables={tables}
            sources={sources}
            onChange={setRules}
          />

          {/* Dry-run */}
          <div className="space-y-3 rounded-md bg-surface-secondary p-3">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wide text-muted">
                {t("policies.versions.dryRun.title")}
              </span>
              <Button
                type="button"
                variant="outline"
                size="sm"
                loading={dryRunning}
                onClick={handleDryRun}
                leftIcon={<PlayIcon size={12} />}
              >
                {t("policies.versions.dryRun.run")}
              </Button>
            </div>
            <p className="text-xs text-muted">{t("policies.versions.dryRun.hint")}</p>

            <div className="grid gap-3 sm:grid-cols-2">
              <Textarea
                label={t("policies.versions.dryRun.sample")}
                value={sampleText}
                onChange={(e) => setSampleText(e.target.value)}
                rows={6}
                className="font-mono text-xs"
              />
              <Textarea
                label={t("policies.versions.dryRun.tables")}
                value={tablesText}
                onChange={(e) => setTablesText(e.target.value)}
                rows={6}
                className="font-mono text-xs"
                placeholder={TABLES_PLACEHOLDER}
                helperText={t("policies.versions.dryRun.tablesHint")}
              />
            </div>

            {dryRunError && <Notice variant="danger" title={dryRunError} />}

            {dryRunResult && (
              <div className="space-y-2" data-testid="dry-run-result">
                <div className="flex flex-wrap gap-2">
                  <Badge variant="default">
                    {t("policies.versions.dryRun.bytesAdded", { count: dryRunResult.bytes_added })}
                  </Badge>
                  {Object.entries(dryRunResult.hits).map(([id, n]) => (
                    <Badge key={`hit-${id}`} variant="success">
                      {id}: {n} {t("policies.versions.dryRun.hits")}
                    </Badge>
                  ))}
                  {Object.entries(dryRunResult.misses).map(([id, n]) => (
                    <Badge key={`miss-${id}`} variant="warning">
                      {id}: {n} {t("policies.versions.dryRun.misses")}
                    </Badge>
                  ))}
                  {Object.entries(dryRunResult.errors).map(([id, n]) => (
                    <Badge key={`err-${id}`} variant="danger">
                      {id}: {n} {t("policies.versions.dryRun.errors")}
                    </Badge>
                  ))}
                </div>
                <div className="max-h-64 overflow-auto rounded-md border border-border p-2">
                  <JsonViewer data={dryRunResult.enriched} collapseLevel={4} />
                </div>
              </div>
            )}
          </div>

          <Input
            label={t("tables.versions.commitMessage")}
            value={commitMessage}
            onChange={(e) => setCommitMessage(e.target.value)}
            placeholder={t("tables.versions.commitMessagePlaceholder")}
          />

          <div className="flex justify-end">
            <Button type="submit" variant="primary" loading={publishing}>
              {t("policies.versions.publish")}
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
                      {v.summary && (
                        <Badge variant="default">
                          {t("policies.versions.ruleCount", { count: v.summary.rule_count })}
                        </Badge>
                      )}
                    </div>
                    <p className="truncate text-sm text-muted">{v.commit_message}</p>
                    {v.created_at && (
                      <p className="text-xs text-muted">{new Date(v.created_at).toLocaleString()}</p>
                    )}
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

export default PolicyVersionsModal
