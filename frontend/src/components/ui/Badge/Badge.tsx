import type React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

/**
 * Badge — é aqui que o estado aparece.
 *
 * A matiz codifica o estágio do pipeline ou o estado, nunca decora. Por isso
 * `default` é cinza: um item que está no lugar não precisa chamar, e é esse
 * silêncio que faz o âmbar registrar quando aparece. Reserve `success` para uma
 * MUDANÇA que vale ver (reduziu 40%), não para "continua ok" — este é `default`.
 *
 * Estágio e estado compartilham a paleta porque as leituras coincidem: coleta é
 * âmbar pelo mesmo motivo que "precisa de atenção" é âmbar; detecção é
 * vermelhão pelo mesmo motivo que falha é vermelhão. Por isso NÃO existe aqui
 * um segundo jogo de variantes `collect`/`normalize`/`reduce`/`detect`: seriam
 * quatro nomes novos para os mesmos pixels de `warning`/`primary`/`success`/
 * `danger`, e um dia alguém ajusta um lado só.
 *
 * O estágio já tem idioma próprio no console — os tokens `stage-*`, usados
 * direto em Navigation, KpiGrid, FlowPage e DestinationHealthGrid. Selo que
 * precise do ciano de "roteado" (a única matiz sem escala de estado
 * equivalente) escreve `className="bg-stage-route/15 text-stage-route"`: mesmo
 * token, sem duplicar a API do componente.
 *
 * Conteúdo puramente numérico entra em mono sozinho: contagem é dado, e dado
 * alinha por coluna. Use `mono` para forçar em id/hash/chave.
 */
const badgeVariants = cva(
  "inline-flex items-center gap-1 font-medium rounded-full whitespace-nowrap",
  {
    variants: {
      variant: {
        // ── Neutro: o padrão. Nada acontecendo, nada gritando. ──
        default: "bg-surface-tertiary text-text-secondary",
        outline: "border border-border text-text-secondary",
        // ── Estado ──
        primary: "bg-primary-100 text-primary-700",
        success: "bg-success-50 text-success-700",
        warning: "bg-warning-50 text-warning-700",
        danger: "bg-danger-50 text-danger-700",
      },
      size: {
        sm: "px-2 py-0.5 text-xs",
        md: "px-2.5 py-0.5 text-xs",
        lg: "px-3 py-1 text-sm",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "md",
    },
  },
)

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {
  dot?: boolean
  dotColor?: string
  /** Força mono. Conteúdo só-número já entra em mono sem isto. */
  mono?: boolean
}

/** Só-número, número formatado ou percentual: é leitura de painel, vai em mono. */
const looksNumeric = (node: React.ReactNode): boolean => {
  if (typeof node === "number") return true
  if (typeof node !== "string") return false
  const s = node.trim()
  return /\d/.test(s) && /^[\d.,\s]+%?$/.test(s)
}

export const Badge: React.FC<BadgeProps> = ({ className, variant, size, dot, dotColor, mono, children, ...props }) => (
  <span
    className={cn(
      badgeVariants({ variant, size }),
      (mono ?? looksNumeric(children)) && "font-mono tabular-nums",
      className,
    )}
    {...props}
  >
    {dot && (
      <span
        className="w-1.5 h-1.5 rounded-full"
        style={{ backgroundColor: dotColor || "currentColor" }}
        aria-hidden="true"
      />
    )}
    {children}
  </span>
)
