import type React from "react"
import { useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import * as api from "@/services/api"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Select } from "@/components/ui/Select/Select"
import { Notice } from "@/components/ui/Notice/Notice"
import {
  RouteConditionEditor,
  clausesToCondition,
  conditionToClauses,
} from "./RouteConditionEditor"
import { PiiRuleEditor } from "./PiiRuleEditor"
import { TileGallery, type Tile } from "@/components/shared/TileGallery"
import { kindToIcon } from "@/components/destinations/DestinationTypeGallery"
import type { Destination, PiiRedactionRule, Route, RouteAction, RouteCreateRequest } from "@/types"

interface RouteFormProps {
  mode: "create" | "edit"
  route?: Route | null
  loading?: boolean
  onCancel: () => void
  onSubmit: (payload: RouteCreateRequest) => Promise<void>
}

/** Normaliza PiiRedaction (spec ou array) para PiiRedactionRule[]. */
function normalizePii(raw: Route["pii_redaction"]): PiiRedactionRule[] {
  if (!raw) return []
  if (Array.isArray(raw)) return raw as PiiRedactionRule[]
  return (raw as { rules?: PiiRedactionRule[] }).rules ?? []
}

export const RouteForm: React.FC<RouteFormProps> = ({ mode, route, loading, onCancel, onSubmit }) => {
  const { t } = useTranslation("routing")
  const [name, setName] = useState(route?.name ?? "")
  const [priority, setPriority] = useState(route?.priority ?? 100)
  const [action, setAction] = useState<RouteAction>(route?.action ?? "route")
  const [destinationIds, setDestinationIds] = useState<string[]>(route?.destination_ids ?? [])
  const [isFinal, setIsFinal] = useState(route?.is_final ?? true)
  const [enabled, setEnabled] = useState(route?.enabled ?? true)
  const [canary, setCanary] = useState(route?.canary_percent ?? 100)
  const [clauses, setClauses] = useState(() => conditionToClauses(route?.condition ?? {}))
  const [piiRules, setPiiRules] = useState<PiiRedactionRule[]>(() => normalizePii(route?.pii_redaction ?? null))
  const [destinations, setDestinations] = useState<Destination[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.listDestinations({ include_disabled: true, limit: 200 }).then(setDestinations).catch(() => setDestinations([]))
  }, [])

  // Tiles do grid de destinos (multi-seleção). wazuh-default é o catch-all
  // (lane dedicada); os demais vêm do catálogo, com ícone por kind.
  const destTiles = useMemo<Tile[]>(
    () => [
      {
        id: "wazuh-default",
        label: t("routeForm.wazuhDefaultLabel"),
        description: t("routeForm.wazuhDefaultDescription"),
        icon: kindToIcon("syslog"),
        badge: t("routeForm.catchAllBadge"),
        badgeTone: "primary",
      },
      ...destinations.map<Tile>((d) => ({
        id: d.id,
        label: d.name,
        description: d.kind,
        icon: kindToIcon(d.kind),
        badge: d.enabled === false ? t("routeForm.destinationDisabledBadge") : undefined,
        badgeTone: "warning",
      })),
    ],
    [destinations, t],
  )

  const toggleDest = (id: string) =>
    setDestinationIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    if (!name.trim()) return setError(t("routeForm.nameRequired"))
    if (action === "route" && destinationIds.length === 0) return setError(t("routeForm.destinationRequired"))
    // Valida regras PII: path obrigatório
    const invalidPii = piiRules.find((r) => !r.path.trim())
    if (invalidPii) return setError(t("routeForm.piiPathRequired"))
    const piiRedaction: RouteCreateRequest["pii_redaction"] = (action === "route" && piiRules.length > 0) ? piiRules : null
    try {
      await onSubmit({
        name: name.trim(),
        priority,
        action,
        condition: clausesToCondition(clauses),
        destination_ids: action === "drop" ? [] : destinationIds,
        is_final: isFinal,
        enabled,
        canary_percent: canary,
        pii_redaction: piiRedaction,
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : t("routeForm.saveError"))
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-5">
      {error && <Notice variant="danger" title={t("routeForm.cannotSaveTitle")}>{error}</Notice>}

      <div className="grid grid-cols-3 gap-3">
        <Input label={t("routeForm.nameLabel")} value={name} onChange={(e) => setName(e.target.value)} required disabled={loading} />
        <Input
          label={t("routeForm.priorityLabel")}
          type="number"
          value={String(priority)}
          onChange={(e) => setPriority(Number(e.target.value) || 0)}
          helperText={t("routeForm.priorityHelper")}
          disabled={loading}
        />
        <Input
          label={t("routeForm.canaryLabel")}
          type="number"
          min={0}
          max={100}
          value={String(canary)}
          onChange={(e) => setCanary(Math.max(0, Math.min(100, Number(e.target.value) || 0)))}
          helperText={t("routeForm.canaryHelper")}
          disabled={loading}
        />
      </div>

      <Select
        label={t("routeForm.actionLabel")}
        value={action}
        options={[{ value: "route", label: t("routeForm.actionRouteOption") }, { value: "drop", label: t("routeForm.actionDropOption") }]}
        disabled={loading}
        onValueChange={(v) => setAction(v as RouteAction)}
      />

      <fieldset className="space-y-3 rounded-lg border border-border p-4">
        <legend className="px-1 text-sm font-semibold text-text">{t("routeForm.conditionLegend")}</legend>
        <RouteConditionEditor clauses={clauses} onChange={setClauses} disabled={loading} />
      </fieldset>

      {action === "route" && (
        <fieldset className="space-y-3 rounded-lg border border-border p-4">
          <legend className="px-1 text-sm font-semibold text-text">
            {t("routeForm.destinationsLegend")}{" "}
            <span className="font-normal text-text-secondary">
              {t("routeForm.destinationsSelectedCount", { count: destinationIds.length })}
            </span>
          </legend>
          <TileGallery
            tiles={destTiles}
            value={destinationIds}
            onChange={toggleDest}
            multiple
            disabled={loading}
            searchPlaceholder={t("routeForm.destinationsSearchPlaceholder")}
            ariaLabel={t("routeForm.destinationsAriaLabel")}
            emptyLabel={t("routeForm.destinationsEmptyLabel")}
            columns={3}
          />
        </fieldset>
      )}

      {action === "route" && (
        <fieldset className="space-y-2 rounded-lg border border-border p-4">
          <legend className="px-1 text-sm font-semibold text-text">
            {t("routeForm.piiLegend")}{" "}
            <span className="font-normal text-text-secondary">{t("routeForm.piiLegendOptional")}</span>
          </legend>
          <p className="text-xs text-text-secondary">
            {t("routeForm.piiDescriptionPrefix")}<code className="font-mono">raw.*</code>{t("routeForm.piiDescriptionOr")}
            <code className="font-mono">normalized.*</code>{t("routeForm.piiDescriptionMiddle")}<code className="font-mono">PII_REDACTION_ENABLED</code>{t("routeForm.piiDescriptionSuffix")}
          </p>
          <PiiRuleEditor rules={piiRules} onChange={setPiiRules} disabled={loading} />
        </fieldset>
      )}

      <div className="flex flex-wrap gap-5">
        <label className="flex items-center gap-2 text-sm text-text">
          <input type="checkbox" className="h-4 w-4 rounded border-border" checked={isFinal} onChange={(e) => setIsFinal(e.target.checked)} disabled={loading} />
          <span>{t("routeForm.finalCheckboxLabel")}</span>
        </label>
        <label className="flex items-center gap-2 text-sm text-text">
          <input type="checkbox" className="h-4 w-4 rounded border-border" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} disabled={loading} />
          <span>{t("routeForm.enabledCheckboxLabel")}</span>
        </label>
      </div>

      <div className="flex justify-end gap-3 pt-2">
        <Button type="button" variant="outline" onClick={onCancel} disabled={loading}>{t("common:actions.cancel")}</Button>
        <Button type="submit" loading={loading}>{mode === "create" ? t("routeForm.createSubmit") : t("routeForm.editSubmit")}</Button>
      </div>
    </form>
  )
}
