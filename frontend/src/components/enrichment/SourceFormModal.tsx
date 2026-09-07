import type React from "react"
import { useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { Modal } from "@/components/ui/Modal/Modal"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Select } from "@/components/ui/Select/Select"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import { Notice } from "@/components/ui/Notice/Notice"
import { JsonSchemaForm } from "@/components/destinations/JsonSchemaForm"
import { usePlatform } from "@/contexts/PlatformContext"
import * as api from "@/services/api"
import type { EnricherCatalogItem, EnrichmentSource } from "@/services/api"

interface SourceFormModalProps {
  open: boolean
  /** `null` = criar; preenchido = editar. */
  source: EnrichmentSource | null
  /** Enricher já escolhido na galeria do catálogo. */
  preselectEnricher?: string | null
  enrichers: EnricherCatalogItem[]
  /** Para escolher as filhas que também usam esta fonte (MSP). */
  organizations?: Array<{ id: number; name: string }>
  onClose: () => void
  onSaved: (source: EnrichmentSource) => void
}

/**
 * Cria/edita uma FONTE CONFIGURADA — a instância de um enricher nesta org.
 *
 * É o análogo de `DestinationForm`: o registry diz o que o *kind* sabe fazer
 * (`config_schema`, `required_secrets`) e esta tela diz com que credencial e
 * contra que endpoint ele roda AQUI. Por isso o formulário de config é dirigido
 * pelo schema — adicionar um enricher novo não toca este arquivo.
 *
 * **A credencial é write-only.** Ela sobe em claro uma única vez, o servidor
 * cifra, e a API nunca a devolve — só `secret_configured`. Na edição, deixar o
 * campo vazio MANTÉM o segredo atual; não é possível lê-lo de volta pela UI,
 * porque o cofre não conhece organização e devolver a referência deixaria colá-la
 * noutra org.
 */
