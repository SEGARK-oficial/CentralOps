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
import { useFirstInvalidFocus } from "@/hooks/useFirstInvalidFocus"
import * as api from "@/services/api"
import type { EnrichmentPolicy } from "@/services/api"

// `Select` não encaminha `ref` (não é forwardRef) — `registerField` só cobre
// `Input`/`Textarea`. Para o Select, o id explícito + `document.getElementById`
// é o jeito de focar o trigger sem reescrever o componente.
const ORG_SELECT_ID = "create-policy-org"

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
  const errorId = "create-policy-modal-error"
  const { error, errorField, registerField, failField, failGeneral, clearError } = useFirstInvalidFocus<
    "name" | "organizationId"
  >()

  useEffect(() => {
    if (open) setOrganizationId(selectedOrgId)
    // Só na ABERTURA do modal — `selectedOrgId` mudando com o modal já aberto
    // não deve pisar a escolha que o operador já fez no formulário.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  function reset() {
    setName("")
    setDescription("")
    clearError()
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
      failField("name", t("policies.form.nameRequired"))
      return
    }
    if (organizationId == null) {
      failField("organizationId", t("policies.form.organizationRequired"))
      // Select não encaminha ref — foca o trigger pelo id (ver comentário no topo do arquivo).
      document.getElementById(ORG_SELECT_ID)?.focus()
      return
    }
    setSubmitting(true)
    clearError()
    try {
      const policy = await api.createEnrichmentPolicy({
        name: name.trim(),
        organization_id: organizationId,
        description: description.trim() || null,
      })
      reset()
      onCreated(policy)
    } catch (err) {
      failGeneral(err instanceof Error ? err.message : String(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal open={open} onClose={handleClose} title={t("policies.form.createTitle")} size="md">
      <form onSubmit={handleSubmit} className="space-y-4" noValidate>
        {error && (
          <Notice id={errorId} variant="danger" title={error} live="assertive" />
        )}

        <Select
          id={ORG_SELECT_ID}
          label={t("tables.form.organization")}
          value={organizationId ?? ""}
          onValueChange={(v) => setOrganizationId(v === "" ? null : Number(v))}
          options={orgOptions}
          placeholder={t("tables.form.organizationPlaceholder")}
          error={errorField === "organizationId" ? error ?? undefined : undefined}
          aria-describedby={errorField === "organizationId" ? errorId : undefined}
        />

        <Input
          ref={registerField("name")}
          label={t("policies.form.name")}
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
          autoFocus
          placeholder={t("policies.form.namePlaceholder")}
          aria-invalid={errorField === "name" ? "true" : undefined}
          aria-describedby={errorField === "name" ? errorId : undefined}
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
