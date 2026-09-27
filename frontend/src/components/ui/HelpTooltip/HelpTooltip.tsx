/**
 * HelpTooltip
 * Ícone de ajuda com tooltip contextual — label, description, example e link.
 * Abre on hover, focus (keyboard tab) e click (toggle).
 * Fecha on escape, blur e click outside.
 *
 * Renderizado via portal (document.body) para escapar de overflow:auto e
 * stacking contexts dos ancestrais. Posiciona com getBoundingClientRect.
 *
 * Anti-flicker:
 *   - Tooltip renderiza com `visibility: hidden` até a posição final ser
 *     medida (1 frame), evitando o "salto" do reposicionamento.
 *   - Hover do trigger se estende ao tooltip via portal: cursor pode
 *     transitar entre os dois sem fechar (delay curto cobre o gap).
 */

import type React from "react"
import { useId, useRef, useState, useEffect, useCallback, useLayoutEffect } from "react"
import { createPortal } from "react-dom"
import { HelpCircleIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"

export interface HelpTooltipProps {
  label: string
  description: string
  example?: string
  learnMoreHref?: string
  className?: string
}

interface TooltipPosition {
  top: number
  left: number
  placement: "top" | "bottom"
}

const TOOLTIP_OFFSET = 8 // px gap between trigger and tooltip
const TOOLTIP_MAX_WIDTH = 320 // matches max-w-xs
const HOVER_BRIDGE_DELAY_MS = 120 // tempo pra cursor transitar trigger↔tooltip

export const HelpTooltip: React.FC<HelpTooltipProps> = ({
  label,
  description,
  example,
  learnMoreHref,
  className,
}) => {
  const { t } = useTranslation("ui")
  const tooltipId = useId()
  // A11Y-18: `role="tooltip"` é para conteúdo PURAMENTE informativo — a
  // maioria dos leitores de tela não inclui elementos com esse role na
  // navegação por Tab (tooltip "some" quando o foco sai do gatilho, por
  // design). Um link real ("Saiba mais") dentro dele fica inalcançável.
  // Com `learnMoreHref`, o widget vira um popover de disclosure comum (sem
  // role=tooltip, `aria-haspopup`/`aria-controls` em vez de
  // `aria-describedby`) — content focável é suportado nesse padrão.
  const isDisclosure = learnMoreHref != null
  // Dois estados independentes:
  // - hoverOpen: aberto enquanto o cursor/foco está no trigger ou tooltip.
  // - clickedOpen: "fixado" via clique — permanece aberto até clicar fora,
  //   teclar Escape, ou clicar no trigger novamente.
  const [hoverOpen, setHoverOpen] = useState(false)
  const [clickedOpen, setClickedOpen] = useState(false)
  const open = hoverOpen || clickedOpen
  const [position, setPosition] = useState<TooltipPosition | null>(null)
  // measured = true depois que o tooltip foi medido e reposicionado com
  // altura real. Enquanto false, render com visibility: hidden — o
  // tooltip está montado mas o usuário não vê o "salto" da medição.
  const [measured, setMeasured] = useState(false)

  const triggerRef = useRef<HTMLButtonElement>(null)
  const tooltipRef = useRef<HTMLDivElement>(null)
  // Timer pra debouncing de close — cursor pode transitar do trigger pro
  // tooltip via portal (gap de poucos pixels). Sem debounce, a transição
  // dispara mouseLeave antes do mouseEnter no tooltip, fechando.
  const closeTimerRef = useRef<number | null>(null)

  const cancelScheduledClose = useCallback(() => {
    if (closeTimerRef.current != null) {
      window.clearTimeout(closeTimerRef.current)
      closeTimerRef.current = null
    }
  }, [])

  const scheduleClose = useCallback(() => {
    cancelScheduledClose()
    closeTimerRef.current = window.setTimeout(() => {
      setHoverOpen(false)
      closeTimerRef.current = null
    }, HOVER_BRIDGE_DELAY_MS)
  }, [cancelScheduledClose])

  const close = useCallback(() => {
    cancelScheduledClose()
    setHoverOpen(false)
    setClickedOpen(false)
  }, [cancelScheduledClose])

  // Limpa o timer ao desmontar.
  useEffect(() => {
    return () => {
      if (closeTimerRef.current != null) {
        window.clearTimeout(closeTimerRef.current)
      }
    }
  }, [])

  // ── Position calculation ─────────────────────────────────────────────────
  // Computes fixed-position coordinates relative to the viewport. Prefers
  // placing the tooltip above the trigger; flips to below if it would clip
  // off the top edge.
  const computePosition = useCallback(() => {
    const trigger = triggerRef.current
    if (!trigger) return

    const rect = trigger.getBoundingClientRect()
    const tooltipHeight = tooltipRef.current?.offsetHeight ?? 80 // estimativa antes do render
    const tooltipWidth = tooltipRef.current?.offsetWidth ?? TOOLTIP_MAX_WIDTH

    // Default: place above
    let placement: "top" | "bottom" = "top"
    let top = rect.top - tooltipHeight - TOOLTIP_OFFSET

    // Flip to below if not enough room above
    if (top < 8) {
      placement = "bottom"
      top = rect.bottom + TOOLTIP_OFFSET
    }

    // Horizontally: align to trigger left, but clamp to viewport
    let left = rect.left
    const viewportWidth = window.innerWidth
    if (left + tooltipWidth > viewportWidth - 8) {
      left = Math.max(8, viewportWidth - tooltipWidth - 8)
    }
    if (left < 8) left = 8

    setPosition({ top, left, placement })
  }, [])

  // 1º layout effect — quando abre, calcula posição inicial (estimada).
  // Quando fecha, reseta.
  useLayoutEffect(() => {
    if (!open) {
      setPosition(null)
      setMeasured(false)
      return
    }
    computePosition()
  }, [open, computePosition])

  // 2º layout effect — após o tooltip estar montado e position setada,
  // mede altura real e ajusta. Marca measured=true ao final, revelando o
  // tooltip pro usuário (visibility: visible). Single recalculation —
  // não loopa porque ou a altura real bate com a estimativa (entra no
  // else direto), ou bate na segunda passada após setPosition.
  useLayoutEffect(() => {
    if (!open || !tooltipRef.current || !position || measured) return
    const measuredHeight = tooltipRef.current.offsetHeight
    if (measuredHeight <= 0) {
      // Sem layout engine (jsdom em testes, ou display:none transitório):
      // confia na posição estimada e revela. Em produção real o
      // offsetHeight é >0 nesse ponto (post-DOM-mutation), então essa
      // branch só é tomada em ambiente de testes.
      setMeasured(true)
      return
    }

    const trigger = triggerRef.current
    if (!trigger) return
    const rect = trigger.getBoundingClientRect()
    const expectedTopPlacement = rect.top - measuredHeight - TOOLTIP_OFFSET
    const tolerancePx = 2

    if (
      position.placement === "top" &&
      Math.abs(position.top - expectedTopPlacement) > tolerancePx
    ) {
      // Altura real difere — recalcula. Mantém measured=false; próxima
      // passada deste effect entra no else.
      computePosition()
      return
    }
    // Posição estabilizada — revela.
    setMeasured(true)
  }, [open, position, measured, computePosition])

  // Reposiciona em scroll/resize enquanto aberto.
  useEffect(() => {
    if (!open) return

    function handleReposition() {
      computePosition()
    }

    window.addEventListener("scroll", handleReposition, true)
    window.addEventListener("resize", handleReposition)
    return () => {
      window.removeEventListener("scroll", handleReposition, true)
      window.removeEventListener("resize", handleReposition)
    }
  }, [open, computePosition])

  // ── Close on click outside ───────────────────────────────────────────────
  useEffect(() => {
    if (!open) return

    function handlePointerDown(e: PointerEvent) {
      const target = e.target as Node
      if (
        triggerRef.current &&
        !triggerRef.current.contains(target) &&
        tooltipRef.current &&
        !tooltipRef.current.contains(target)
      ) {
        close()
      }
    }

    document.addEventListener("pointerdown", handlePointerDown)
    return () => document.removeEventListener("pointerdown", handlePointerDown)
  }, [open, close])

  // ── Close on Escape ──────────────────────────────────────────────────────
  useEffect(() => {
    if (!open) return

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        close()
        triggerRef.current?.focus()
      }
    }

    document.addEventListener("keydown", handleKeyDown)
    return () => document.removeEventListener("keydown", handleKeyDown)
  }, [open, close])

  function handleTriggerKeyDown(e: React.KeyboardEvent<HTMLButtonElement>) {
    if (e.key === "Escape" && open) {
      e.stopPropagation()
      close()
    }
  }

  // Tooltip handlers — necessários porque o tooltip está em portal:
  // mouseLeave do trigger não enxerga o tooltip como destino.
  // Manter hover sustentado quando cursor está sobre o tooltip.
  function handleTooltipMouseEnter() {
    cancelScheduledClose()
    setHoverOpen(true)
  }

  function handleTooltipMouseLeave() {
    scheduleClose()
  }

  const tooltipNode = open && position && typeof document !== "undefined"
    ? createPortal(
        <div
          ref={tooltipRef}
          id={tooltipId}
          role={isDisclosure ? undefined : "tooltip"}
          aria-label={isDisclosure ? label : undefined}
          onMouseEnter={handleTooltipMouseEnter}
          onMouseLeave={handleTooltipMouseLeave}
          // A11Y-18: quando o link "Saiba mais" é o último Tab-stop do
          // popover, sair dele por Tab precisa fechar o popover — sem isto,
          // ele ficaria aberto (fixed, sobre o resto da página) até um clique
          // fora ou Escape.
          onBlur={(e) => {
            const next = e.relatedTarget as Node | null
            if (!triggerRef.current?.contains(next) && !tooltipRef.current?.contains(next)) {
              setHoverOpen(false)
            }
          }}
          style={{
            position: "fixed",
            top: position.top,
            left: position.left,
            // Lê o token em vez de repetir o número: o valor antigo estava
            // hardcoded e citava `--z-tooltip`, nome que nem existe mais.
            zIndex: "var(--z-index-tooltip)",
            maxWidth: TOOLTIP_MAX_WIDTH,
            // Render invisível até measured=true. Evita usuário ver o
            // tooltip na posição estimada e depois "saltar" pra final.
            visibility: measured ? "visible" : "hidden",
          }}
          className={cn(
            "bg-surface border border-border shadow-lg rounded-md p-3",
            "w-max text-xs",
            measured && "animate-fade-in",
          )}
        >
          {/* O label é a chave que a pessoa vê no formulário: mono, porque é
              nome de campo, não prosa. */}
          <p className="font-mono text-[11px] font-medium text-text mb-1">{label}</p>
          <p className="text-text-secondary leading-relaxed">{description}</p>
          {example != null && (
            <p className="mt-1.5 text-text-secondary">
              {t("helpTooltip.exampleLabel")}{" "}
              <code className="font-mono rounded bg-surface-tertiary px-1 text-text">
                {example}
              </code>
            </p>
          )}
          {learnMoreHref != null && (
            <a
              href={learnMoreHref}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-2 inline-block text-primary-600 hover:underline focus-ring rounded"
            >
              {t("helpTooltip.learnMore")}
            </a>
          )}
        </div>,
        document.body,
      )
    : null

  return (
    <span className={cn("relative inline-flex", className)}>
      <button
        ref={triggerRef}
        type="button"
        // A11Y-18: `aria-describedby` é o padrão do tooltip PURO (texto
        // informativo); com link focável dentro, o padrão correto é
        // `aria-haspopup`/`aria-controls` (disclosure), não describedby.
        aria-describedby={!isDisclosure && open ? tooltipId : undefined}
        aria-haspopup={isDisclosure ? "true" : undefined}
        aria-controls={isDisclosure && open ? tooltipId : undefined}
        aria-label={t("helpTooltip.triggerAriaLabel", { label })}
        aria-expanded={open}
        // Click "fixa" o tooltip aberto. Quando já está fixado, click fecha.
        onClick={(e) => {
          e.stopPropagation()
          cancelScheduledClose()
          setClickedOpen((prev) => !prev)
        }}
        onMouseEnter={() => {
          cancelScheduledClose()
          setHoverOpen(true)
        }}
        onMouseLeave={() => {
          // Debounce: dá tempo do cursor transitar do trigger pro tooltip
          // sem que o gap de poucos pixels (portal) feche prematuramente.
          scheduleClose()
        }}
        onFocus={() => {
          cancelScheduledClose()
          setHoverOpen(true)
        }}
        onKeyDown={handleTriggerKeyDown}
        onBlur={(e) => {
          // Não fecha se foco está indo pro próprio tooltip.
          if (!tooltipRef.current?.contains(e.relatedTarget as Node)) {
            setHoverOpen(false)
          }
        }}
        // A11Y-19: alvo de toque/clique era do tamanho do ícone (14px) —
        // abaixo do mínimo recomendado de 24px (WCAG 2.5.8).
        className="inline-flex min-h-6 min-w-6 items-center justify-center rounded text-text-tertiary transition-colors hover:text-text-secondary focus-ring"
      >
        <HelpCircleIcon size={14} aria-hidden="true" />
      </button>
      {tooltipNode}
    </span>
  )
}

export default HelpTooltip