export const SourceFormModal: React.FC<SourceFormModalProps> = ({
  open,
  source,
  preselectEnricher,
  enrichers,
  organizations: orgsProp,
  onClose,
  onSaved,
}) => {
  const { t } = useTranslation("enrichment")
  const { organizations, selectedOrgId } = usePlatform()
  const isEdit = source != null

  const [name, setName] = useState("")
  const [enricher, setEnricher] = useState("")
  const [description, setDescription] = useState("")
  const [config, setConfig] = useState<Record<string, unknown>>({})
  const [secret, setSecret] = useState("")
  const [enabled, setEnabled] = useState(true)
  const [organizationId, setOrganizationId] = useState<number | null>(selectedOrgId)
  const [sharedIds, setSharedIds] = useState<number[]>([])
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<api.EnrichmentSourceTestResult | null>(null)
  const [egressAck, setEgressAck] = useState(false)

  // Depende de `source?.id`, não de `source`: o pai recria o objeto a cada
  // recarga da lista, e depender da identidade resetaria o que está sendo
  // digitado a cada refresh.
  useEffect(() => {
    if (!open) return
    setError(null)
    setSecret("")
    if (source) {
      setName(source.name)
      setEnricher(source.enricher)
      setDescription(source.description ?? "")
      setConfig(source.config ?? {})
      setEnabled(source.enabled)
      setOrganizationId(source.organization_id)
      setSharedIds(source.shared_organization_ids ?? [])
    } else {
      setName("")
      setEnricher(enrichers[0]?.name ?? "")
      setDescription("")
      setConfig({})
      setEnabled(true)
      setOrganizationId(selectedOrgId)
      setSharedIds([])
      if (preselectEnricher) setEnricher(preselectEnricher)
    }
    setTestResult(null)
    setEgressAck(source != null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, source?.id])

  const selected = useMemo(
    () => enrichers.find((e) => e.name === enricher),
    [enrichers, enricher],
  )
  const needsSecret = (selected?.required_secrets?.length ?? 0) > 0
  /**
   * Egresso a terceiro exige consentimento EXPLÍCITO nesta tela.
   *
   * O selo no catálogo diz que a fonte envia indicadores para fora, mas dizer
   * não é o mesmo que obter consentimento. Cadastrar uma credencial de
   * VirusTotal é a decisão que de fato manda IP e hash do cliente para um
   * terceiro, e em ambiente regulado ou sob NDA essa decisão precisa ser
   * deliberada — não um efeito colateral de preencher um formulário.
   *
   * Na EDIÇÃO a caixa já vem marcada: o consentimento foi dado quando a fonte
   * foi criada, e reapresentá-lo em branco faria toda edição de porta parecer
   * uma autorização nova.
   */
  const sendsToThirdParty = selected?.egress === "third_party"

  function handleClose() {
    if (submitting) return
    onClose()
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!name.trim()) {
      setError(t("sources.form.nameRequired"))
      return
    }
    if (organizationId == null) {
      setError(t("sources.form.organizationRequired"))
      return
    }
    if (!isEdit && needsSecret && !secret.trim()) {
      setError(t("sources.form.secretRequired"))
      return
    }
    if (sendsToThirdParty && !egressAck) {
      setError(t("sources.form.egressRequired"))
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      const saved = isEdit
        ? await api.updateEnrichmentSource(source!.id, {
            description: description.trim() || null,
            config,
            // Vazio na edição = MANTER. Enviar "" apagaria a credencial.
            ...(secret.trim() ? { secret: secret.trim() } : {}),
            enabled,
            shared_organization_ids: sharedIds,
          })
        : await api.createEnrichmentSource({
            name: name.trim(),
            enricher,
            organization_id: organizationId,
            description: description.trim() || null,
            config,
            ...(secret.trim() ? { secret: secret.trim() } : {}),
            enabled,
            shared_organization_ids: sharedIds,
          })
      setSecret("")
      onSaved(saved)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSubmitting(false)
    }
  }

  const orgOptions = organizations.map((o) => ({ value: o.id, label: o.name }))
  const enricherOptions = enrichers.map((e) => ({
    value: e.name,
    label: `${e.label} (${e.name})`,
  }))

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title={isEdit ? t("sources.form.editTitle", { name: source?.name }) : t("sources.form.createTitle")}
      size="lg"
    >
      <form onSubmit={handleSubmit} className="space-y-4" noValidate>
        {error && <Notice variant="danger" title={error} />}

        {!isEdit && (
          <Select
            label={t("tables.form.organization")}
            value={organizationId ?? ""}
            onValueChange={(v) => setOrganizationId(v === "" ? null : Number(v))}
            options={orgOptions}
            placeholder={t("tables.form.organizationPlaceholder")}
          />
        )}

        <Input
          label={t("sources.form.name")}
          value={name}
          onChange={(e) => setName(e.target.value)}
          disabled={isEdit}
          required
          placeholder="opencti-interno"
          helperText={isEdit ? t("sources.form.nameImmutable") : undefined}
        />

        <Select
          label={t("sources.form.enricher")}
          value={enricher}
          onValueChange={(v) => setEnricher(String(v))}
          options={enricherOptions}
          disabled={isEdit}
        />

        <Textarea
          label={t("sources.form.description")}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={2}
        />

        {selected?.config_schema ? (
          <fieldset className="space-y-3 rounded-lg border border-border p-4">
            <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-muted">
              {t("sources.form.configLegend")}
            </legend>
            <JsonSchemaForm
              schema={selected.config_schema}
              values={config}
              onChange={setConfig}
              disabled={submitting}
              idPrefix="enrich-source-config"
            />
          </fieldset>
        ) : null}

        {needsSecret && (
          <Input
            label={t("sources.form.secret")}
            type="password"
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            autoComplete="off"
            placeholder={
              isEdit && source?.secret_configured
                ? t("sources.form.secretKeepPlaceholder")
                : undefined
            }
            helperText={t("sources.form.secretHint", {
              names: (selected?.required_secrets ?? []).join(", "),
            })}
          />
        )}

        {/* Consentimento de egresso. Vem DEPOIS da credencial de propósito: é
            ao cadastrar a chave que a decisão de mandar indicador para fora
            passa a valer, e é aí que a pergunta faz sentido. */}
        {sendsToThirdParty && (
          <label
            className="flex items-start gap-3 rounded-lg border border-warning-500/40 bg-warning-500/10 p-4"
            data-testid="egress-ack"
          >
            <input
              type="checkbox"
              className="mt-0.5"
              checked={egressAck}
              onChange={(e) => setEgressAck(e.target.checked)}
            />
            <span>
              <span className="block text-sm font-medium">
                {t("sources.form.egressAck", { name: selected?.label ?? enricher })}
              </span>
              <span className="mt-0.5 block text-xs text-muted">
                {t("sources.form.egressAckHint", {
                  kinds: (selected?.key_kinds ?? []).join(", "),
                })}
              </span>
            </span>
          </label>
        )}

        {/* Filhas que usam esta fonte. Só aparece quando há mais de uma org
            visível, e o backend recusa a lista sem a edição Enterprise. */}
        {(orgsProp?.length ?? 0) > 1 && organizationId != null && (
          <fieldset className="space-y-2 rounded-lg border border-border p-4">
            <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-muted">
              {t("sources.form.sharedOrgs")}
            </legend>
            <p className="text-xs text-muted">{t("sources.form.sharedOrgsHint")}</p>
            <div className="grid gap-1.5 sm:grid-cols-2">
              {(orgsProp ?? [])
                .filter((o) => o.id !== organizationId)
                .map((o) => (
                  <label key={o.id} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={sharedIds.includes(o.id)}
                      onChange={(e) =>
                        setSharedIds((prev) =>
                          e.target.checked
                            ? [...prev, o.id]
                            : prev.filter((x) => x !== o.id),
                        )
                      }
                    />
                    {o.name}
                  </label>
                ))}
            </div>
          </fieldset>
        )}

        {/* Testar vale ANTES de salvar. Sem isto, o único jeito de descobrir
            que a chave está errada era gravar, esperar o ciclo e ler a aba de
            Execução — ou seja, o operador só sabia que errou depois de a
            credencial já estar no banco e a política já publicada. */}
        {(
          <div className="space-y-2 rounded-lg border border-border p-4">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wide text-muted">
                {t("sources.test.title")}
              </span>
              <Button
                type="button"
                variant="outline"
                size="sm"
                loading={testing}
                onClick={async () => {
                  setTesting(true)
                  setTestResult(null)
                  try {
                    // Editando uma fonte SEM mexer na credencial, a sondagem
                    // com o registro salvo é a mais fiel: usa exatamente o que
                    // o worker usaria. Em qualquer outro caso (criação, ou
                    // credencial recém-digitada) vai pelo rascunho, que não
                    // grava nada.
                    const usaGravada = isEdit && !secret.trim()
                    setTestResult(
                      usaGravada
                        ? await api.testEnrichmentSource(source!.id)
                        : await api.testEnrichmentSourceDraft({
                            enricher,
                            organization_id: organizationId,
                            config,
                            ...(secret.trim() ? { secret: secret.trim() } : {}),
                            ...(isEdit && source ? { source_id: source.id } : {}),
                          }),
                    )
                  } catch (err) {
                    setTestResult({
                      ok: false,
                      message: err instanceof Error ? err.message : String(err),
                    })
                  } finally {
                    setTesting(false)
                  }
                }}
              >
                {t("sources.test.run")}
              </Button>
            </div>
            <p className="text-xs text-muted">
              {isEdit && !secret.trim()
                ? t("sources.test.hint")
                : t("sources.test.hintDraft")}
            </p>
            {testResult && (
              <Notice
                variant={testResult.ok ? "success" : "danger"}
                title={testResult.message}
              >
                {testResult.sample_count != null
                  ? t("sources.test.sampleCount", { count: testResult.sample_count })
                  : null}
              </Notice>
            )}
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="outline" onClick={handleClose} disabled={submitting}>
            {t("common:actions.cancel")}
          </Button>
          <Button type="submit" variant="primary" loading={submitting}>
            {isEdit ? t("common:actions.save") : t("sources.form.create")}
          </Button>
        </div>
      </form>
    </Modal>
  )
}

export default SourceFormModal
