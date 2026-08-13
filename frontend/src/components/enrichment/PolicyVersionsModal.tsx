import type React from "react"
import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { PencilIcon, PlayIcon } from "lucide-react"
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
  /**
   * Última versão que o editor carregou. Publicar cria uma versão nova e a
   * página recarrega a política, então `current_version_id` muda e o efeito
   * dispara de novo. Sem esta guarda, ele re-hidrataria por cima do que o
   * operador já começou a editar DEPOIS de publicar.
   */
  const hydratedVersionRef = useRef<string | null>(null)
  /** Política que o modal está mostrando agora. Ver o efeito abaixo. */
  const openedPolicyRef = useRef<string | null>(null)
  const [loadingRules, setLoadingRules] = useState(false)
  /** Caminhos que a org produz, para sugerir em `key.source`. */
  const [keySources, setKeySources] = useState<string[]>([])
  const [keySourcesFromMappings, setKeySourcesFromMappings] = useState(false)
  const [hydrateError, setHydrateError] = useState<string | null>(null)
  /** Versão de onde as regras do editor vieram. Só serve para rotular a UI. */
  const [loadedVersionNumber, setLoadedVersionNumber] = useState<number | null>(null)
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

  /**
   * Carrega as regras de uma versão no editor.
   *
   * Publicar SUBSTITUI a versão inteira, não faz merge. Com o editor abrindo
   * vazio, adicionar uma regra a uma política que já tinha três publicava uma
   * versão com uma só, e as outras três sumiam da versão vigente sem aviso.
   * Por isso a hidratação não é conveniência: é o que torna a edição correta.
   */
  const hydrateFrom = useCallback(
    (versionId: string) => {
      if (!policy) return
      const token = `${policy.id}:${versionId}`
      hydratedVersionRef.current = token
      setLoadingRules(true)
      setHydrateError(null)
      // Limpa ANTES de buscar. Sem isto, o editor segue exibindo as regras da
      // política anterior enquanto carrega, e se a carga falhar elas ficam lá
      // parecendo ser desta política. Publicar dali substituiria a política
      // errada, porque publicar troca a lista inteira.
      setRules([])
      setLoadedVersionNumber(null)
      api
        .getEnrichmentPolicyVersion(policy.id, versionId)
        .then((v) => {
          // Descarta resposta obsoleta. O modal NUNCA desmonta (a página o
          // renderiza sempre, com `open`), então o GET atrasado de uma política
          // já fechada chegava e sobrescrevia o editor da política aberta agora.
          if (hydratedVersionRef.current !== token) return
          setRules(v.rules ?? [])
          setLoadedVersionNumber(v.version_number)
        })
        .catch((err) => {
          if (hydratedVersionRef.current !== token) return
          setHydrateError(err instanceof Error ? err.message : String(err))
        })
        .finally(() => {
          if (hydratedVersionRef.current === token) setLoadingRules(false)
        })
    },
    [policy],
  )

  // IDENTIDADE, não referência. Publicar chama `onChanged`, que recarrega a
  // lista de políticas na página e entrega um OBJETO NOVO com o mesmo id. Um
  // efeito que dependesse de `policy` re-hidrataria aí: apagaria o aviso de
  // "versão publicada" e sobrescreveria qualquer regra que o operador já
  // tivesse começado a editar depois de publicar.
  const policyId = policy?.id ?? null
  const currentVersionId = policy?.current_version_id ?? null

  // Sugestões de caminho são da org DA POLÍTICA, não a do usuário: um admin
  // global editando a política do cliente X tem que ver os campos do X.
  useEffect(() => {
    if (!open || !policy) return
    let cancelled = false
    api
      .listEnrichmentKeySources({ organization_id: policy.organization_id })
      .then((res) => {
        if (cancelled) return
        setKeySources(res.suggestions.map((s) => s.path))
        setKeySourcesFromMappings(res.from_active_mappings)
      })
      .catch(() => {
        // Sem sugestão o campo degrada para texto livre, que é o que ele era
        // antes. Falhar aqui não pode impedir a edição da política.
        if (!cancelled) {
          setKeySources([])
          setKeySourcesFromMappings(false)
        }
      })
    return () => { cancelled = true }
  }, [open, policy])

  useEffect(() => {
    if (!open || !policyId) {
      hydratedVersionRef.current = null
      openedPolicyRef.current = null
      return
    }
    loadVersions()

    // Só zera o transitório quando é OUTRA política. Publicar faz a página
    // recarregar as políticas, o que muda `current_version_id` e redispara este
    // efeito: limpar aqui apagava o "Versão publicada" e o resultado do dry-run
    // segundos depois de aparecerem, sem que nada tivesse dado errado.
    if (openedPolicyRef.current !== policyId) {
      openedPolicyRef.current = policyId
      setCommitMessage("")
      setPublishError(null)
      setPublishSummary(null)
      setDryRunResult(null)
      setDryRunError(null)
      setToggleError(null)
      setHydrateError(null)
    }
    // Política sem versão publicada abre em branco, que é o correto: não há
    // o que preservar. Com versão vigente, o editor parte dela.
    if (currentVersionId) {
      if (hydratedVersionRef.current !== `${policyId}:${currentVersionId}`) {
        hydrateFrom(currentVersionId)
      }
    } else {
      // Política sem versão: qualquer resposta em voo passa a ser obsoleta.
      hydratedVersionRef.current = `${policyId}:vazia`
      setRules([])
      setLoadedVersionNumber(null)
      // Baixar a bandeira aqui também. A carga anterior já foi invalidada pelo
      // token acima, então o `finally` dela não vai mais mexer neste estado, e
      // sem isto o editor ficava em esqueleto para sempre.
      setLoadingRules(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, policyId, currentVersionId])

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
      // As regras FICAM no editor: acabaram de virar a versão vigente, e
      // esvaziar aqui reintroduziria o mesmo erro que a hidratação corrige
      // (publicar de novo em seguida apagaria tudo).
      setLoadedVersionNumber(result.version_number ?? null)
      hydratedVersionRef.current = `${policy.id}:${result.id}`
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
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold">{t("policies.versions.publishNew")}</h3>
            {loadedVersionNumber != null && (
              <Badge variant="default" data-testid="editing-from-version">
                {t("policies.versions.editingFrom", { version: loadedVersionNumber })}
              </Badge>
            )}
          </div>

          {/* Publicar substitui a versão inteira. Dizer isso aqui evita a
              descoberta cara: publicar e só depois notar que as outras regras
              sumiram da versão vigente. */}
          <p className="text-xs text-muted">{t("policies.versions.replaceWarning")}</p>

          {hydrateError && <Notice variant="danger" title={hydrateError} />}
          {publishError && <Notice variant="danger" title={publishError} />}
          {publishSummary && (
            <Notice variant="success" title={t("policies.versions.published")}>
              {t("policies.versions.publishedDetail", { count: publishSummary?.rule_count ?? 0 })}
            </Notice>
          )}

          {loadingRules ? (
            <SkeletonCard />
          ) : (
            <PolicyRuleEditor
              rules={rules}
              enrichers={enrichers}
              tables={tables}
              sources={sources}
              keySources={keySources}
              keySourcesFromMappings={keySourcesFromMappings}
              onChange={setRules}
            />
          )}

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
            disabled={loadingRules}
          />

          <div className="flex justify-end">
            <Button
              type="submit"
              variant="primary"
              loading={publishing}
              disabled={loadingRules}
            >
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
                  <div className="flex shrink-0 items-center gap-2">
                    {/* Carregar uma versão antiga no editor é diferente de
                        rollback: rollback troca a vigente na hora, isto abre
                        para revisar e publicar uma nova por cima. */}
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => hydrateFrom(v.id)}
                      data-testid={`load-version-${v.version_number}`}
                      leftIcon={<PencilIcon size={12} />}
                    >
                      {t("policies.versions.loadIntoEditor")}
                    </Button>
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
                  </div>
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
