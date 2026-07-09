/**
 * BulkActionBar
 * -------------
 * Barra contextual presentational que aparece quando há `count >= 1`.
 * Slot-based: o caller compõe os botões de ação como `children`.
 *
 * Visual:
 *   [N {contextLabel} selecionado(s)]   [Limpar seleção] [...children]
 *
 * Acessibilidade:
 *   - role="region", aria-label="Ações em massa"
 *   - aria-live="polite" no contador para anunciar mudanças de N
 *
 * O componente NÃO controla seleção. Use `useBulkSelection` no caller
 * e passe `count` + `onClear`.
 */

import type React from "react"
import { XIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/Button/Button"
import { cn } from "@/lib/utils"

export interface BulkActionBarProps {
  /** Quantidade de itens selecionados. Quando 0, o componente não renderiza. */
  count: number
  /** Handler do botão "Limpar seleção". */
  onClear: () => void
  /** Rótulo contextual usado na string "{N} {contextLabel} selecionado(s)".
   *  Default: "selecionado(s)" sem label. */
  contextLabel?: string
  /** Botões de ação adicionais (slot à direita). */
  children?: React.ReactNode
  /** Classes extras para o container externo. */
  className?: string
  /** Override do test-id (default: "bulk-action-bar"). */
  "data-testid"?: string
}

/**
 * Render: nada se count === 0; caso contrário, barra com contador + slot.
 *
 * Pluralização:
 *   - `contextLabel` ausente → "{N} selecionado" / "{N} selecionados"
 *   - `contextLabel` presente → "{N} {contextLabel} selecionado(s)" sempre
 *     (contexto plural-agnóstico, escolha consciente: o caller pode passar
 *     "tenant(s)" se quiser explicitar)
 */
export const BulkActionBar: React.FC<BulkActionBarProps> = ({
  count,
  onClear,
  contextLabel,
  children,
  className,
  "data-testid": dataTestId = "bulk-action-bar",
}) => {
  const { t } = useTranslation("ui")
  if (count <= 0) return null

  const counterText = contextLabel
    ? t("bulkActionBar.counterWithLabel", { count, contextLabel })
    : t("bulkActionBar.counter", { count })

  return (
    <div
      data-testid={dataTestId}
      role="region"
      aria-label={t("bulkActionBar.regionAriaLabel")}
      className={cn(
        "flex flex-wrap items-center justify-between gap-2 rounded-md border border-primary-200 bg-primary-50 px-3 py-2",
        className,
      )}
    >
      <span
        className="text-sm font-medium text-primary-800"
        aria-live="polite"
        data-testid={`${dataTestId}-count`}
      >
        {counterText}
      </span>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="ghost"
          size="sm"
          onClick={onClear}
          leftIcon={<XIcon size={14} />}
          aria-label={t("bulkActionBar.clearSelection")}
          data-testid={`${dataTestId}-clear`}
        >
          {t("bulkActionBar.clearSelection")}
        </Button>
        {children}
      </div>
    </div>
  )
}

export default BulkActionBar
