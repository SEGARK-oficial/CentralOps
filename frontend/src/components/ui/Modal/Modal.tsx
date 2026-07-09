"use client"

import type React from "react"
import { useEffect, useId, useRef } from "react"
import { createPortal } from "react-dom"
import { XIcon } from "lucide-react"
import { FocusScope } from "@radix-ui/react-focus-scope"
import { useTranslation } from "react-i18next"
import { Button } from "../Button/Button"
import { cn } from "@/lib/utils"

interface ModalProps {
  open: boolean
  onClose: () => void
  title?: string
  children: React.ReactNode
  size?: "sm" | "md" | "lg" | "xl"
  closeOnOverlayClick?: boolean
  closeOnEscape?: boolean
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
}) => {
  const { t } = useTranslation("ui")
  const previousActiveElement = useRef<HTMLElement | null>(null)
  const titleId = useId()

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

    const handleEscape = (event: KeyboardEvent) => {
      if (closeOnEscapeRef.current && event.key === "Escape") onCloseRef.current()
    }
    document.addEventListener("keydown", handleEscape)

    return () => {
      document.removeEventListener("keydown", handleEscape)
      document.body.style.overflow = ""
      // Retorna foco ao elemento que abriu o modal (só no fechamento real, não por tecla).
      previousActiveElement.current?.focus()
    }
  }, [open])

  const handleOverlayClick = (event: React.MouseEvent) => {
    if (closeOnOverlayClick && event.target === event.currentTarget) onClose()
  }

  if (!open) return null

  return createPortal(
    <div
      className="fixed inset-0 z-modal-backdrop bg-overlay flex items-center justify-center p-4 animate-fade-in"
      onClick={handleOverlayClick}
      role="dialog"
      aria-modal="true"
      aria-labelledby={title ? titleId : undefined}
    >
      {/*
        FocusScope (trapped) contém o foco dentro do modal — Tab/Shift+Tab não
        escapam. `loop` faz o foco circular do último para o primeiro elemento.
      */}
      <FocusScope trapped loop>
        <div
          className={cn(
            "w-full bg-surface rounded-lg shadow-xl animate-slide-up max-h-[90vh] flex flex-col",
            sizeMap[size],
          )}
          tabIndex={-1}
        >
          {title && (
            <div className="flex items-center justify-between px-6 py-4 border-b border-border">
              <h2 id={titleId} className="text-lg font-semibold text-text">{title}</h2>
              <Button variant="ghost" size="xs" onClick={onClose} aria-label={t("modal.closeAriaLabel")}>
                <XIcon size={18} />
              </Button>
            </div>
          )}
          <div className="flex-1 overflow-y-auto p-6">{children}</div>
        </div>
      </FocusScope>
    </div>,
    document.body,
  )
}
