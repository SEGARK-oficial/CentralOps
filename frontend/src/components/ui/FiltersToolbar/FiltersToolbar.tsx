/**
 * FiltersToolbar
 * --------------
 * Componente presentational que combina:
 *   - input de busca controlado COM debounce interno (callback chamado
 *     após `debounceMs`)
 *   - slot horizontal para selects/dropdowns custom (children)
 *   - botão "Resetar" condicional
 *
 * Uso típico:
 *
 *   <FiltersToolbar
 *     search={{
 *       value: searchInput,
 *       onChange: setSearchInput,
 *       placeholder: "Buscar...",
 *       debounceMs: 300,
 *       onDebouncedChange: (v) => setQuery(v),
 *       label: "Buscar",
 *     }}
 *     hasActiveFilters={!!query || stateFilter !== "all"}
 *     onReset={() => { setQuery(""); setStateFilter("all"); }}
 *   >
 *     <Select label="Estado" .../>
 *   </FiltersToolbar>
 *
 * Notas:
 *   - O caller mantém o estado da string do input via `search.value` e
 *     `search.onChange`. O hook interno só dispara `onDebouncedChange`
 *     com o valor estabilizado.
 *   - Se `onDebouncedChange` não for passado, o componente apenas exibe
 *     o input sem disparar nada extra (o caller cuida).
 */

import type React from "react"
import { useEffect } from "react"
import { useTranslation } from "react-i18next"
import { Input } from "@/components/ui/Input/Input"
import { Button } from "@/components/ui/Button/Button"
import { useDebounce } from "@/hooks/useDebounce"
import { cn } from "@/lib/utils"

export interface FiltersToolbarSearchProps {
  /** Valor atual do input (controlado). */
  value: string
  /** Atualiza o valor imediato (a cada keystroke). */
  onChange: (next: string) => void
  /** Placeholder do input. */
  placeholder?: string
  /** Label visível acima do input (opcional). */
  label?: string
  /** aria-label do input. */
  ariaLabel?: string
  /** Tempo de debounce em ms para `onDebouncedChange`. Default: 300. */
  debounceMs?: number
  /** Callback chamado com valor estabilizado após `debounceMs`. */
  onDebouncedChange?: (debounced: string) => void
}

export interface FiltersToolbarProps {
  /** Configuração do search input. Opcional — se ausente, só renderiza children. */
  search?: FiltersToolbarSearchProps
  /** Sinaliza se algum filtro está ativo (controla visibilidade do botão Resetar). */
  hasActiveFilters?: boolean
  /** Handler do botão Resetar. Botão só aparece se `hasActiveFilters && onReset`. */
  onReset?: () => void
  /** Slot horizontal para dropdowns/selects custom. */
  children?: React.ReactNode
  /** Classes extras para o container. */
  className?: string
  /** Test-id do container (default: "filters-toolbar"). */
  "data-testid"?: string
}

export const FiltersToolbar: React.FC<FiltersToolbarProps> = ({
  search,
  hasActiveFilters = false,
  onReset,
  children,
  className,
  "data-testid": dataTestId = "filters-toolbar",
}) => {
  const { t } = useTranslation("ui")
  // Debounce interno do search — só dispara callback quando o caller pediu.
  const debouncedValue = useDebounce(search?.value ?? "", search?.debounceMs ?? 300)

  useEffect(() => {
    if (!search?.onDebouncedChange) return
    search.onDebouncedChange(debouncedValue)
    // queremos disparar quando o valor estabilizado mudar
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedValue])

  const showReset = hasActiveFilters && typeof onReset === "function"

  return (
    <div
      data-testid={dataTestId}
      className={cn(
        "flex flex-col gap-2 md:flex-row md:items-end",
        className,
      )}
    >
      {search && (
        <div className="flex-1 min-w-[200px]" data-testid={`${dataTestId}-search`}>
          <Input
            label={search.label}
            placeholder={search.placeholder}
            value={search.value}
            onChange={(e) => search.onChange(e.target.value)}
            aria-label={search.ariaLabel ?? search.label ?? t("filtersToolbar.searchAriaLabel")}
          />
        </div>
      )}

      {children}

      {showReset && (
        <div className="md:self-end">
          <Button
            variant="ghost"
            size="sm"
            onClick={onReset}
            data-testid={`${dataTestId}-reset`}
            aria-label={t("filtersToolbar.resetAriaLabel")}
          >
            {t("filtersToolbar.reset")}
          </Button>
        </div>
      )}
    </div>
  )
}

export default FiltersToolbar
