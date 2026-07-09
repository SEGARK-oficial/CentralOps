/**
 * PreprocessEditor
 * Seção colapsável de pré-processamento no editor de mappings.
 * Renderiza a lista de PreprocessRow e o botão "+ Adicionar pré-processamento".
 * Posicionada ACIMA da lista de regras; ocupa no máximo 1/3 da altura do painel.
 */

import type React from "react"
import { useCallback, useId } from "react"
import { ChevronDownIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/Button/Button"
import { PreprocessRow, PREPROCESS_OPS } from "@/components/mappings/PreprocessRow"
import type { PreprocessOp } from "@/types"

// ── Props ─────────────────────────────────────────────────────────────────────

export interface PreprocessEditorProps {
  ops: PreprocessOp[]
  expanded: boolean
  onToggleExpand: () => void
  onChange: (ops: PreprocessOp[]) => void
  /** Se false, exibe somente leitura (sem botões de edição). */
  readOnly?: boolean
}

// ── Fábrica de op padrão ──────────────────────────────────────────────────────

function newOp(): PreprocessOp {
  return {
    op: PREPROCESS_OPS[0],
    source: "",
    target: "_",
    tolerant: true,
  }
}

// ── Component ─────────────────────────────────────────────────────────────────

export const PreprocessEditor: React.FC<PreprocessEditorProps> = ({
  ops,
  expanded,
  onToggleExpand,
  onChange,
  readOnly = false,
}) => {
  const { t } = useTranslation("mappings")
  const headingId = useId()

  // ── Edit handlers ────────────────────────────────────────────────────────────

  const handleAdd = useCallback(() => {
    onChange([...ops, newOp()])
  }, [ops, onChange])

  const handleChange = useCallback(
    (index: number, updated: PreprocessOp) => {
      const next = [...ops]
      next[index] = updated
      onChange(next)
    },
    [ops, onChange],
  )

  const handleRemove = useCallback(
    (index: number) => {
      onChange(ops.filter((_, i) => i !== index))
    },
    [ops, onChange],
  )

  const handleMoveUp = useCallback(
    (index: number) => {
      if (index === 0) return
      const next = [...ops]
      ;[next[index - 1], next[index]] = [next[index], next[index - 1]]
      onChange(next)
    },
    [ops, onChange],
  )

  const handleMoveDown = useCallback(
    (index: number) => {
      if (index === ops.length - 1) return
      const next = [...ops]
      ;[next[index], next[index + 1]] = [next[index + 1], next[index]]
      onChange(next)
    },
    [ops, onChange],
  )

  const hasOps = ops.length > 0

  return (
    <section
      role="region"
      aria-labelledby={headingId}
      data-testid="preprocess-editor"
      className="flex flex-col gap-2 rounded-lg border border-border bg-surface p-3"
    >
      {/* ── Cabeçalho colapsável ──────────────────────────────────────── */}
      <div className="flex items-center gap-2">
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={`${headingId}-body`}
          onClick={onToggleExpand}
          className="flex items-center gap-1 focus-visible:outline-2 focus-visible:outline-primary-500 rounded"
          data-testid="preprocess-toggle"
        >
          <ChevronDownIcon
            size={14}
            className={cn("transition-transform text-text-tertiary", !expanded && "-rotate-90")}
            aria-hidden="true"
          />
        </button>

        <h3
          id={headingId}
          className="text-xs font-semibold text-text-secondary uppercase tracking-wide flex-1"
        >
          {t("preprocessEditor.heading")}
          {hasOps && (
            <span className="ml-2 font-normal normal-case tracking-normal text-text-tertiary">
              ({t("preprocessEditor.opsCount", { count: ops.length })})
            </span>
          )}
        </h3>

        {/* Botão de adicionar — disponível mesmo colapsado para UX rápida */}
        {!readOnly && (
          <Button
            type="button"
            variant="outline"
            size="xs"
            onClick={handleAdd}
            data-testid="preprocess-add-button"
            aria-label={t("preprocessEditor.addAriaLabel")}
          >
            {t("preprocessEditor.addButton")}
          </Button>
        )}
      </div>

      {/* ── Body colapsável ───────────────────────────────────────────── */}
      {expanded && (
        <div
          id={`${headingId}-body`}
          className={cn(
            "flex flex-col gap-2",
            // Limitar a altura do painel de preprocess a ~1/3 do painel de regras.
            // overflow-auto permite scroll quando há muitas ops sem invadir o
            // espaço das regras.
            hasOps && "max-h-64 overflow-auto",
          )}
          data-testid="preprocess-list"
        >
          {hasOps ? (
            ops.map((op, index) =>
              readOnly ? (
                // View mode: exibe info compacta sem botões de edição
                <div
                  key={index}
                  data-testid={`preprocess-row-${index}`}
                  className="flex items-center gap-2 px-3 py-2 border border-border rounded-md bg-surface text-xs"
                >
                  <span className="font-mono text-primary-700">{op.op}</span>
                  <span className="text-text-tertiary">·</span>
                  <span className="font-mono text-text">{op.source || "—"}</span>
                  <span className="text-text-tertiary">→</span>
                  <span className="font-mono text-text">{op.target || "—"}</span>
                  {op.tolerant && (
                    <span className="ml-auto text-text-tertiary">{t("preprocessEditor.tolerant")}</span>
                  )}
                </div>
              ) : (
                <PreprocessRow
                  key={index}
                  op={op}
                  index={index}
                  onChange={handleChange}
                  onRemove={handleRemove}
                  onMoveUp={handleMoveUp}
                  onMoveDown={handleMoveDown}
                  canMoveUp={index > 0}
                  canMoveDown={index < ops.length - 1}
                />
              ),
            )
          ) : (
            <p className="text-xs text-text-tertiary py-2 text-center">
              {t("preprocessEditor.emptyState")}
            </p>
          )}
        </div>
      )}
    </section>
  )
}

export default PreprocessEditor
