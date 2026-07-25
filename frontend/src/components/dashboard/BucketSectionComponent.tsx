import type React from "react"
import { useTranslation } from "react-i18next"
import { ChevronRightIcon } from "lucide-react"
import { useNavigate } from "react-router-dom"
import { Badge } from "@/components/ui/Badge/Badge"
import { Card } from "@/components/ui/Card/Card"
import { iconFor } from "@/lib/icons"
import { cn } from "@/lib/utils"
import type { BucketItem, BucketSection, DashSeverity } from "@/types"

/**
 * Só warn e critical ganham selo. "ok" com selo verde em toda linha gasta a
 * varredura do operador, que existe para achar o que NÃO está em ordem.
 */
const SEVERITY_LABEL_KEY: Partial<Record<DashSeverity, string>> = {
  warn: "dashboardPage.kpi.severity.warn",
  critical: "dashboardPage.kpi.severity.critical",
}

interface BucketItemRowProps {
  item: BucketItem
}

const BucketItemRow: React.FC<BucketItemRowProps> = ({ item }) => {
  const { t } = useTranslation("dashboard")
  const navigate = useNavigate()
  const isClickable = Boolean(item.href)
  const severityKey = item.severity ? SEVERITY_LABEL_KEY[item.severity] : undefined

  const inner = (
    <div
      className={cn(
        "flex items-center justify-between gap-3 rounded-lg px-3 py-2.5 text-sm",
        isClickable
          ? "cursor-pointer transition-colors hover:bg-surface-hover"
          : "cursor-default",
      )}
    >
      <div className="min-w-0">
        <span className="block truncate font-medium text-text">{item.label}</span>
        {item.sub && (
          <span className="block truncate font-mono text-xs text-text-tertiary">{item.sub}</span>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {severityKey && (
          <Badge variant={item.severity === "critical" ? "danger" : "warning"} size="sm">
            {t(severityKey)}
          </Badge>
        )}
        <span className="font-mono text-sm font-medium text-text">{item.value}</span>
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
  const { t } = useTranslation("dashboard")
  const Icon = iconFor(section.icon_id)
  const emptyText = section.empty_hint ?? t("dashboardPage.buckets.empty")

  return (
    <Card padding="md">
      <div className="mb-3 flex items-center gap-2">
        <Icon size={15} className="text-text-tertiary" aria-hidden="true" />
        <h3 className="font-mono text-xs uppercase tracking-[0.1em] text-text-secondary">
          {section.label}
        </h3>
      </div>

      {section.items.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-text-tertiary">
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
