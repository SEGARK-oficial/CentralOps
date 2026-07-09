/**
 * JsonViewer
 * Wrapper sobre react-json-view-lite com defaults sensatos para o design system.
 * Memoizado para evitar re-renders quando a referência de `data` não muda.
 */

import { memo } from "react"
import { JsonView, allExpanded, collapseAllNested, defaultStyles } from "react-json-view-lite"
import "react-json-view-lite/dist/index.css"
import { cn } from "@/lib/utils"

export interface JsonViewerProps {
  data: unknown
  /** Nível de profundidade a partir do qual colapsar automaticamente (default: 2) */
  collapseLevel?: number
  className?: string
}

/**
 * Retorna a função de colapso adequada para o nível solicitado.
 * collapseLevel 0 = tudo colapsado; Infinity = tudo expandido.
 */
function buildShouldExpand(collapseLevel: number): (level: number) => boolean {
  if (collapseLevel <= 0) return () => false
  if (collapseLevel === Infinity) return allExpanded
  // colapsa nós em níveis >= collapseLevel
  return (level: number) => level < collapseLevel
}

export const JsonViewer = memo(function JsonViewer({ data, collapseLevel = 2, className }: JsonViewerProps) {
  // Normaliza null/undefined para objeto exibível
  const safeData = data === null || data === undefined ? { value: data } : (data as object)

  const shouldExpandNode = buildShouldExpand(collapseLevel)

  return (
    <div
      className={cn(
        // Neutraliza cores fortes da lib — usa a paleta do DS
        "text-xs font-mono [&_.json-view-lite]:bg-transparent",
        "[&_.json-view-lite-string]:text-success-700",
        "[&_.json-view-lite-number]:text-primary-700",
        "[&_.json-view-lite-boolean]:text-warning-700",
        "[&_.json-view-lite-null]:text-text-tertiary",
        className,
      )}
    >
      <JsonView
        data={safeData}
        shouldExpandNode={shouldExpandNode}
        style={defaultStyles}
      />
    </div>
  )
})
