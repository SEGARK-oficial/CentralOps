/**
 * DestinationTypeGallery — galeria em grid para seleção do tipo de destino.
 *
 * 100% PLUGIN-DRIVEN (simetria com a galeria de integrações): ícone, categoria,
 * descrição e tier vêm do CATÁLOGO do backend (`GET /collectors/destinations/
 * destination-types` → `DestinationRegistration.describe()`), NÃO de mapas
 * hardcoded aqui. Adicionar um destino novo = registrar no backend; esta tela
 * NÃO muda e o card aparece com ícone de marca + categoria automaticamente.
 *
 * - Busca por label/kind (debounced via state)
 * - Filtro por categoria (chips derivados do próprio catálogo)
 * - Ícones de marca via `brandIconFor(icon_id)` (fallback p/ glifo genérico)
 * - Badge de tier (Beta/Genérico) quando aplicável
 */

import type React from "react"
import { useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { SearchIcon } from "lucide-react"
import { cn } from "@/lib/utils"
import { Input } from "@/components/ui/Input/Input"
import { Badge } from "@/components/ui/Badge/Badge"
import { brandIconFor } from "@/lib/brand-icons"
import type { DestinationType } from "@/types"

// ── Back-compat: heurística kind → icon_id de marca ───────────────────────────
// Usada APENAS quando o catálogo não traz `icon_id` (destinos legados) e pelo
// `kindToIcon` exportado para o RouteForm. O caminho normal usa `t.icon_id`.
function iconIdForKind(kind: string): string {
  const k = kind.toLowerCase()
  if (k.includes("splunk") || k.includes("hec")) return "splunk"
  if (k.includes("elastic") || k.includes("opensearch")) return "elastic"
  if (k.includes("clickhouse")) return "clickhouse"
  if (k.includes("crowdstrike") || k.includes("logscale") || k.includes("ngsiem")) return "crowdstrike"
  if (k.includes("sentinel")) return "microsoftsentinel"
  if (k.includes("chronicle") || k.includes("secops")) return "chronicle"
  if (k.includes("datadog")) return "datadog"
  if (k.includes("otlp") || k.includes("otel") || k.includes("telemetry")) return "opentelemetry"
  if (k.includes("security_lake") || k.includes("securitylake")) return "amazonsecuritylake"
  if (k.includes("s3") || k.includes("object")) return "amazons3"
  if (k.includes("kafka")) return "apachekafka"
  if (k.includes("syslog")) return "syslog"
  if (k.includes("webhook") || k.includes("http")) return "webhook"
  if (k.includes("jsonl") || k.includes("file")) return "jsonl"
  return kind
}

/** Back-compat para `RouteForm` (importa `kindToIcon`). Usa ícones de marca. */
export function kindToIcon(kind: string, size = 28): React.ReactNode {
  return brandIconFor(iconIdForKind(kind), { size })
}

// ── Tier → badge ──────────────────────────────────────────────────────────────

function tierBadge(
  tier: string | undefined,
  t: (key: string) => string,
): { label: string; variant: "warning" | "default" } | null {
  if (tier === "beta") return { label: t("typeGallery.tierBeta"), variant: "warning" }
  if (tier === "generic") return { label: t("typeGallery.tierGeneric"), variant: "default" }
  return null // "stable" (default) não recebe badge — reduz ruído visual
}

// ── Componente ────────────────────────────────────────────────────────────────

export interface DestinationTypeGalleryProps {
  /** Catálogo de tipos vindo da API (GET /collectors/destination-types). */
  catalog: DestinationType[]
  /** Kind atualmente selecionado (controlled). */
  selectedKind: string
  /** Callback quando o usuário seleciona um card. */
  onSelect: (kind: string) => void
  /** Desabilita todos os cards (ex.: enquanto salva). */
  disabled?: boolean
}

export const DestinationTypeGallery: React.FC<DestinationTypeGalleryProps> = ({
  catalog,
  selectedKind,
  onSelect,
  disabled = false,
}) => {
  const { t } = useTranslation("destinations")
  const ALL = t("typeGallery.allCategory")
  const OTHER = t("typeGallery.otherCategory")
  const [search, setSearch] = useState("")
  const [category, setCategory] = useState<string>(ALL)

  // Categorias derivadas do catálogo (curadas pelo backend via `order`/`category`).
  const categories = useMemo<string[]>(() => {
    const seen: string[] = []
    for (const dest of catalog) {
      const c = dest.category || OTHER
      if (!seen.includes(c)) seen.push(c)
    }
    return [ALL, ...seen]
  }, [catalog, ALL, OTHER])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return catalog.filter((dest) => {
      const cat = dest.category || OTHER
      if (category !== ALL && cat !== category) return false
      if (q && !dest.label.toLowerCase().includes(q) && !dest.kind.toLowerCase().includes(q)) return false
      return true
    })
  }, [catalog, search, category, ALL, OTHER])

  return (
    <div className="space-y-4">
      <Input
        placeholder={t("typeGallery.searchPlaceholder")}
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        leftIcon={<SearchIcon size={16} />}
        aria-label={t("typeGallery.searchAriaLabel")}
        disabled={disabled}
        data-testid="gallery-search"
      />

      {/* Chips de categoria (derivados do catálogo) */}
      <div
        role="group"
        aria-label={t("typeGallery.categoryGroupAriaLabel")}
        className="flex flex-wrap gap-2"
        data-testid="gallery-categories"
      >
        {categories.map((cat) => (
          <button
            key={cat}
            type="button"
            role="radio"
            aria-checked={category === cat}
            disabled={disabled}
            onClick={() => setCategory(cat)}
            className={cn(
              "inline-flex items-center rounded-full px-3 py-1 text-xs font-medium transition-colors focus-ring",
              "disabled:cursor-not-allowed disabled:opacity-50",
              category === cat
                ? "bg-primary-100 text-primary-700 ring-1 ring-primary-600"
                : "bg-surface-tertiary text-text-secondary hover:bg-surface hover:text-text border border-border",
            )}
            data-testid={`gallery-cat-${cat.toLowerCase()}`}
          >
            {cat}
          </button>
        ))}
      </div>

      {filtered.length === 0 ? (
        <p className="py-8 text-center text-sm text-text-tertiary" data-testid="gallery-empty">
          {t("typeGallery.emptyMessage", { query: search || category })}
        </p>
      ) : (
        <div
          role="radiogroup"
          aria-label={t("typeGallery.gridAriaLabel")}
          className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3"
          data-testid="gallery-grid"
        >
          {filtered.map((dest) => {
            const isSelected = selectedKind === dest.kind
            const tier = tierBadge(dest.tier, t)
            const iconId = dest.icon_id || iconIdForKind(dest.kind)
            return (
              <button
                key={dest.kind}
                type="button"
                role="radio"
                aria-checked={isSelected}
                aria-label={t("typeGallery.selectAriaLabel", { label: dest.label })}
                disabled={disabled}
                onClick={() => onSelect(dest.kind)}
                data-testid={`gallery-card-${dest.kind}`}
                className={cn(
                  "flex flex-col items-start gap-3 rounded-lg border p-4 text-left transition-all focus-ring",
                  "disabled:cursor-not-allowed disabled:opacity-50",
                  !isSelected && "border-border bg-surface hover:border-primary-300 hover:bg-surface-secondary",
                  isSelected && "border-primary-600 bg-primary-50 ring-1 ring-primary-600",
                )}
              >
                <div className="flex w-full items-start justify-between gap-2">
                  {/* Chip claro fixo p/ logos de marca lerem em light + dark mode. */}
                  <span className="flex h-11 w-11 items-center justify-center rounded-lg bg-white ring-1 ring-border shadow-sm">
                    {brandIconFor(iconId, { size: 26 })}
                  </span>
                  {tier && (
                    <Badge variant={tier.variant} size="sm">
                      {tier.label}
                    </Badge>
                  )}
                </div>

                <div className="space-y-1">
                  <span className={cn("block text-sm font-semibold", isSelected ? "text-primary-700" : "text-text")}>
                    {dest.label}
                  </span>
                  <span className="block text-xs leading-relaxed text-text-tertiary">
                    {dest.description || t("typeGallery.defaultDescription")}
                  </span>
                </div>

                <Badge variant="default" size="sm" className="mt-auto">
                  {dest.category || OTHER}
                </Badge>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
