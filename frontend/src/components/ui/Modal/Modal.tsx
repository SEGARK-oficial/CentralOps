"use client"

import type React from "react"
import { createContext, useEffect, useId, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { XIcon } from "lucide-react"
import { FocusScope } from "@radix-ui/react-focus-scope"
import { useTranslation } from "react-i18next"
import { Button } from "../Button/Button"
import { cn } from "@/lib/utils"
import { isTopmostDialog, nextDialogOrder, registerOpenDialog, unregisterOpenDialog } from "../internal/dialogStack"

/**
 * A11Y-01: o Select porta sua listbox para `document.body`, fora do
 * `FocusScope trapped` do Modal. Quando um dropdown tenta focar uma opção
 * fora do container rastreado pelo FocusScope, o Radix devolve o foco ao
 * gatilho (a lista "não segura" o foco). Expondo o elemento do painel via
 * contexto, o Select — e qualquer outro portal que precise conviver com o
 * focus trap — pode portar PARA DENTRO do painel em vez de para o body,
 * então o `contains()` do FocusScope enxerga a opção como parte do Modal.
 * `null` fora de um Modal (comportamento atual, sem mudança).
 */
export const PortalContainerContext = createContext<HTMLElement | null>(null)

/**
 * A11Y-02: pilha de modais (ver `internal/dialogStack.ts`, compartilhada com
 * o `Drawer`). O listener de Escape é por instância (documento inteiro),
 * então um ConfirmDialog aninhado dentro de outro Modal faria os DOIS
 * ouvirem o mesmo Escape. Só o topo da pilha deve reagir; o
 * `defaultPrevented` cobre o caso do Select/HelpTooltip abertos por cima,
 * que consomem o Escape antes dele "contar" como fechamento do Modal.
 */

interface ModalProps {
  open: boolean
  onClose: () => void
  title?: string
  children: React.ReactNode
  size?: "sm" | "md" | "lg" | "xl"
  closeOnOverlayClick?: boolean
  closeOnEscape?: boolean
  /** A11Y-32: `alertdialog` para confirmações destrutivas (ConfirmDialog). */
  role?: "dialog" | "alertdialog"
  /** A11Y-32: nome acessível quando não há `title` visível. */
  ariaLabel?: string
  /** A11Y-32: liga a descrição (ex.: texto do ConfirmDialog) ao painel. */
  ariaDescribedBy?: string
}

const sizeMap = {
  sm: "max-w-md",
  md: "max-w-lg",
  lg: "max-w-2xl",
  xl: "max-w-4xl",
}

export const Modal: React.FC<ModalProps> = ({
  open,
  onClose,
  title,
  children,
  size = "md",
  closeOnOverlayClick = true,
  closeOnEscape = true,
  role = "dialog",
  ariaLabel,
  ariaDescribedBy,
}) => {
  const { t } = useTranslation("ui")
  const previousActiveElement = useRef<HTMLElement | null>(null)
  const titleId = useId()
  const modalId = useId()
  // Calculado uma única vez, no primeiro render desta instância — ver o
  // comentário em `internal/dialogStack.ts` sobre por que não pode ser feito
  // dentro de um `useEffect`.
  const [modalOrder] = useState(nextDialogOrder)
  const [panelEl, setPanelEl] = useState<HTMLDivElement | null>(null)

  // ``onClose``/``closeOnEscape`` costumam ser recriados a cada render do pai (ex.:
  // ``onClose={() => setOpen(false)}`` inline). Se entrassem nas deps do efeito de foco
  // abaixo, ele re-rodaria a CADA tecla digitada e o cleanup (``previousActiveElement
  // .focus()``) roubaria o foco do input de volta p/ quem abriu o modal — o clássico
  // "campo perde o foco a cada letra". Mantemos as versões atuais num ref e o efeito
  // depende SÓ de ``open``, então ele só corre ao abrir/fechar.
  const onCloseRef = useRef(onClose)
  const closeOnEscapeRef = useRef(closeOnEscape)
  useEffect(() => {
    onCloseRef.current = onClose
    closeOnEscapeRef.current = closeOnEscape
  })

  useEffect(() => {
    if (!open) return

    previousActiveElement.current = document.activeElement as HTMLElement
    document.body.style.overflow = "hidden"
    registerOpenDialog(modalId, modalOrder)

    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return
      // Um Select/HelpTooltip aberto por cima consome o próprio Escape
      // (preventDefault) — nesse caso o Modal nem deveria contar o evento.
      if (event.defaultPrevented) return
      // Só o modal do TOPO (maior ordem entre os abertos) reage: um
      // ConfirmDialog aninhado não deve fechar o Modal por baixo dele no
      // mesmo Escape.
      if (!isTopmostDialog(modalOrder)) return
      if (closeOnEscapeRef.current) onCloseRef.current()
    }
    document.addEventListener("keydown", handleEscape)

    return () => {
      document.removeEventListener("keydown", handleEscape)
      document.body.style.overflow = ""
      unregisterOpenDialog(modalId)
      // Retorna foco ao elemento que abriu o modal (só no fechamento real, não por tecla).
      previousActiveElement.current?.focus()
    }
  }, [open, modalId, modalOrder])

  const handleOverlayClick = (event: React.MouseEvent) => {
    if (closeOnOverlayClick && event.target === event.currentTarget) onClose()
  }

  if (!open) return null

  return createPortal(
    <div
      className="fixed inset-0 z-modal-backdrop bg-overlay flex items-center justify-center p-4 animate-fade-in"
      onClick={handleOverlayClick}
    >
      {/*
        FocusScope (trapped) contém o foco dentro do modal — Tab/Shift+Tab não
        escapam. `loop` faz o foco circular do último para o primeiro elemento.
      */}
      <FocusScope trapped loop>
        {/* Modal flutua de verdade, então é um dos dois lugares onde a sombra
            entra. A hairline vem junto: no ground escuro é ela que desenha a
            aresta que a sombra não consegue.
            A11Y-32: role/aria-modal/aria-labelledby moram AQUI (no painel),
            não no overlay — é o painel que é a caixa de diálogo. */}
        <div
          ref={setPanelEl}
          className={cn(
            "w-full bg-surface border border-border-hover rounded-lg shadow-xl animate-slide-up max-h-[90vh] flex flex-col",
            sizeMap[size],
          )}
          tabIndex={-1}
          role={role}
          aria-modal="true"
          aria-labelledby={title ? titleId : undefined}
          aria-label={!title ? ariaLabel : undefined}
          aria-describedby={ariaDescribedBy}
        >
          <PortalContainerContext.Provider value={panelEl}>
            {title && (
              <div className="flex items-center justify-between gap-4 px-5 py-3 border-b border-border">
                <h2 id={titleId} className="text-base font-semibold text-text">{title}</h2>
                <Button variant="ghost" size="xs" onClick={onClose} aria-label={t("modal.closeAriaLabel")}>
                  <XIcon size={16} />
                </Button>
              </div>
            )}
            <div className="flex-1 overflow-y-auto p-5">{children}</div>
          </PortalContainerContext.Provider>
        </div>
      </FocusScope>
    </div>,
    document.body,
  )
}
