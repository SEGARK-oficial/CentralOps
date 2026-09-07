import type React from "react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Modal } from "@/components/ui/Modal/Modal"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Notice } from "@/components/ui/Notice/Notice"
import { Select } from "@/components/ui/Select/Select"
import * as api from "@/services/api"
import type {
  EnrichmentDuplicatePreflight,
  EnrichmentPolicy,
} from "@/services/api"

/**
 * Copia as regras de uma política para OUTRA organização.
 *
 * Não é compartilhamento: cada organização fica com a própria política, com
 * versionamento e rollback próprios. Funciona porque a regra cita tabela e
 * fonte **por nome**, nunca por id — o mesmo documento vale em qualquer
 * organização que tenha os nomes correspondentes.
 *
 * O passo que dá sentido à tela é o preflight. Sem ele, a política nasceria
 * válida no destino e a carga da tabela falharia a cada ciclo, num log de
 * worker que ninguém lê, com os eventos saindo sem contexto e sem erro em tela
 * nenhuma. Por isso a verificação roda ao escolher o destino, e não só ao
 * clicar em copiar.
 */

interface Props {
  open: boolean
  policy: EnrichmentPolicy | null
  organizations: Array<{ id: number; name: string }>
  onClose: () => void
  onDuplicated: (created: EnrichmentPolicy) => void
}

export const DuplicatePolicyModal: React.FC<Props> = ({
  open,
  policy,
  organizations,
  onClose,
  onDuplicated,
}) => {
  const { t } = useTranslation("enrichment")
  const [targetId, setTargetId] = useState<number | null>(null)
  const [name, setName] = useState("")
  const [preflight, setPreflight] = useState<EnrichmentDuplicatePreflight | null>(null)
  const [checking, setChecking] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const candidates = organizations.filter((o) => o.id !== policy?.organization_id)

  useEffect(() => {
    if (!open) return
    setTargetId(candidates[0]?.id ?? null)
    setName(policy?.name ?? "")
    setPreflight(null)
    setError(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, policy?.id])

  // Verifica ao escolher o destino e ao trocar o nome. Esperar o clique em
  // copiar transformaria a verificação num erro, quando ela é uma orientação.
  useEffect(() => {
    if (!open || !policy || targetId == null) return
    let cancelled = false
    setChecking(true)
    api
      .preflightDuplicateEnrichmentPolicy(policy.id, {
        target_organization_id: targetId,
        ...(name.trim() ? { name: name.trim() } : {}),
      })
      .then((res) => {
        if (!cancelled) setPreflight(res)
      })
      .catch((err) => {
        if (!cancelled) {
          setPreflight(null)
          setError(err instanceof Error ? err.message : String(err))
        }
      })
      .finally(() => {
        if (!cancelled) setChecking(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, policy, targetId, name])

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!policy || targetId == null) return
    setSubmitting(true)
    setError(null)
    try {
      const created = await api.duplicateEnrichmentPolicy(policy.id, {
        target_organization_id: targetId,
        ...(name.trim() ? { name: name.trim() } : {}),
        commit_message: t("policies.duplicate.commitMessage", { name: policy.name }),
      })
      onDuplicated(created)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSubmitting(false)
    }
  }

  if (!policy) return null

  const blocked = preflight != null && !preflight.ok

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t("policies.duplicate.title", { name: policy.name })}
      size="md"
    >
      <form onSubmit={handleSubmit} className="space-y-4" noValidate>
        {error && <Notice variant="danger" title={error} />}

        {candidates.length === 0 ? (
          <Notice variant="info" title={t("policies.duplicate.noTargets")} />
        ) : (
          <>
            <Select
              label={t("policies.duplicate.target")}
              value={targetId != null ? String(targetId) : ""}
              onValueChange={(v) => setTargetId(Number(v))}
              options={candidates.map((o) => ({ value: String(o.id), label: o.name }))}
            />
            <Input
              label={t("policies.duplicate.name")}
              value={name}
              onChange={(e) => setName(e.target.value)}
              helperText={t("policies.duplicate.nameHint")}
            />

            {checking && (
              <p className="text-xs text-muted">{t("policies.duplicate.checking")}</p>
            )}

            {preflight && !checking && (
              <div data-testid="duplicate-preflight">
                {preflight.ok ? (
                  <Notice variant="success" title={t("policies.duplicate.readyTitle")}>
                    {t("policies.duplicate.readyBody")}
                  </Notice>
                ) : (
                  <Notice variant="danger" title={t("policies.duplicate.blockedTitle")}>
                    <ul className="mt-1 list-disc space-y-1 pl-4 text-xs">
                      {preflight.name_conflict && (
                        <li>{t("policies.duplicate.nameConflict", { name })}</li>
                      )}
                      {preflight.missing_tables.length > 0 && (
                        <li>
                          {t("policies.duplicate.missingTables", {
                            names: preflight.missing_tables.join(", "),
                          })}
                        </li>
                      )}
                      {preflight.missing_sources.length > 0 && (
                        <li>
                          {t("policies.duplicate.missingSources", {
                            names: preflight.missing_sources.join(", "),
                          })}
                        </li>
                      )}
                    </ul>
                  </Notice>
                )}

                {/* Existir e estar publicada são coisas diferentes: a tabela
                    existir basta para a política ser válida, mas sem versão
                    publicada ela não funciona. Avisa, não bloqueia. */}
                {preflight.tables_without_version.length > 0 && (
                  <Notice
                    variant="warning"
                    title={t("policies.duplicate.tablesWithoutVersionTitle")}
                  >
                    {t("policies.duplicate.tablesWithoutVersionBody", {
                      names: preflight.tables_without_version.join(", "),
                    })}
                  </Notice>
                )}
              </div>
            )}

            <Notice variant="info" title={t("policies.duplicate.disabledTitle")}>
              {t("policies.duplicate.disabledBody")}
            </Notice>
          </>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="outline" onClick={onClose} disabled={submitting}>
            {t("common:actions.cancel")}
          </Button>
          <Button
            type="submit"
            variant="primary"
            loading={submitting}
            disabled={candidates.length === 0 || blocked || checking}
          >
            {t("policies.duplicate.confirm")}
          </Button>
        </div>
      </form>
    </Modal>
  )
}

export default DuplicatePolicyModal
