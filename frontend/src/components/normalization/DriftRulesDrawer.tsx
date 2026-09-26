/**
 * DriftRulesDrawer
 * Drawer lateral que lista as regras de mapping que consomem um field_path de drift.
 * Abre pela direita como overlay sobre o conteúdo da página.
 *
 * A11y: casco (portal, overlay, focus trap, Escape, restauração de foco) vem
 * do primitivo `ui/Drawer` (A11Y-17/ARQ-06) — este componente só monta o
 * header/body por cima.
 */

import type React from "react"
import { useTranslation } from "react-i18next"
import { XIcon, ExternalLinkIcon } from "lucide-react"
import { Link } from "react-router-dom"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Drawer } from "@/components/ui/Drawer/Drawer"
import type { MatchedRule, MatchKind } from "@/hooks/useFieldRules"

// ── Badge de match_kind ───────────────────────────────────────────────────────

const MATCH_KIND_VARIANTS: Record<MatchKind, "primary" | "default" | "warning" | "outline"> = {
  primary: "primary",
  fallback: "default",
  array_builder_item: "warning",
  preprocess: "outline",
}

// ── Props ─────────────────────────────────────────────────────────────────────

interface DriftRulesDrawerProps {
  open: boolean
  onClose: () => void
  field_path: string
  rules: MatchedRule[]
}

// ── Componente ────────────────────────────────────────────────────────────────

const TITLE_ID = "drift-rules-drawer-title"

export const DriftRulesDrawer: React.FC<DriftRulesDrawerProps> = ({
  open,
  onClose,
  field_path,
  rules,
}) => {
  const { t } = useTranslation("drift")
  const MATCH_KIND_LABELS: Record<MatchKind, string> = {
    primary: t("rulesDrawer.matchKind.primary"),
    fallback: t("rulesDrawer.matchKind.fallback"),
    array_builder_item: t("rulesDrawer.matchKind.array_builder_item"),
    preprocess: t("rulesDrawer.matchKind.preprocess"),
  }
  return (
    <Drawer open={open} onClose={onClose} size="md" ariaLabelledBy={TITLE_ID} data-testid="drift-rules-drawer">
      <div className="flex h-full flex-col">
        {/* Header */}
        <div className="flex items-start justify-between gap-3 px-6 py-4 border-b border-border">
          <div className="flex-1 min-w-0">
            <p className="text-xs text-text-tertiary uppercase tracking-wide mb-0.5">
              {t("rulesDrawer.eyebrow")}
            </p>
            <h2
              id={TITLE_ID}
              className="text-base font-semibold text-text font-mono truncate"
              title={field_path}
            >
              {field_path}
            </h2>
          </div>
          <Button
            variant="ghost"
            size="xs"
            onClick={onClose}
            aria-label={t("rulesDrawer.closeAriaLabel")}
            className="mt-0.5 shrink-0"
          >
            <XIcon size={18} />
          </Button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
            {rules.length === 0 ? (
              <p className="text-text-tertiary text-sm text-center py-8">
                {t("rulesDrawer.emptyMessage")}
              </p>
            ) : (
              <ul className="flex flex-col gap-3" data-testid="drawer-rules-list">
                {rules.map((rule, idx) => (
                  <li
                    key={`${rule.mapping_definition_id}-${rule.rule_target}-${idx}`}
                    className="border border-border rounded-md p-4 flex flex-col gap-2 bg-surface-secondary"
                    data-testid={`drawer-rule-item-${idx}`}
                  >
                    {/* Target + kind */}
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-mono text-xs font-semibold text-text truncate">
                        {rule.rule_target}
                      </span>
                      <Badge
                        variant={MATCH_KIND_VARIANTS[rule.match_kind]}
                        size="sm"
                        data-testid={`drawer-rule-kind-${idx}`}
                      >
                        {MATCH_KIND_LABELS[rule.match_kind]}
                      </Badge>
                    </div>

                    {/* Source */}
                    <p className="text-xs text-text-secondary">
                      <span className="text-text-tertiary">{t("rulesDrawer.sourceLabel")}</span>
                      <span className="font-mono">{rule.source}</span>
                    </p>

                    {/* Link para o mapping editor */}
                    <Link
                      to={`/mappings/${rule.mapping_definition_id}`}
                      className="inline-flex items-center gap-1 text-xs text-primary-600 hover:text-primary-700 hover:underline w-fit"
                      data-testid={`drawer-rule-link-${idx}`}
                    >
                      {t("rulesDrawer.openMapping")}
                      <ExternalLinkIcon size={11} aria-hidden="true" />
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
    </Drawer>
  )
}

export default DriftRulesDrawer
