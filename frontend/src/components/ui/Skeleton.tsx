/**
 * Skeleton — bloco placeholder com animação pulse para estados de carregamento.
 *
 * prefers-reduced-motion é tratado globalmente via CSS (Tailwind animate-pulse
 * respeita @media (prefers-reduced-motion: reduce) no build padrão).
 *
 * Variantes disponíveis:
 *   Skeleton       — bloco genérico (base)
 *   SkeletonText   — N linhas de texto; última linha mais curta (realismo)
 *   SkeletonCard   — card com header + linhas de conteúdo
 *   SkeletonTable  — tabela com N linhas x M colunas
 */

import type React from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"

// ── Skeleton base ─────────────────────────────────────────────────────────────

interface SkeletonProps {
  className?: string
  /** Largura inline (ex.: "60%"). Prefira classes Tailwind quando possível. */
  width?: string
  /** Altura inline (ex.: "1rem"). Prefira classes Tailwind quando possível. */
  height?: string
}

/**
 * Bloco base com pulse animado. Oculto de leitores de tela (aria-hidden).
 * Coloque role="status" + aria-label no container pai para anunciar o carregamento.
 */
export const Skeleton: React.FC<SkeletonProps> = ({ className, width, height }) => (
  <div
    aria-hidden="true"
    className={cn(
      "animate-pulse rounded bg-surface-tertiary",
      className,
    )}
    style={{ width, height }}
  />
)

// ── SkeletonText ──────────────────────────────────────────────────────────────

interface SkeletonTextProps {
  /** Número de linhas a renderizar (padrão: 3). */
  lines?: number
  className?: string
}

/**
 * Linhas de texto placeholder; a última linha é mais curta (≈ 60 %) para
 * simular um parágrafo real.
 */
export const SkeletonText: React.FC<SkeletonTextProps> = ({ lines = 3, className }) => {
  const { t } = useTranslation("ui")
  return (
    <div
      role="status"
      aria-label={t("skeleton.loadingText")}
      className={cn("flex flex-col gap-2", className)}
    >
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton
          key={i}
          className="h-4"
          width={i === lines - 1 ? "60%" : "100%"}
        />
      ))}
    </div>
  )
}

// ── SkeletonCard ──────────────────────────────────────────────────────────────

interface SkeletonCardProps {
  /** Número de linhas de conteúdo abaixo do header (padrão: 3). */
  lines?: number
  className?: string
}

/**
 * Placeholder para um card com avatar/título no topo e linhas de conteúdo.
 */
export const SkeletonCard: React.FC<SkeletonCardProps> = ({ lines = 3, className }) => {
  const { t } = useTranslation("ui")
  return (
    <div
      role="status"
      aria-label={t("skeleton.loadingCard")}
      className={cn("rounded-lg border border-border p-4 flex flex-col gap-4", className)}
    >
      {/* Header: ícone + título */}
      <div className="flex items-center gap-3">
        <Skeleton className="h-10 w-10 rounded-full shrink-0" />
        <div className="flex flex-col gap-2 flex-1">
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-3 w-1/3" />
        </div>
      </div>
      {/* Corpo */}
      <SkeletonText lines={lines} />
    </div>
  )
}

// ── SkeletonTable ─────────────────────────────────────────────────────────────

interface SkeletonTableProps {
  /** Número de linhas de dados (padrão: 5). */
  rows?: number
  /** Número de colunas (padrão: 4). */
  columns?: number
  className?: string
}

/**
 * Placeholder para tabelas; renderiza header + N linhas × M colunas.
 */
export const SkeletonTable: React.FC<SkeletonTableProps> = ({
  rows = 5,
  columns = 4,
  className,
}) => {
  const { t } = useTranslation("ui")
  return (
    <div
      role="status"
      aria-label={t("skeleton.loadingTable")}
      className={cn("w-full overflow-hidden rounded-lg border border-border", className)}
    >
      {/* Cabeçalho */}
      <div className="flex gap-4 border-b border-border bg-surface-tertiary px-4 py-3">
        {Array.from({ length: columns }).map((_, c) => (
          <Skeleton key={c} className="h-4 flex-1" />
        ))}
      </div>
      {/* Linhas */}
      {Array.from({ length: rows }).map((_, r) => (
        <div
          key={r}
          className="flex gap-4 border-b border-border px-4 py-3 last:border-b-0"
        >
          {Array.from({ length: columns }).map((_, c) => (
            <Skeleton key={c} className="h-4 flex-1" />
          ))}
        </div>
      ))}
    </div>
  )
}

export default Skeleton
