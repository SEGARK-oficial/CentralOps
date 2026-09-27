/**
 * JsonViewer
 * Wrapper sobre react-json-view-lite com defaults sensatos para o design system.
 * Memoizado para evitar re-renders quando a referência de `data` não muda.
 */

import { memo } from "react"
import { JsonView, allExpanded } from "react-json-view-lite"
import "react-json-view-lite/dist/index.css"
import { cn } from "@/lib/utils"

export interface JsonViewerProps {
  data: unknown
  /** Nível de profundidade a partir do qual colapsar automaticamente (default: 2) */
  collapseLevel?: number
  className?: string
}

/**
 * LAY-07: o preset `darkStyles` da lib é Solarized (fundo `rgb(0,43,54)`,
 * string em laranja, número em magenta, boolean em violeta) — nenhuma dessas
 * cores é um token do design system, e violeta/magenta/laranja aqui colidem
 * com a semântica de estágio usada no resto do produto (violeta = normalize,
 * âmbar = collect). `StyleProps` recebe NOMES DE CLASSE (não CSS inline), e
 * a lib não expõe as classes puramente estruturais (espaçamento, cursor,
 * glifo ▸/▾) separadas das de cor — então reconstruímos tudo com classes do
 * DS, priorizando neutros e mono para os valores (nenhuma matiz de estágio é
 * usada aqui).
 *
 * `container: "bg-transparent"` de propósito — todo call-site já embrulha o
 * JsonViewer num container com sua própria superfície (`bg-surface-tertiary`
 * ou `bg-surface-secondary` + borda); um fundo opaco aqui duplicava a caixa.
 *
 * Tipo não anotado explicitamente: `StyleProps` é interno da lib (não
 * exportado na raiz do pacote) — o shape é verificado estruturalmente no
 * `style={...}` do `<JsonView>` abaixo (`Props.style: Partial<StyleProps>`).
 */
const CENTRALOPS_JSON_STYLES = {
  container: "bg-transparent whitespace-pre-wrap break-words leading-snug",
  basicChildStyle: "block pl-2.5",
  childFieldsContainer: "block",
  label: "font-mono font-semibold text-text-secondary mr-1",
  clickableLabel: "font-mono font-semibold text-text-secondary mr-1 cursor-pointer hover:text-text",
  punctuation: "text-text-tertiary font-bold mr-1",
  nullValue: "font-mono text-text-tertiary italic",
  undefinedValue: "font-mono text-text-tertiary italic",
  stringValue: "font-mono text-text",
  numberValue: "font-mono text-primary-600",
  booleanValue: "font-mono text-primary-600",
  otherValue: "font-mono text-text",
  collapseIcon: "text-text-tertiary cursor-pointer select-none mr-1 text-[1.1em] after:content-['▾']",
  expandIcon: "text-text-tertiary cursor-pointer select-none mr-1 text-[1.1em] after:content-['▸']",
  collapsedContent: "text-text-tertiary cursor-pointer mr-1 after:content-['…']",
  noQuotesForStringValues: false,
  quotesForFieldNames: false,
  ariaLables: { collapseJson: "collapse JSON", expandJson: "expand JSON" },
  stringifyStringValues: false,
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
    <div className={cn("text-xs font-mono", className)}>
      <JsonView
        data={safeData}
        shouldExpandNode={shouldExpandNode}
        style={CENTRALOPS_JSON_STYLES}
      />
    </div>
  )
})
