"use client"

import type React from "react"
import { Suspense, useEffect, useId, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { FocusScope } from "@radix-ui/react-focus-scope"
import { cn } from "@/lib/utils"
import { PortalContainerContext } from "@/components/ui/Modal/Modal"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { isTopmostDialog, lockBodyScroll, nextDialogOrder, registerOpenDialog, unlockBodyScroll, unregisterOpenDialog } from "@/components/ui/internal/dialogStack"

/**
 * ui/Drawer — primitivo de painel lateral (A11Y-17 / ARQ-06 / LAY-20).
 *
 * Consolida o que `DriftRulesDrawer` e `DetectionDetailsDrawer` reimplementavam
 * cada um à sua maneira (um com `FocusScope contain` — prop inválida, TS-03 —
 * outro com um trap de Tab escrito à mão): portal, overlay com token
 * (`bg-overlay`), trava de scroll, `FocusScope trapped loop` (mesmo padrão do
 * Modal), participação na PILHA de Escape compartilhada (só o diálogo do topo
 * fecha), restauração de foco ao elemento que abriu, e o mesmo
 * `PortalContainerContext` do Modal — então um Select aberto DENTRO de um
 * Drawer também porta para o painel em vez de para o body.
 *
 * O header/rodapé ficam por conta de quem consome (como no `DetectionDetailsDrawer`
 * hoje): o Drawer só entrega o "casco" — moldura, foco e teclado.
 */
export interface DrawerProps {
  open: boolean
  onClose: () => void
  children: React.ReactNode
  /** Lado por onde o painel desliza. Padrão "right". */
  side?: "left" | "right"
  size?: "sm" | "md" | "lg" | "xl" | "full"
  closeOnOverlayClick?: boolean
  closeOnEscape?: boolean
  role?: "dialog" | "alertdialog"
  /** Nome acessível quando o consumidor não referencia um heading via `ariaLabelledBy`. */
  ariaLabel?: string
  ariaLabelledBy?: string
  "data-testid"?: string
}

const sizeMap = {
  sm: "sm:max-w-sm",
  md: "sm:max-w-md",
  lg: "sm:max-w-lg",
  xl: "sm:max-w-2xl",
  full: "sm:max-w-3xl",
}

export const Drawer: React.FC<DrawerProps> = ({
  open,
  onClose,
  children,
  side = "right",
  size = "lg",
  closeOnOverlayClick = true,
  closeOnEscape = true,
  role = "dialog",
  ariaLabel,
  ariaLabelledBy,
  "data-testid": dataTestId,
}) => {
  const previousActiveElement = useRef<HTMLElement | null>(null)
  const drawerId = useId()
  const [drawerOrder] = useState(nextDialogOrder)
  const [panelEl, setPanelEl] = useState<HTMLDivElement | null>(null)

  // Mesmo padrão do Modal: `onClose`/`closeOnEscape` recriados a cada render
  // do pai não podem entrar nas deps do efeito de foco — senão ele re-roda a
  // cada re-render e o cleanup (`previousActiveElement.focus()`) rouba o foco
  // de volta (A11Y-16/17: "onClose inline re-dispara o foco").
  const onCloseRef = useRef(onClose)
  const closeOnEscapeRef = useRef(closeOnEscape)
  useEffect(() => {
    onCloseRef.current = onClose
    closeOnEscapeRef.current = closeOnEscape
  })

  useEffect(() => {
    if (!open) return

    previousActiveElement.current = document.activeElement as HTMLElement
    lockBodyScroll()
    registerOpenDialog(drawerId, drawerOrder)

    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return
      if (event.defaultPrevented) return
      if (!isTopmostDialog(drawerOrder)) return
      if (closeOnEscapeRef.current) onCloseRef.current()
    }
    document.addEventListener("keydown", handleEscape)

    return () => {
      document.removeEventListener("keydown", handleEscape)
      unlockBodyScroll()
      unregisterOpenDialog(drawerId)
      previousActiveElement.current?.focus()
    }
  }, [open, drawerId, drawerOrder])

  if (!open) return null

  const handleOverlayClick = (event: React.MouseEvent) => {
    if (closeOnOverlayClick && event.target === event.currentTarget) onClose()
  }

  return createPortal(
    <div className="fixed inset-0 z-modal-backdrop bg-overlay animate-fade-in" onClick={handleOverlayClick}>
      <FocusScope trapped loop>
        <div
          ref={setPanelEl}
          className={cn(
            "fixed top-0 h-full w-full flex flex-col bg-surface shadow-2xl",
            side === "right" ? "right-0 border-l border-border animate-slide-left" : "left-0 border-r border-border",
            sizeMap[size],
          )}
          tabIndex={-1}
          role={role}
          aria-modal="true"
          aria-label={!ariaLabelledBy ? ariaLabel : undefined}
          aria-labelledby={ariaLabelledBy}
          data-testid={dataTestId}
        >
          {/* R3-8.1: mesmo Suspense local do Modal — ver comentário lá. */}
          <PortalContainerContext.Provider value={panelEl}>
            <Suspense fallback={<LoadingSpinner size="sm" className="p-8" />}>{children}</Suspense>
          </PortalContainerContext.Provider>
        </div>
      </FocusScope>
    </div>,
    document.body,
  )
}

export default Drawer
