/**
 * JsonViewer
 * Wrapper sobre react-json-view-lite com defaults sensatos para o design system.
 * Memoizado para evitar re-renders quando a referência de `data` não muda.
 */

import { memo } from "react"
import { JsonView, allExpanded, collapseAllNested, darkStyles } from "react-json-view-lite"
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
        // Os overrides que havia aqui miravam `.json-view-lite*`, classes que esta
        // lib NÃO emite: ela usa CSS modules com hash (`_2IvMF`). Nenhum dos cinco
        // seletores casava com coisa alguma, e o container ficava no cinza claro
        // (#eee) do preset default — uma caixa clara dentro do tema escuro, em toda
        // tela que mostra JSON. Como não dá para mirar classe com hash, a correção é
        // trocar o PRESET (`darkStyles`), não empilhar seletor.
        "text-xs font-mono",
        className,
      )}
    >
      <JsonView
        data={safeData}
        shouldExpandNode={shouldExpandNode}
        style={darkStyles}
      />
    </div>
  )
})
