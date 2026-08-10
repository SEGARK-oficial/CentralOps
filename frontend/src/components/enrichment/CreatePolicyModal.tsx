import type React from "react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Modal } from "@/components/ui/Modal/Modal"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Select } from "@/components/ui/Select/Select"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import { Notice } from "@/components/ui/Notice/Notice"
import { usePlatform } from "@/contexts/PlatformContext"
import * as api from "@/services/api"
import type { EnrichmentPolicy } from "@/services/api"

interface CreatePolicyModalProps {
  open: boolean
  onClose: () => void
  onCreated: (policy: EnrichmentPolicy) => void
}

/**
 * Cria uma política DESLIGADA e sem versão — criar não habilita (mesmo modelo
 * das tabelas): publicar a primeira versão e habilitar são passos distintos,
 * feitos em {@link PolicyVersionsModal}.
 */
export const CreatePolicyModal: React.FC<CreatePolicyModalProps> = ({
  open,
  onClose,
  onCreated,
}) => {
  const { t } = useTranslation("enrichment")
  const { organizations, selectedOrgId } = usePlatform()
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const [organizationId, setOrganizationId] = useState<number | null>(selectedOrgId)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open) setOrganizationId(selectedOrgId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  function reset() {
    setName("")
    setDescription("")
    setError(null)
  }

  const orgOptions = organizations.map((o) => ({ value: o.id, label: o.name }))

  function handleClose() {
    if (submitting) return
    reset()
    onClose()
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!name.trim()) {
      setError(t("policies.form.nameRequired"))
      return
    }
    if (organizationId == null) {
      setError(t("policies.form.organizationRequired"))
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      const policy = await api.createEnrichmentPolicy({
        name: name.trim(),
        organization_id: organizationId,
        description: description.trim() || null,
      })
      reset()
      onCreated(policy)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal open={open} onClose={handleClose} title={t("policies.form.createTitle")} size="md">
      <form onSubmit={handleSubmit} className="space-y-4" noValidate>
        {error && <Notice variant="danger" title={error} />}

        <Select
          label={t("tables.form.organization")}
          value={organizationId ?? ""}
          onValueChange={(v) => setOrganizationId(v === "" ? null : Number(v))}
          options={orgOptions}
          placeholder={t("tables.form.organizationPlaceholder")}
        />

        <Input
          label={t("policies.form.name")}
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
          autoFocus
          placeholder="contexto-de-ativo"
        />

        <Textarea
          label={t("policies.form.description")}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={2}
        />

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="outline" onClick={handleClose} disabled={submitting}>
            {t("common:actions.cancel")}
          </Button>
          <Button type="submit" variant="primary" loading={submitting}>
            {t("policies.form.create")}
          </Button>
        </div>
      </form>
    </Modal>
  )
}

export default CreatePolicyModal
