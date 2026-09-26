/**
 * TileGallery — grid de cards (tiles) com ícones para SELEÇÃO.
 *
 * Padrão de mercado (Cribl / Datadog / Fivetran / Airbyte / Axoflow): em vez de
 * um <select> básico, o usuário escolhe entre cards visuais com ícone, descrição
 * e busca/categoria. Generaliza a DestinationTypeGallery para
 * reuso em Integrações (single) e seleção de Destinos em Rotas (multiple).
 *
 * - Busca por label/id/descrição
 * - Filtro por categoria em chips (ocultado quando nenhum tile tem categoria)
 * - Single: radiogroup/radio · Multiple: cards com checkbox visual
 * - Acessível por teclado, foco visível (focus-ring do design system)
 */

import type React from "react"
import { useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { CheckIcon, SearchIcon } from "lucide-react"
import { cn } from "@/lib/utils"
import { Input } from "@/components/ui/Input/Input"
import { Badge } from "@/components/ui/Badge/Badge"

export interface Tile {
  id: string
  label: string
  description?: string
  icon: React.ReactNode
  /** Categoria opcional — habilita os chips de filtro quando presente. */
  category?: string
  /** Badge opcional (ex.: "Beta", "Nativo"). */
  badge?: string
  badgeTone?: "default" | "outline" | "warning" | "success" | "primary"
}

export interface TileGalleryProps {
  tiles: Tile[]
  /** Selecionado(s): string (single) ou string[] (multiple). */
  value: string | string[]
  /** Single: seleciona o id. Multiple: alterna (toggle) o id. */
  onChange: (id: string) => void
  multiple?: boolean
  disabled?: boolean
  showSearch?: boolean
  searchPlaceholder?: string
  emptyLabel?: string
  ariaLabel?: string
  columns?: 2 | 3 | 4
}

// Grade intrínseca por largura de coluna (auto-fill + minmax) em vez de
// breakpoints de viewport. Em Tailwind v4 `sm:`/`lg:` reagem à VIEWPORT, não ao
// contêiner — quando a galeria vive numa célula estreita (ex.: dentro de um
// modal), os tiles esticavam para a largura da célula. Com auto-fill os tracks
// têm largura mínima fixa e os vazios absorvem o espaço extra (auto-fill, não
// auto-fit), mantendo os cards consistentes e sem esticar.
// LAY-12: `minmax(220px,1fr)` força a coluna a ter NO MÍNIMO 220px mesmo
// quando o container inteiro é mais estreito que isso (ex.: modal em telas
// pequenas) — a grade estourava e criava scroll horizontal. `min(220px,100%)`
// deixa a coluna encolher até caber quando o container é menor que o mínimo.
const COL_CLASS: Record<2 | 3 | 4, string> = {
  2: "grid-cols-[repeat(auto-fill,minmax(min(240px,100%),1fr))]",
  3: "grid-cols-[repeat(auto-fill,minmax(min(220px,100%),1fr))]",
  4: "grid-cols-[repeat(auto-fill,minmax(min(180px,100%),1fr))]",
}

// Sentinela interno da categoria "todas": o valor exibido vem do i18n
// (common:states.all). Antes era a string PT "Todos", que fazia papel de
// chave E de rótulo e aparecia em PT em qualquer idioma.
const ALL_CATEGORIES = "__all__"

export const TileGallery: React.FC<TileGalleryProps> = ({
  tiles,
  value,
  onChange,
  multiple = false,
  disabled = false,
  showSearch = true,
  searchPlaceholder,
  emptyLabel,
  ariaLabel,
  columns = 3,
}) => {
  const { t: tCommon } = useTranslation("common")
  const searchPlaceholderText = searchPlaceholder ?? `${tCommon("actions.search")}…`
  const emptyLabelText = emptyLabel ?? tCommon("states.noResults")
  const ariaLabelText = ariaLabel ?? tCommon("actions.select")
  const [search, setSearch] = useState("")
  const [category, setCategory] = useState<string>(ALL_CATEGORIES)
  const categoryGroupRef = useRef<HTMLDivElement>(null)

  const selected = useMemo(
    () => (Array.isArray(value) ? new Set(value) : new Set([value])),
    [value],
  )

  const categories = useMemo(() => {
    const cats = new Set<string>()
    for (const t of tiles) if (t.category) cats.add(t.category)
    return cats.size > 0 ? [ALL_CATEGORIES, ...Array.from(cats).sort()] : []
  }, [tiles])

  // A11Y-37: roving tabindex + setas — o container já era `role="group"` com
  // filhos `role="radio"`, mas sem isso um radiogroup só é navegável clicando
  // (Tab entra e sai do grupo INTEIRO de uma vez, sem mover ENTRE as opções).
  function handleCategoryKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return
    if (categories.length === 0) return
    const radios = Array.from(
      categoryGroupRef.current?.querySelectorAll<HTMLButtonElement>('[role="radio"]') ?? [],
    )
    const currentIndex = Math.max(0, categories.indexOf(category))
    let nextIndex = currentIndex
    if (e.key === "ArrowRight") nextIndex = (currentIndex + 1) % categories.length
    else if (e.key === "ArrowLeft") nextIndex = (currentIndex - 1 + categories.length) % categories.length
    else if (e.key === "Home") nextIndex = 0
    else if (e.key === "End") nextIndex = categories.length - 1
    e.preventDefault()
    setCategory(categories[nextIndex])
    radios[nextIndex]?.focus()
  }

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return tiles.filter((t) => {
      if (category !== ALL_CATEGORIES && t.category !== category) return false
      if (
        q &&
        !t.label.toLowerCase().includes(q) &&
        !t.id.toLowerCase().includes(q) &&
        !(t.description ?? "").toLowerCase().includes(q)
      )
        return false
      return true
    })
  }, [tiles, search, category])

  return (
    <div className="space-y-4">
      {showSearch && (
        <Input
          placeholder={searchPlaceholderText}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          leftIcon={<SearchIcon size={16} />}
          aria-label={searchPlaceholderText}
          disabled={disabled}
          data-testid="tile-search"
        />
      )}

      {categories.length > 0 && (
        <div
          ref={categoryGroupRef}
          role="radiogroup"
          aria-label={tCommon("actions.filterByCategory")}
          onKeyDown={handleCategoryKeyDown}
          className="flex flex-wrap gap-2"
          data-testid="tile-categories"
        >
          {categories.map((cat) => (
            <button
              key={cat}
              type="button"
              role="radio"
              aria-checked={category === cat}
              // A11Y-37: roving tabindex — só a opção selecionada é uma parada
              // de Tab; as demais se alcançam pelas setas (ArrowLeft/Right,
              // Home/End), padrão ARIA de radiogroup.
              tabIndex={category === cat ? 0 : -1}
              disabled={disabled}
              onClick={() => setCategory(cat)}
              className={cn(
                "inline-flex items-center rounded-full px-3 py-1 text-xs font-medium transition-colors focus-ring",
                "disabled:cursor-not-allowed disabled:opacity-50",
                category === cat
                  ? "bg-primary-100 text-primary-700 ring-1 ring-primary-600"
                  : "bg-surface-tertiary text-text-secondary hover:bg-surface hover:text-text border border-border",
              )}
              data-testid={`tile-cat-${cat === ALL_CATEGORIES ? "all" : cat.toLowerCase()}`}
            >
              {cat === ALL_CATEGORIES ? tCommon("states.all") : cat}
            </button>
          ))}
        </div>
      )}

      {filtered.length === 0 ? (
        <p className="py-8 text-center text-sm text-text-tertiary" data-testid="tile-empty">
          {emptyLabelText}
        </p>
      ) : (
        <div
          role={multiple ? "group" : "radiogroup"}
          aria-label={ariaLabelText}
          className={cn("grid gap-3", COL_CLASS[columns])}
          data-testid="tile-grid"
        >
          {filtered.map((t) => {
            const isSelected = selected.has(t.id)
            return (
              <button
                key={t.id}
                type="button"
                role={multiple ? "checkbox" : "radio"}
                aria-checked={isSelected}
                aria-label={tCommon("actions.selectItem", { label: t.label })}
                disabled={disabled}
                onClick={() => onChange(t.id)}
                data-testid={`tile-card-${t.id}`}
                className={cn(
                  "relative flex min-h-[120px] flex-col items-start gap-3 rounded-lg border p-4 text-left transition-all focus-ring",
                  "disabled:cursor-not-allowed disabled:opacity-50",
                  !isSelected && "border-border bg-surface hover:border-primary-300 hover:bg-surface-secondary",
                  isSelected && "border-primary-600 bg-primary-50 ring-1 ring-primary-600",
                )}
              >
                <div className="flex w-full items-start justify-between gap-2">
                  <span
                    className={cn(
                      "rounded-md p-2",
                      isSelected ? "bg-primary-100 text-primary-700" : "bg-surface-tertiary text-text-secondary",
                    )}
                  >
                    {t.icon}
                  </span>
                  <div className="flex items-center gap-1">
                    {t.badge && (
                      <Badge variant={t.badgeTone ?? "outline"} size="sm">
                        {t.badge}
                      </Badge>
                    )}
                    {/* Indicador de seleção (checkbox no modo multiple). */}
                    {multiple && (
                      <span
                        aria-hidden="true"
                        className={cn(
                          "flex h-5 w-5 items-center justify-center rounded border",
                          isSelected
                            ? "border-primary-600 bg-primary-600 text-text-inverse"
                            : "border-border bg-surface",
                        )}
                      >
                        {isSelected && <CheckIcon size={14} />}
                      </span>
                    )}
                  </div>
                </div>

                <div className="space-y-1">
                  <span className={cn("block text-sm font-semibold", isSelected ? "text-primary-700" : "text-text")}>
                    {t.label}
                  </span>
                  {t.description && (
                    <span className="block text-xs leading-relaxed text-text-tertiary">{t.description}</span>
                  )}
                </div>

                {t.category && (
                  <Badge variant="default" size="sm" className="mt-auto">
                    {t.category}
                  </Badge>
                )}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
