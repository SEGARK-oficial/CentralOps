import type React from "react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { useNavigate, useParams } from "react-router-dom"
import { ArrowLeftIcon, HistoryIcon, PlayIcon } from "lucide-react"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { ErrorState } from "@/components/ui/ErrorState"
import { Input } from "@/components/ui/Input/Input"
import { Notice } from "@/components/ui/Notice/Notice"
import { Select } from "@/components/ui/Select/Select"
import { SkeletonCard } from "@/components/ui/Skeleton"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import { JsonViewer } from "@/components/shared/JsonViewer"
import { PolicyRuleEditor } from "@/components/enrichment/PolicyRuleEditor"
import { PolicyDiff } from "@/components/enrichment/PolicyDiff"
import { usePlatform } from "@/contexts/PlatformContext"
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

/**
 * Editor de política em PÁGINA, não em modal.
 *
 * O modal anterior acumulava quatro responsabilidades — ligar/desligar, editar
 * regras, testar e histórico com rollback — e rolava por três telas. Três
 * consequências, todas caras:
 *
 * 1. **Sem URL.** Não dava para mandar "olha esta política" para um colega, e
 *    recarregar a página voltava para a lista.
 * 2. **Sem rascunho.** Fechar sem querer perdia tudo o que estava escrito, e o
 *    modal fechava com clique fora e com Esc.
 * 3. **Sem diff.** Publicar SUBSTITUI a lista inteira de regras. O editor abre
 *    hidratado da versão vigente justamente por isso, mas nada dizia o que a
 *    publicação ia mudar — o operador descobria pelo comportamento em produção.
 *
 * O editor de regras em si (`PolicyRuleEditor`) foi mantido: ele já resolve os
 * erros silenciosos da DSL (caminho de chave sugerido a partir dos mappings
 * ativos, prefixo de destino fixo, gate que compila e nunca casa). Trocá-lo
 * seria refazer trabalho bom para ganhar aparência.
 */

/** Chave do rascunho. Por POLÍTICA: dois rascunhos não podem se misturar. */
function draftKey(policyId: string): string {
  return `centralops:enrich:policy-draft:${policyId}`
}

interface StoredDraft {
  rules: EnrichmentRule[]
  /** Versão a partir da qual o rascunho foi feito. */
  baseVersionId: string | null
  savedAt: number
}

function readDraft(policyId: string): StoredDraft | null {
  try {
    const raw = window.localStorage.getItem(draftKey(policyId))
    if (!raw) return null
    const parsed = JSON.parse(raw) as StoredDraft
    if (!Array.isArray(parsed.rules)) return null
    return parsed
  } catch {
    // Armazenamento indisponível (janela privada, cota) não pode impedir a
    // edição: sem rascunho o editor volta a se comportar como o modal antigo.
    return null
  }
}

function writeDraft(policyId: string, draft: StoredDraft): void {
  try {
    window.localStorage.setItem(draftKey(policyId), JSON.stringify(draft))
  } catch {
    /* idem */
  }
}

function clearDraft(policyId: string): void {
  try {
    window.localStorage.removeItem(draftKey(policyId))
  } catch {
    /* idem */
  }
}

const SAMPLE_PLACEHOLDER = `{
  "_centralops": { "organization_id": 1 },
  "normalized": { "src_endpoint": { "ip": "10.0.5.7" } },
  "raw": {}
}`

