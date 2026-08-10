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
import type { EnrichmentTable } from "@/services/api"

interface CreateTableModalProps {
  open: boolean
  onClose: () => void
  onCreated: (table: EnrichmentTable) => void
}

/**
 * Cria uma tabela VAZIA. Ela só ganha conteúdo depois que uma versão é
 * publicada (ver {@link TableVersionsModal}) — os dois passos são
 * deliberadamente separados, mesmo modelo de `MappingDefinition`/`MappingVersion`.
 */
export const CreateTableModal: React.FC<CreateTableModalProps> = ({
  open,
  onClose,
  onCreated,
}) => {
  const { t } = useTranslation("enrichment")
  const { organizations, selectedOrgId } = usePlatform()
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const [matchMode, setMatchMode] = useState<"exact" | "cidr">("exact")
  const [keyKind, setKeyKind] = useState("ip")
  const [organizationId, setOrganizationId] = useState<number | null>(selectedOrgId)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Acompanha o filtro global ao abrir o modal — não sobrescreve uma escolha
  // que o usuário já fez dentro do próprio formulário.
  useEffect(() => {
    if (open) setOrganizationId(selectedOrgId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  function reset() {
    setName("")
    setDescription("")
    setMatchMode("exact")
    setKeyKind("ip")
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
      setError(t("tables.form.nameRequired"))
      return
    }
    if (organizationId == null) {
      // Não existe recurso de enriquecimento global — sem org selecionada
      // não há para onde publicar (ADR-LOCAL-0002 §0.8).
      setError(t("tables.form.organizationRequired"))
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      const table = await api.createEnrichmentTable({
        name: name.trim(),
        organization_id: organizationId,
        description: description.trim() || null,
        match_mode: matchMode,
        key_kind: keyKind,
      })
      reset()
      onCreated(table)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal open={open} onClose={handleClose} title={t("tables.form.createTitle")} size="md">
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
          label={t("tables.form.name")}
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
          autoFocus
          placeholder="rede-corporativa"
        />

        <Textarea
          label={t("tables.form.description")}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={2}
        />

        <Select
          label={t("tables.form.matchMode")}
          value={matchMode}
          onValueChange={(v) => setMatchMode(v as "exact" | "cidr")}
          options={[
            { value: "exact", label: t("tables.mode.exact") },
            { value: "cidr", label: t("tables.mode.cidr") },
          ]}
          helperText={matchMode === "cidr" ? t("tables.form.matchModeCidrHint") : t("tables.form.matchModeExactHint")}
        />

        {matchMode === "exact" && (
          <Select
            label={t("tables.form.keyKind")}
            value={keyKind}
            onValueChange={(v) => setKeyKind(String(v))}
            options={[
              "ip",
              "domain",
              "url",
              "file_hash",
              "cve",
              "mac",
              "user",
              "container_id",
            ].map((k) => ({ value: k, label: k }))}
          />
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="outline" onClick={handleClose} disabled={submitting}>
            {t("common:actions.cancel")}
          </Button>
          <Button type="submit" variant="primary" loading={submitting}>
            {t("tables.form.create")}
          </Button>
        </div>
      </form>
    </Modal>
  )
}

export default CreateTableModal
