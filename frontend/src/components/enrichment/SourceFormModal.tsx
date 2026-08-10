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
  enrichers: EnricherCatalogItem[]
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
  enrichers,
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
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

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
    } else {
      setName("")
      setEnricher(enrichers[0]?.name ?? "")
      setDescription("")
      setConfig({})
      setEnabled(true)
      setOrganizationId(selectedOrgId)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, source?.id])

  const selected = useMemo(
    () => enrichers.find((e) => e.name === enricher),
    [enrichers, enricher],
  )
  const needsSecret = (selected?.required_secrets?.length ?? 0) > 0

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
          })
        : await api.createEnrichmentSource({
            name: name.trim(),
            enricher,
            organization_id: organizationId,
            description: description.trim() || null,
            config,
            ...(secret.trim() ? { secret: secret.trim() } : {}),
            enabled,
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
