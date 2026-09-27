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
import { useFirstInvalidFocus } from "@/hooks/useFirstInvalidFocus"

/** R4-8.4: os únicos dois campos do form — usado por `useFirstInvalidFocus`
 *  para saber em qual `Input`/`Select` focar e marcar `aria-invalid`. */
type DuplicateField = "name" | "target"

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
  const { error, errorField, registerField, failField, failGeneral, clearError } =
    useFirstInvalidFocus<DuplicateField>()

  const candidates = organizations.filter((o) => o.id !== policy?.organization_id)

  useEffect(() => {
    if (!open) return
    setTargetId(candidates[0]?.id ?? null)
    setName(policy?.name ?? "")
    setPreflight(null)
    clearError()
    // Só na abertura/troca de política — `candidates` (derivado de
    // `organizations`) mudando com o modal já aberto não deve resetar a
    // escolha do operador.
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
          failGeneral(err instanceof Error ? err.message : String(err))
        }
      })
      .finally(() => {
        if (!cancelled) setChecking(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, policy, targetId, name, failGeneral])

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!policy) return
    clearError()
    // R4-8.4: campo-a-campo, na ordem em que aparecem no form — required de
    // destino e de nome. `targetId` só é `null` estruturalmente impossível
    // enquanto há candidatos (o Select escolhe o primeiro na abertura), mas
    // guardamos mesmo assim em vez de um `return` mudo: submeter sem alvo não
    // pode falhar em silêncio.
    if (targetId == null) {
      failField("target", t("policies.duplicate.targetRequired"))
      return
    }
    const trimmedName = name.trim()
    if (!trimmedName) {
      failField("name", t("policies.duplicate.nameRequired"))
      return
    }
    setSubmitting(true)
    try {
      const created = await api.duplicateEnrichmentPolicy(policy.id, {
        target_organization_id: targetId,
        name: trimmedName,
        commit_message: t("policies.duplicate.commitMessage", { name: policy.name }),
      })
      onDuplicated(created)
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      // O backend não devolve o campo culpado estruturado — mas o preflight
      // já sabe se o bloqueio É de nome (roda com os MESMOS parâmetros da
      // duplicação de verdade). Corrida rara (nome mudou de válido pra
      // conflitante entre o último preflight e o clique) ainda assim aponta
      // pro campo certo em vez de só um banner genérico.
      if (preflight?.name_conflict) {
        failField("name", message)
      } else {
        failGeneral(message)
      }
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
        {error && <Notice variant="danger" title={error} live="assertive" />}

        {candidates.length === 0 ? (
          <Notice variant="info" title={t("policies.duplicate.noTargets")} />
        ) : (
          <>
            <Select
              ref={registerField("target")}
              label={t("policies.duplicate.target")}
              value={targetId != null ? String(targetId) : ""}
              onValueChange={(v) => setTargetId(Number(v))}
              options={candidates.map((o) => ({ value: String(o.id), label: o.name }))}
              error={errorField === "target" ? error ?? undefined : undefined}
            />
            <Input
              ref={registerField("name")}
              label={t("policies.duplicate.name")}
              value={name}
              onChange={(e) => setName(e.target.value)}
              helperText={t("policies.duplicate.nameHint")}
              // R4-8.4: erro inline — o required do submit ganha prioridade
              // (`errorField === "name"`); fora de uma tentativa de submit, o
              // preflight (ao vivo, a cada tecla) já avisa de conflito de nome
              // aqui em vez de só na lista genérica lá embaixo.
              error={
                errorField === "name"
                  ? (error ?? undefined)
                  : preflight?.name_conflict
                    ? t("policies.duplicate.nameConflict", { name })
                    : undefined
              }
            />

            {checking && (
              <p className="text-xs text-text-tertiary">{t("policies.duplicate.checking")}</p>
            )}

            {preflight && !checking && (
              <div data-testid="duplicate-preflight">
                {preflight.ok ? (
                  <Notice variant="success" title={t("policies.duplicate.readyTitle")}>
                    {t("policies.duplicate.readyBody")}
                  </Notice>
                ) : preflight.missing_tables.length > 0 || preflight.missing_sources.length > 0 ? (
                  // R4-8.4: conflito de nome já vira erro INLINE no campo
                  // "Nome da cópia" acima — repeti-lo aqui duplicaria a
                  // mensagem. Este banner sobra só para o que não tem campo
                  // próprio (tabela/fonte faltando no destino).
                  <Notice variant="danger" title={t("policies.duplicate.blockedTitle")}>
                    <ul className="mt-1 list-disc space-y-1 pl-4 text-xs">
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
                ) : null}

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