export function EnrichmentPolicyPage(): React.ReactElement {
  const { t } = useTranslation("enrichment")
  const { id: policyId = "" } = useParams()
  const navigate = useNavigate()
  const { organizations } = usePlatform()

  const [policy, setPolicy] = useState<EnrichmentPolicy | null>(null)
  const [enrichers, setEnrichers] = useState<EnricherCatalogItem[]>([])
  const [tables, setTables] = useState<EnrichmentTable[]>([])
  const [sources, setSources] = useState<EnrichmentSource[]>([])
  const [versions, setVersions] = useState<EnrichmentPolicyVersion[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  /** Regras no editor. */
  const [rules, setRules] = useState<EnrichmentRule[]>([])
  /** Regras da versão VIGENTE, congeladas — é contra elas que o diff compara. */
  const [publishedRules, setPublishedRules] = useState<EnrichmentRule[]>([])
  const [loadedVersionNumber, setLoadedVersionNumber] = useState<number | null>(null)
  const [restoredDraft, setRestoredDraft] = useState(false)

  const [keySources, setKeySources] = useState<string[]>([])
  const [keySourcesFromMappings, setKeySourcesFromMappings] = useState(false)

  const [commitMessage, setCommitMessage] = useState("")
  const [publishing, setPublishing] = useState(false)
  const [publishError, setPublishError] = useState<string | null>(null)
  const [publishedNotice, setPublishedNotice] = useState<string | null>(null)
  const [togglingEnabled, setTogglingEnabled] = useState(false)
  const [toggleError, setToggleError] = useState<string | null>(null)
  const [rollingBackId, setRollingBackId] = useState<string | null>(null)
  const [showHistory, setShowHistory] = useState(false)

  const [sampleText, setSampleText] = useState(SAMPLE_PLACEHOLDER)
  const [sampleLabel, setSampleLabel] = useState<string | null>(null)
  /**
   * Origens de amostra real: o reservoir é indexado por vendor + tipo de
   * evento, não é um feed genérico de "últimos eventos". Listar os mapeamentos
   * ATIVOS é o que transforma isso numa escolha curta e verdadeira, em vez de
   * uma caixa onde o operador teria que adivinhar o par certo.
   */
  const [sampleSources, setSampleSources] = useState<
    Array<{ vendor: string; event_type: string }>
  >([])
  const [samplePick, setSamplePick] = useState("")
  const [loadingSample, setLoadingSample] = useState(false)
  const [dryRunning, setDryRunning] = useState(false)
  const [dryRunError, setDryRunError] = useState<string | null>(null)
  const [dryRunResult, setDryRunResult] = useState<EnrichmentDryRunResponse | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [policies, cat, tbs, srcs, maps] = await Promise.all([
        api.listEnrichmentPolicies(),
        api.listEnrichers(),
        api.listEnrichmentTables(),
        api.listEnrichmentSources(),
        // Falhar aqui só tira o atalho de amostra real; o teste com JSON
        // colado segue funcionando.
        api.listMappings({ only_active: true }).catch(() => []),
      ])
      const found = policies.find((p) => p.id === policyId) ?? null
      setPolicy(found)
      setEnrichers(cat)
      setTables(tbs)
      setSources(srcs)
      const origens = maps.map((m) => ({ vendor: m.vendor, event_type: m.event_type }))
      setSampleSources(origens)
      if (origens.length > 0) {
        setSamplePick(`${origens[0].vendor}|${origens[0].event_type}`)
      }
      if (!found) {
        setError(t("policies.page.notFound"))
        return
      }

      const vs = await api.listEnrichmentPolicyVersions(found.id)
      setVersions(vs)

      let current: EnrichmentRule[] = []
      let currentNumber: number | null = null
      if (found.current_version_id) {
        const v = await api.getEnrichmentPolicyVersion(found.id, found.current_version_id)
        current = v.rules ?? []
        currentNumber = v.version_number
      }
      setPublishedRules(current)
      setLoadedVersionNumber(currentNumber)

      // Rascunho vence a versão publicada — foi o que o operador escreveu por
      // último. Só é adotado quando parte da MESMA versão: um rascunho feito
      // sobre a v2 e aplicado por cima da v3 apagaria o que a v3 trouxe.
      const draft = readDraft(found.id)
      if (draft && draft.baseVersionId === (found.current_version_id ?? null)) {
        setRules(draft.rules)
        setRestoredDraft(true)
      } else {
        if (draft) clearDraft(found.id)
        setRules(current)
        setRestoredDraft(false)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [policyId, t])

  useEffect(() => {
    void load()
  }, [load])

  // Sugestões de caminho vêm da org DA POLÍTICA: um admin global editando a
  // política do cliente X tem que ver os campos do X.
  useEffect(() => {
    if (!policy) return
    let cancelled = false
    api
      .listEnrichmentKeySources({ organization_id: policy.organization_id })
      .then((res) => {
        if (cancelled) return
        setKeySources(res.suggestions.map((s) => s.path))
        setKeySourcesFromMappings(res.from_active_mappings)
      })
      .catch(() => {
        if (!cancelled) {
          setKeySources([])
          setKeySourcesFromMappings(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [policy])

  const dirty = useMemo(
    () => JSON.stringify(rules) !== JSON.stringify(publishedRules),
    [rules, publishedRules],
  )

  // Grava o rascunho a cada mudança. Em efeito e com guarda de "sujo": gravar
  // um rascunho idêntico ao publicado faria a página anunciar alterações que
  // não existem toda vez que ela abrisse.
  const skipFirstSave = useRef(true)
  useEffect(() => {
    if (!policy) return
    if (skipFirstSave.current) {
      skipFirstSave.current = false
      return
    }
    if (dirty) {
      writeDraft(policy.id, {
        rules,
        baseVersionId: policy.current_version_id ?? null,
        savedAt: Date.now(),
      })
    } else {
      clearDraft(policy.id)
      setRestoredDraft(false)
    }
  }, [rules, dirty, policy])

  async function handleUseRealSample() {
    if (!policy) return
    setLoadingSample(true)
    setDryRunError(null)
    try {
      // O reservoir de amostras já existe e alimenta o editor de mapeamento.
      // Usá-lo aqui elimina o erro mais comum do teste: inventar o evento e
      // errar o caminho do campo, que faz a regra parecer quebrada quando
      // quem estava errado era o exemplo.
      const [vendor, eventType] = samplePick.split("|")
      if (!vendor || !eventType) {
        setDryRunError(t("policies.page.noSamples"))
        return
      }
      // `org_id` explícito: o reservoir é escopado por organização e um admin
      // GLOBAL tem `organization_id` nulo — sem o parâmetro, a resposta volta
      // vazia e pareceria "não há evento", quando o que faltou foi o escopo.
      const res = await api.getMappingSamples({
        vendor,
        event_type: eventType,
        limit: 1,
        org_id: policy.organization_id,
      })
      const first = res.items?.[0]
      if (!first) {
        setDryRunError(t("policies.page.noSamples"))
        return
      }
      // O dry-run recebe o envelope como o pipeline o monta: o evento do
      // fornecedor entra sob `raw`, e o caminho da chave da regra aponta para
      // `normalized`, que o mapeamento produz. Mandar o bruto na raiz faria a
      // regra "não casar" por um motivo que não é o dela.
      setSampleText(
        JSON.stringify(
          {
            _centralops: { organization_id: policy.organization_id },
            normalized: (first as Record<string, unknown>).normalized ?? {},
            raw: first,
          },
          null,
          2,
        ),
      )
      setSampleLabel(`${vendor} · ${eventType}`)
    } catch (err) {
      setDryRunError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoadingSample(false)
    }
  }

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
    setDryRunning(true)
    try {
      setDryRunResult(await api.dryRunEnrichment({ rules, sample }))
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
      setPublishedNotice(
        t("policies.versions.publishedDetail", { count: result.summary?.rule_count ?? 0 }),
      )
      // O que acabou de ser publicado vira a nova base do diff, e o rascunho
      // deixa de existir: mantê-lo faria a página anunciar alterações
      // pendentes segundos depois de publicá-las.
      setPublishedRules(rules)
      setLoadedVersionNumber(result.version_number ?? null)
      // Redundante e deliberado: ao igualar `publishedRules` a `rules`, o efeito
      // de rascunho já vê "não sujo" e limpa. Manter a chamada aqui mantém a
      // garantia dentro do fluxo de publicação, em vez de fazê-la depender das
      // dependências de um efeito que alguém pode reescrever depois. A mutação
      // deste par mostra o dobro: remover QUALQUER um dos dois deixa a
      // propriedade de pé — que é a definição de defesa em profundidade.
      clearDraft(policy.id)
      setRestoredDraft(false)
      setCommitMessage("")
      const [fresh, vs] = await Promise.all([
        api.listEnrichmentPolicies(),
        api.listEnrichmentPolicyVersions(policy.id),
      ])
      setPolicy(fresh.find((p) => p.id === policy.id) ?? policy)
      setVersions(vs)
    } catch (err) {
      setPublishError(err instanceof Error ? err.message : String(err))
    } finally {
      setPublishing(false)
    }
  }

  async function handleToggleEnabled() {
    if (!policy) return
    setTogglingEnabled(true)
    setToggleError(null)
    try {
      await api.setEnrichmentPolicyEnabled(policy.id, !policy.enabled)
      const fresh = await api.listEnrichmentPolicies()
      setPolicy(fresh.find((p) => p.id === policy.id) ?? policy)
    } catch (err) {
      setToggleError(err instanceof Error ? err.message : String(err))
    } finally {
      setTogglingEnabled(false)
    }
  }

  async function handleRollback(versionId: string) {
    if (!policy) return
    setRollingBackId(versionId)
    try {
      await api.rollbackEnrichmentPolicy(policy.id, versionId)
      await load()
    } catch (err) {
      setToggleError(err instanceof Error ? err.message : String(err))
    } finally {
      setRollingBackId(null)
    }
  }

  async function handleLoadVersion(versionId: string) {
    if (!policy) return
    try {
      const v = await api.getEnrichmentPolicyVersion(policy.id, versionId)
      setRules(v.rules ?? [])
      setShowHistory(false)
    } catch (err) {
      setToggleError(err instanceof Error ? err.message : String(err))
    }
  }

  if (loading) return <SkeletonCard />
  if (error || !policy) {
    return (
      <ErrorState
        title={t("errorTitle")}
        message={error ?? t("policies.page.notFound")}
        onRetry={() => void load()}
      />
    )
  }

  const orgLabel =
    organizations.find((o) => o.id === policy.organization_id)?.name ??
    t("tables.org", { id: policy.organization_id })

  return (
    <div className="space-y-4 pb-24">
      <PageHeader
        title={policy.name}
        description={policy.description ?? t("policies.page.subtitle", { org: orgLabel })}
        actions={
          <>
            <Badge variant={policy.is_active ? "success" : policy.enabled ? "warning" : "default"}>
              {policy.is_active
                ? t("policies.active")
                : policy.enabled
                  ? t("policies.shadowed")
                  : t("policies.disabled")}
            </Badge>
            {loadedVersionNumber != null && (
              <Badge variant="default" data-testid="editing-from-version">
                {t("policies.versions.editingFrom", { version: loadedVersionNumber })}
              </Badge>
            )}
            {dirty && (
              <Badge variant="warning" data-testid="unpublished-badge">
                {t("policies.page.unpublished")}
              </Badge>
            )}
            <Button
              variant={policy.enabled ? "outline" : "primary"}
              loading={togglingEnabled}
              disabled={!policy.enabled && !policy.current_version_id}
              onClick={handleToggleEnabled}
            >
              {policy.enabled
                ? t("policies.versions.disable")
                : t("policies.versions.enable")}
            </Button>
            <Button
              variant="secondary"
              onClick={() => setShowHistory((v) => !v)}
              leftIcon={<HistoryIcon size={16} />}
            >
              {t("policies.page.history", { count: versions.length })}
            </Button>
            <Button
              variant="ghost"
              onClick={() => navigate("/enrichment")}
              leftIcon={<ArrowLeftIcon size={16} />}
            >
              {t("policies.page.back")}
            </Button>
          </>
        }
      />

      {toggleError && <Notice variant="danger" title={toggleError} />}
      {publishedNotice && (
        <Notice variant="success" title={t("policies.versions.published")}>
          {publishedNotice}
        </Notice>
      )}
      {restoredDraft && (
        // Sem dizer isto, o operador acha que está vendo o que está em
        // produção — e publica um rascunho que ele já tinha esquecido.
        <Notice variant="info" title={t("policies.page.draftRestoredTitle")}>
          {t("policies.page.draftRestoredBody")}
        </Notice>
      )}
      {!policy.current_version_id && (
        <Notice variant="warning" title={t("policies.versions.needsVersionToEnable")} />
      )}
      {policy.enabled && !policy.is_active && (
        <Notice variant="warning" title={t("policies.versions.shadowedNow")} />
      )}

      {showHistory && (
        <Card>
          <div className="space-y-2 p-4">
            <h3 className="text-sm font-semibold">{t("tables.versions.history")}</h3>
            {versions.length === 0 ? (
              <p className="text-sm text-muted">{t("tables.versions.empty")}</p>
            ) : (
              <ul className="divide-y divide-border">
                {versions.map((v) => (
                  <li key={v.id} className="flex items-center justify-between gap-3 py-2">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-sm">v{v.version_number}</span>
                        {v.is_current && (
                          <Badge variant="success">{t("tables.versions.current")}</Badge>
                        )}
                      </div>
                      <p className="truncate text-sm text-muted">{v.commit_message}</p>
                    </div>
                    <div className="flex shrink-0 gap-2">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleLoadVersion(v.id)}
                        data-testid={`load-version-${v.version_number}`}
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
        </Card>
      )}

      <div className="grid gap-4 xl:grid-cols-[1fr_400px]">
        <Card>
          <div className="p-4">
            <PolicyRuleEditor
              rules={rules}
              enrichers={enrichers}
              tables={tables}
              sources={sources}
              keySources={keySources}
              keySourcesFromMappings={keySourcesFromMappings}
              onChange={setRules}
            />
          </div>
        </Card>

        <div className="space-y-4">
          <Card>
            <div className="space-y-3 p-4">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold">
                  {t("policies.versions.dryRun.title")}
                </h3>
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
              {sampleSources.length > 0 && (
                <div className="flex flex-wrap items-end gap-2">
                  <div className="min-w-[180px] flex-1">
                    <Select
                      label={t("policies.page.sampleSource")}
                      value={samplePick}
                      onValueChange={(v) => setSamplePick(String(v))}
                      options={sampleSources.map((o) => ({
                        value: `${o.vendor}|${o.event_type}`,
                        label: `${o.vendor} · ${o.event_type}`,
                      }))}
                      size="sm"
                    />
                  </div>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    loading={loadingSample}
                    onClick={handleUseRealSample}
                  >
                    {t("policies.page.useRealSample")}
                  </Button>
                </div>
              )}
              {sampleLabel && (
                <Badge variant="default" data-testid="sample-label">
                  {sampleLabel}
                </Badge>
              )}
              <Textarea
                label={t("policies.versions.dryRun.sample")}
                value={sampleText}
                onChange={(e) => setSampleText(e.target.value)}
                rows={8}
                className="font-mono text-xs"
              />
              {dryRunError && <Notice variant="danger" title={dryRunError} />}
              {dryRunResult && (
                <div className="space-y-2" data-testid="dry-run-result">
                  <div className="flex flex-wrap gap-2">
                    <Badge variant="default">
                      {t("policies.versions.dryRun.bytesAdded", {
                        count: dryRunResult.bytes_added,
                      })}
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
          </Card>
        </div>
      </div>

      {/* Rodapé fixo: o diff e a publicação ficam sempre à vista, porque a
          decisão de publicar depende de enxergar o que muda. */}
      <form
        onSubmit={handlePublish}
        className="fixed inset-x-0 bottom-0 z-20 border-t border-border bg-surface-secondary/95 px-6 py-3 backdrop-blur"
      >
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-3">
          <PolicyDiff published={publishedRules} draft={rules} />
          <Input
            aria-label={t("tables.versions.commitMessage")}
            value={commitMessage}
            onChange={(e) => setCommitMessage(e.target.value)}
            placeholder={t("tables.versions.commitMessagePlaceholder")}
            className="min-w-[240px] flex-1"
          />
          {publishError && (
            <span className="text-xs text-danger-500">{publishError}</span>
          )}
          <Button
            type="button"
            variant="outline"
            disabled={!dirty || publishing}
            onClick={() => {
              setRules(publishedRules)
              if (policy) clearDraft(policy.id)
              setRestoredDraft(false)
            }}
          >
            {t("policies.page.discardDraft")}
          </Button>
          <Button type="submit" variant="primary" loading={publishing}>
            {t("policies.page.publishVersion", {
              version: (loadedVersionNumber ?? 0) + 1,
            })}
          </Button>
        </div>
      </form>
    </div>
  )
}

export default EnrichmentPolicyPage
