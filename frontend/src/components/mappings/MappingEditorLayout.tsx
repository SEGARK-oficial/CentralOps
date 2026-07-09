/**
 * MappingEditorLayout
 * Editor denso de mappings em 3 painéis. Padrão de ferramenta (IDE-like):
 *
 * Desktop (≥1280px): painéis redimensionáveis lado a lado (react-resizable-panels),
 *   com tamanhos persistidos (autoSaveId), painel de amostra colapsável e altura
 *   de viewport com scroll independente por painel.
 * Mobile/tablet (<1280px): painéis empilhados verticalmente (3 colunas seriam
 *   apertadas demais).
 *
 * O full-bleed (sair do max-w-7xl do shell) é resolvido no AppLayout, que dá
 * largura total às rotas de editor. min-w-0 nos filhos garante que conteúdo
 * rígido (JSON longo, tabelas) role DENTRO do painel em vez de estourar a página.
 */

import type React from "react"
import { useRef, useState } from "react"
import { Panel, PanelGroup, PanelResizeHandle, type ImperativePanelHandle } from "react-resizable-panels"
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"
import { useMediaQuery } from "@/hooks/useMediaQuery"

interface MappingEditorLayoutProps {
  payload: React.ReactNode
  rules: React.ReactNode
  envelope: React.ReactNode
  className?: string
}

// Scroll interno do painel (IDE-style) — a página não rola, cada coluna sim.
const PanelScroll: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div className="h-full min-w-0 overflow-auto scrollbar-thin pr-1">{children}</div>
)

export const MappingEditorLayout: React.FC<MappingEditorLayoutProps> = ({
  payload,
  rules,
  envelope,
  className,
}) => {
  const { t } = useTranslation("mappings")
  const isWide = useMediaQuery("(min-width: 1280px)")
  const payloadRef = useRef<ImperativePanelHandle>(null)
  const [payloadCollapsed, setPayloadCollapsed] = useState(false)

  // <1280px: empilhado. Painéis horizontais redimensionáveis não cabem.
  if (!isWide) {
    return (
      <div className={cn("flex flex-col gap-4", className)}>
        <div className="min-w-0">{payload}</div>
        <div className="min-w-0">{rules}</div>
        <div className="min-w-0">{envelope}</div>
      </div>
    )
  }

  const togglePayload = () => {
    const panel = payloadRef.current
    if (!panel) return
    if (panel.isCollapsed()) panel.expand()
    else panel.collapse()
  }

  return (
    <PanelGroup
      direction="horizontal"
      autoSaveId="centralops:mapping-editor"
      className={cn("h-[calc(100vh-15rem)] min-h-[34rem]", className)}
    >
      <Panel
        ref={payloadRef}
        id="payload"
        order={1}
        collapsible
        collapsedSize={0}
        defaultSize={26}
        minSize={16}
        onCollapse={() => setPayloadCollapsed(true)}
        onExpand={() => setPayloadCollapsed(false)}
      >
        <PanelScroll>{payload}</PanelScroll>
      </Panel>

      {/* Divisória payload│regras — arrastável + botão de colapsar/expandir. */}
      <PanelResizeHandle className="group relative flex w-3 items-center justify-center rounded outline-none focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-primary-500">
        <div
          aria-hidden="true"
          className="h-10 w-1 rounded-full bg-border transition-colors group-hover:bg-primary-400 group-data-[resize-handle-state=drag]:bg-primary-500"
        />
        <button
          type="button"
          onPointerDown={(e) => e.stopPropagation()}
          onClick={togglePayload}
          aria-label={payloadCollapsed ? t("editorLayout.expandPayloadPanel") : t("editorLayout.collapsePayloadPanel")}
          className="absolute top-3 z-10 flex h-6 w-6 items-center justify-center rounded-full border border-border bg-surface text-text-tertiary shadow-sm transition-colors hover:text-text focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
        >
          {payloadCollapsed ? <ChevronRightIcon size={13} aria-hidden="true" /> : <ChevronLeftIcon size={13} aria-hidden="true" />}
        </button>
      </PanelResizeHandle>

      <Panel id="rules" order={2} defaultSize={40} minSize={28}>
        <PanelScroll>{rules}</PanelScroll>
      </Panel>

      {/* Divisória regras│envelope. */}
      <PanelResizeHandle className="group relative flex w-3 items-center justify-center rounded outline-none focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-primary-500">
        <div
          aria-hidden="true"
          className="h-10 w-1 rounded-full bg-border transition-colors group-hover:bg-primary-400 group-data-[resize-handle-state=drag]:bg-primary-500"
        />
      </PanelResizeHandle>

      <Panel id="envelope" order={3} defaultSize={34} minSize={22}>
        <PanelScroll>{envelope}</PanelScroll>
      </Panel>
    </PanelGroup>
  )
}

export default MappingEditorLayout
