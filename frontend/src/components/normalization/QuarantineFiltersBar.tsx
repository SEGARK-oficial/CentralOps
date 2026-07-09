/**
 * QuarantineFiltersBar
 * Barra de filtros para a página de Quarentena.
 *
 * PR #3: refatorado sobre <FiltersToolbar> primitive — adiciona search
 * (debounce 300ms) por nome de integração e dropdown status (pending/
 * reprocessed/all). Mantém os 3 selects existentes (vendor, event_type,
 * error_kind).
 */

import type React from "react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Select } from "@/components/ui/Select/Select"
import { FiltersToolbar } from "@/components/ui/FiltersToolbar/FiltersToolbar"
import type { QuarantineFilters } from "@/hooks/useQuarantine"
import type { SelectOption } from "@/components/ui/Select/Select"

// Os valores DEVEM casar com os error_kind reais do backend
// (backend/app/collectors/quarantine.py): parse | map | validate |
// missing_customer_id | missing_mapping. O filtro é match exato no backend
// (QuarantineEvent.error_kind == error_kind), então valores divergentes
// (o conjunto antigo: schema_error/missing_required/…) zeravam todo filtro.
// Exportado (labels em pt-BR fixo) para o guard de regressão de valores —
// a renderização usa `errorKindOptions` localizado dentro do componente.
export const ERROR_KIND_OPTIONS: SelectOption[] = [
  { value: "map", label: "Erro de mapeamento" },
  { value: "parse", label: "Erro de parse" },
  { value: "validate", label: "Erro de validação" },
  { value: "missing_customer_id", label: "Customer ID ausente" },
  { value: "missing_mapping", label: "Mapping ausente" },
]

type StatusValue = "pending" | "reprocessed" | "all"

interface QuarantineFiltersBarProps {
  filters: QuarantineFilters
  vendorOptions: SelectOption[]
  eventTypeOptions: SelectOption[]
  onFiltersChange: (filters: QuarantineFilters) => void
}

export const QuarantineFiltersBar: React.FC<QuarantineFiltersBarProps> = ({
  filters,
  vendorOptions,
  eventTypeOptions,
  onFiltersChange,
}) => {
  const { t } = useTranslation("drift")

  const errorKindOptions: SelectOption[] = [
    { value: "map", label: t("quarantine.filters.errorKind.map") },
    { value: "parse", label: t("quarantine.filters.errorKind.parse") },
    { value: "validate", label: t("quarantine.filters.errorKind.validate") },
    { value: "missing_customer_id", label: t("quarantine.filters.errorKind.missing_customer_id") },
    { value: "missing_mapping", label: t("quarantine.filters.errorKind.missing_mapping") },
  ]

  const statusOptions: SelectOption[] = [
    { value: "pending", label: t("quarantine.filters.status.pending") },
    { value: "reprocessed", label: t("quarantine.filters.status.reprocessed") },
    { value: "all", label: t("quarantine.filters.status.all") },
  ]

  // Estado local do search input — o FiltersToolbar gere o debounce.
  const [searchValue, setSearchValue] = useState<string>(
    filters.integration_name ?? "",
  )

  // Sincroniza search local quando filtros externos mudam (ex: reset).
  useEffect(() => {
    setSearchValue(filters.integration_name ?? "")
  }, [filters.integration_name])

  const currentStatus: StatusValue = filters.status ?? "pending"

  const hasActiveFilters =
    !!filters.vendor ||
    !!filters.event_type ||
    !!filters.error_kind ||
    !!filters.integration_name ||
    currentStatus !== "pending"

  const handleReset = () => {
    setSearchValue("")
    onFiltersChange({ limit: filters.limit, offset: 0 })
  }

  return (
    <FiltersToolbar
      data-testid="quarantine-filters"
      search={{
        value: searchValue,
        onChange: setSearchValue,
        placeholder: t("quarantine.filters.searchPlaceholder"),
        label: t("quarantine.filters.searchLabel"),
        ariaLabel: t("quarantine.filters.searchAriaLabel"),
        debounceMs: 300,
        onDebouncedChange: (debounced) => {
          // Só dispara se o valor estabilizado for diferente do filtro
          // atual — evita loop "filters change → searchValue sync →
          // debounce dispara → filters change novamente".
          const next = debounced.trim()
          const current = filters.integration_name ?? ""
          if (next === current) return
          onFiltersChange({
            ...filters,
            integration_name: next === "" ? undefined : next,
            offset: 0,
          })
        },
      }}
      hasActiveFilters={hasActiveFilters}
      onReset={handleReset}
    >
      <div data-testid="filter-status">
        <Select
          label={t("quarantine.filters.statusLabel")}
          options={statusOptions}
          value={currentStatus}
          onValueChange={(v) =>
            onFiltersChange({
              ...filters,
              status: ((v as StatusValue) || "pending"),
              offset: 0,
            })
          }
          className="w-40"
          aria-label={t("quarantine.filters.statusAriaLabel")}
        />
      </div>

      <div data-testid="filter-vendor">
        <Select
          label={t("quarantine.filters.vendorLabel")}
          placeholder={t("quarantine.filters.vendorPlaceholder")}
          options={[{ value: "", label: t("quarantine.filters.vendorAll") }, ...vendorOptions]}
          value={filters.vendor ?? ""}
          onValueChange={(v) =>
            onFiltersChange({ ...filters, vendor: v === "" ? undefined : String(v), offset: 0 })
          }
          className="w-44"
          aria-label={t("quarantine.filters.vendorAriaLabel")}
        />
      </div>

      <div data-testid="filter-event-type">
        <Select
          label={t("quarantine.filters.eventTypeLabel")}
          placeholder={t("quarantine.filters.eventTypePlaceholder")}
          options={[{ value: "", label: t("quarantine.filters.eventTypeAll") }, ...eventTypeOptions]}
          value={filters.event_type ?? ""}
          onValueChange={(v) =>
            onFiltersChange({
              ...filters,
              event_type: v === "" ? undefined : String(v),
              offset: 0,
            })
          }
          className="w-52"
          aria-label={t("quarantine.filters.eventTypeAriaLabel")}
        />
      </div>

      <div data-testid="filter-error-kind">
        <Select
          label={t("quarantine.filters.errorKindLabel")}
          placeholder={t("quarantine.filters.errorKindPlaceholder")}
          options={[{ value: "", label: t("quarantine.filters.errorKindAll") }, ...errorKindOptions]}
          value={filters.error_kind ?? ""}
          onValueChange={(v) =>
            onFiltersChange({
              ...filters,
              error_kind: v === "" ? undefined : String(v),
              offset: 0,
            })
          }
          className="w-64"
          aria-label={t("quarantine.filters.errorKindAriaLabel")}
        />
      </div>
    </FiltersToolbar>
  )
}

export default QuarantineFiltersBar
