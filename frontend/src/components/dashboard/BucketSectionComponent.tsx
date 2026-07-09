import type React from "react"
import { ChevronRightIcon } from "lucide-react"
import { useNavigate } from "react-router-dom"
import { Badge } from "@/components/ui/Badge/Badge"
import { Card } from "@/components/ui/Card/Card"
import { iconFor } from "@/lib/icons"
import { cn } from "@/lib/utils"
import type { BucketItem, BucketSection, DashSeverity } from "@/types"

const SEVERITY_VARIANT: Record<DashSeverity, "success" | "warning" | "danger" | "primary"> = {
  ok: "success",
  warn: "warning",
  critical: "danger",
  info: "primary",
}

interface BucketItemRowProps {
  item: BucketItem
}

const BucketItemRow: React.FC<BucketItemRowProps> = ({ item }) => {
  const navigate = useNavigate()
  const isClickable = Boolean(item.href)

  const inner = (
    <div
      className={cn(
        "flex items-center justify-between gap-3 rounded-lg px-3 py-2.5 text-sm",
        isClickable
          ? "cursor-pointer hover:bg-surface-tertiary transition-colors"
          : "cursor-default",
      )}
    >
      <div className="min-w-0">
        <span className="block truncate font-medium text-text">{item.label}</span>
        {item.sub && (
          <span className="block text-xs text-text-secondary truncate">{item.sub}</span>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {item.severity && (
          <Badge variant={SEVERITY_VARIANT[item.severity]} size="sm">
            {item.severity}
          </Badge>
        )}
        <span className="text-sm font-semibold text-text tabular-nums">{item.value}</span>
        {isClickable && (
          <ChevronRightIcon
            size={14}
            className="text-text-tertiary"
            aria-hidden="true"
          />
        )}
      </div>
    </div>
  )

  if (!isClickable) return <div>{inner}</div>

  return (
    <button
      type="button"
      className="w-full text-left"
      onClick={() => {
        if (item.href) {
          if (item.href.startsWith("http")) {
            window.open(item.href, "_blank", "noopener,noreferrer")
          } else {
            navigate(item.href)
          }
        }
      }}
      aria-label={`${item.label}: ${item.value}`}
    >
      {inner}
    </button>
  )
}

// ── BucketSectionComponent ────────────────────────────────────────────────────

interface BucketSectionComponentProps {
  section: BucketSection
}

export const BucketSectionComponent: React.FC<BucketSectionComponentProps> = ({ section }) => {
  const Icon = iconFor(section.icon_id)
  const emptyText = section.empty_hint ?? "Sem dados na janela atual."

  return (
    <Card padding="md" className="shadow-sm">
      <div className="flex items-center gap-2 mb-4">
        <Icon size={16} className="text-text-tertiary" aria-hidden="true" />
        <h3 className="font-semibold text-text">{section.label}</h3>
      </div>

      {section.items.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-text-secondary">
          {emptyText}
        </div>
      ) : (
        <div className="divide-y divide-border">
          {section.items.map((item) => (
            <BucketItemRow key={item.id} item={item} />
          ))}
        </div>
      )}
    </Card>
  )
}
