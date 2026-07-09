/**
 * DriftFiltersBar
 * Barra de filtros para o Drift Explorer.
 * Vendor e EventType são derivados da listagem de mappings existentes.
 */

import type React from "react"
import { useTranslation } from "react-i18next"
import { RotateCcwIcon } from "lucide-react"
import { Select } from "@/components/ui/Select/Select"
import { Button } from "@/components/ui/Button/Button"
import { Badge } from "@/components/ui/Badge/Badge"
import type { DriftFilters } from "@/hooks/useDrift"
import type { SelectOption } from "@/components/ui/Select/Select"

interface DriftFiltersBarProps {
  filters: DriftFilters
  vendorOptions: SelectOption[]
  eventTypeOptions: SelectOption[]
  onFiltersChange: (filters: DriftFilters) => void
}

export const DriftFiltersBar: React.FC<DriftFiltersBarProps> = ({
  filters,
  vendorOptions,
  eventTypeOptions,
  onFiltersChange,
}) => {
  const { t } = useTranslation("drift")
  const activeStatus = filters.status ?? "all"

  const STATUS_OPTIONS: Array<{ value: DriftFilters["status"] | "all"; label: string }> = [
    { value: "all", label: t("filters.status.all") },
    { value: "new", label: t("filters.status.new") },
    { value: "ignored", label: t("filters.status.ignored") },
    { value: "mapped", label: t("filters.status.mapped") },
  ]

  const handleReset = () => {
    onFiltersChange({ limit: filters.limit, offset: 0 })
  }

  const hasActiveFilters =
    !!filters.vendor || !!filters.event_type || !!filters.status

  return (
    <div className="flex flex-wrap items-end gap-3">
      <div data-testid="filter-vendor">
        <Select
          label={t("filters.vendorLabel")}
          placeholder={t("filters.vendorPlaceholder")}
          options={[{ value: "", label: t("filters.vendorAll") }, ...vendorOptions]}
          value={filters.vendor ?? ""}
          onValueChange={(v) =>
            onFiltersChange({ ...filters, vendor: v === "" ? undefined : String(v), offset: 0 })
          }
          className="w-44"
          aria-label={t("filters.vendorAriaLabel")}
        />
      </div>

      <div data-testid="filter-event-type">
        <Select
          label={t("filters.eventTypeLabel")}
          placeholder={t("filters.eventTypePlaceholder")}
          options={[{ value: "", label: t("filters.eventTypeAll") }, ...eventTypeOptions]}
          value={filters.event_type ?? ""}
          onValueChange={(v) =>
            onFiltersChange({
              ...filters,
              event_type: v === "" ? undefined : String(v),
              offset: 0,
            })
          }
          className="w-52"
          aria-label={t("filters.eventTypeAriaLabel")}
        />
      </div>

      <div className="flex flex-col gap-1.5">
        <span className="text-sm font-medium text-text">{t("filters.statusLabel")}</span>
        <div
          className="flex items-center gap-1"
          role="group"
          aria-label={t("filters.statusGroupAriaLabel")}
          data-testid="filter-status"
        >
          {STATUS_OPTIONS.map((opt) => {
            const isActive = activeStatus === opt.value
            return (
              <button
                key={opt.value ?? "all"}
                type="button"
                onClick={() =>
                  onFiltersChange({
                    ...filters,
                    status: opt.value === "all" ? undefined : opt.value,
                    offset: 0,
                  })
                }
                className="cursor-pointer focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 rounded-full"
                aria-pressed={isActive}
              >
                <Badge
                  variant={isActive ? "primary" : "default"}
                  size="md"
                >
                  {opt.label}
                </Badge>
              </button>
            )
          })}
        </div>
      </div>

      {hasActiveFilters && (
        <Button
          variant="ghost"
          size="sm"
          onClick={handleReset}
          leftIcon={<RotateCcwIcon size={14} />}
          aria-label={t("filters.resetAriaLabel")}
        >
          {t("filters.reset")}
        </Button>
      )}
    </div>
  )
}

export default DriftFiltersBar
