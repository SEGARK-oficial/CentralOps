import type React from "react"
import { useMemo } from "react"
import { useTranslation } from "react-i18next"
import { Badge } from "@/components/ui/Badge/Badge"
import type { EnrichmentRule } from "@/services/api"

/**
 * O que esta publicação muda em relação à versão vigente.
 *
 * Publicar SUBSTITUI a lista inteira de regras — não faz mesclagem. O editor já
 * abre hidratado da versão vigente por causa disso, mas nada dizia o que a
 * publicação ia alterar, e o operador descobria pelo comportamento em produção:
 * uma regra removida sem querer não gera erro nenhum, só para de casar.
 *
 * A comparação é por `id` de regra, não por posição: reordenar não é alteração
 * de conteúdo, e tratar como se fosse encheria o diff de ruído — a ordem
 * importa para a cascata de alternativas, e por isso ela é reportada à parte.
 */

interface Props {
  published: EnrichmentRule[]
  draft: EnrichmentRule[]
}

export interface PolicyDiffSummary {
  added: string[]
  removed: string[]
  changed: string[]
  reordered: boolean
}

export function diffRules(
  published: EnrichmentRule[],
  draft: EnrichmentRule[],
): PolicyDiffSummary {
  const byId = (list: EnrichmentRule[]) => {
    const map = new Map<string, EnrichmentRule>()
    for (const r of list) map.set(r.id, r)
    return map
  }
  const before = byId(published)
  const after = byId(draft)

  const added = [...after.keys()].filter((id) => !before.has(id))
  const removed = [...before.keys()].filter((id) => !after.has(id))
  const changed = [...after.keys()].filter(
    (id) => before.has(id) && JSON.stringify(before.get(id)) !== JSON.stringify(after.get(id)),
  )

  // Só compara ordem entre as regras que existem dos dois lados; do contrário
  // toda adição contaria como reordenação.
  const commonBefore = published.map((r) => r.id).filter((id) => after.has(id))
  const commonAfter = draft.map((r) => r.id).filter((id) => before.has(id))
  const reordered = JSON.stringify(commonBefore) !== JSON.stringify(commonAfter)

  return { added, removed, changed, reordered }
}

export const PolicyDiff: React.FC<Props> = ({ published, draft }) => {
  const { t } = useTranslation("enrichment")
  const summary = useMemo(() => diffRules(published, draft), [published, draft])

  const nothing =
    summary.added.length === 0 &&
    summary.removed.length === 0 &&
    summary.changed.length === 0 &&
    !summary.reordered

  if (nothing) {
    return (
      <span className="text-xs text-muted" data-testid="policy-diff-empty">
        {t("policies.page.diffNone")}
      </span>
    )
  }

  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="policy-diff">
      <span className="text-xs text-muted">{t("policies.page.diffLabel")}</span>
      {summary.added.length > 0 && (
        <Badge variant="success" title={summary.added.join(", ")}>
          {t("policies.page.diffAdded", {
            count: summary.added.length,
            names: summary.added.join(", "),
          })}
        </Badge>
      )}
      {summary.changed.length > 0 && (
        <Badge variant="warning" title={summary.changed.join(", ")}>
          {t("policies.page.diffChanged", {
            count: summary.changed.length,
            names: summary.changed.join(", "),
          })}
        </Badge>
      )}
      {/* Remoção em destaque: é a única que apaga comportamento sem erro. */}
      {summary.removed.length > 0 && (
        <Badge variant="danger" title={summary.removed.join(", ")}>
          {t("policies.page.diffRemoved", {
            count: summary.removed.length,
            names: summary.removed.join(", "),
          })}
        </Badge>
      )}
      {summary.reordered && (
        // A ordem decide a cascata de alternativas (uma regra marca tag, a
        // seguinte reage a ela), então reordenar É mudança de comportamento.
        <Badge variant="default">{t("policies.page.diffReordered")}</Badge>
      )}
    </div>
  )
}

export default PolicyDiff
