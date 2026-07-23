import type React from "react"
import { useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import * as api from "@/services/api"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Select } from "@/components/ui/Select/Select"
import { Notice } from "@/components/ui/Notice/Notice"
import { Checkbox } from "@/components/ui/Checkbox/Checkbox"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
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

  // ── Alavancas de redução de volume (ADR-0011) ────────────────────────
  // protect_detection é fail-safe: default TRUE (protege). Desligar é opt-out
  // consciente por-rota — nunca um checkbox neutro (ver handleProtectDetectionChange).
  const [protectDetection, setProtectDetection] = useState(route?.protect_detection ?? true)
  const [samplePercent, setSamplePercent] = useState(route?.sample_percent ?? 100)
  const [suppressKey, setSuppressKey] = useState(route?.suppress_key ?? "")
  const [suppressAllow, setSuppressAllow] = useState(route?.suppress_allow ?? 0)
  const [suppressWindowS, setSuppressWindowS] = useState(route?.suppress_window_s ?? 30)
  const [confirmUnprotectOpen, setConfirmUnprotectOpen] = useState(false)

  const handleProtectDetectionChange = (checked: boolean) => {
    if (checked) {
      // Re-ligar a proteção é sempre seguro — sem confirmação.
      setProtectDetection(true)
      return
    }
    // Desligar é a ação de risco (perda de detecção) — exige confirmação explícita.
    setConfirmUnprotectOpen(true)
  }

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
        protect_detection: protectDetection,
        sample_percent: samplePercent,
        suppress_key: suppressKey.trim() ? suppressKey.trim() : null,
        suppress_allow: suppressAllow,
        suppress_window_s: suppressWindowS,
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

      {action === "route" && (
        <fieldset className="space-y-3 rounded-lg border border-border p-4">
          <legend className="px-1 text-sm font-semibold text-text">
            {t("routeForm.reductionLegend")}{" "}
            <span className="font-normal text-text-secondary">{t("routeForm.reductionLegendOptional")}</span>
          </legend>

          {/* ADR-0015 inverteu os defaults: REDUCTION_SAMPLE_ENABLED,
              REDUCTION_SUPPRESS_ENABLED e REDUCTION_TRIM_ENABLED nascem ON
              (core/config.py:405,422,433) — só REDUCTION_AGGREGATE_ENABLED segue OFF.
              O portão que importa é o default POR-ROTA (sample_percent=100,
              suppress_allow=0), e não a flag global. O texto anterior afirmava o
              contrário e induzia o operador a achar que "Evitado" deveria ser zero.
              Aviso estático: nenhum endpoint expõe o estado das flags hoje. */}
          <Notice variant="info" title={t("routeForm.reductionFlagsNoticeTitle")}>
            {t("routeForm.reductionFlagsNoticeBody")}
          </Notice>

          <Checkbox
            label={t("routeForm.protectDetectionLabel")}
            description={t("routeForm.protectDetectionDescription")}
            checked={protectDetection}
            onChange={(e) => handleProtectDetectionChange(e.target.checked)}
            disabled={loading}
            data-testid="route-form-protect-detection"
          />

          {!protectDetection && (
            <Notice variant="warning" title={t("routeForm.unprotectedWarningTitle")}>
              {t("routeForm.unprotectedWarningBody")}
            </Notice>
          )}

          <div className="grid grid-cols-3 gap-3">
            <Input
              label={t("routeForm.samplePercentLabel")}
              type="number"
              min={0}
              max={100}
              value={String(samplePercent)}
              onChange={(e) => setSamplePercent(Math.max(0, Math.min(100, Number(e.target.value) || 0)))}
              helperText={protectDetection ? t("routeForm.samplePercentHelperProtected") : t("routeForm.samplePercentHelper")}
              disabled={loading || protectDetection}
              data-testid="route-form-sample-percent"
            />
            <Input
              label={t("routeForm.suppressAllowLabel")}
              type="number"
              min={0}
              value={String(suppressAllow)}
              onChange={(e) => setSuppressAllow(Math.max(0, Number(e.target.value) || 0))}
              helperText={t("routeForm.suppressAllowHelper")}
              disabled={loading || protectDetection}
              data-testid="route-form-suppress-allow"
            />
            <Input
              label={t("routeForm.suppressWindowLabel")}
              type="number"
              min={1}
              value={String(suppressWindowS)}
              onChange={(e) => setSuppressWindowS(Math.max(1, Number(e.target.value) || 1))}
              helperText={t("routeForm.suppressWindowHelper")}
              disabled={loading || protectDetection}
              data-testid="route-form-suppress-window"
            />
          </div>
          <Input
            label={t("routeForm.suppressKeyLabel")}
            value={suppressKey}
            onChange={(e) => setSuppressKey(e.target.value)}
            placeholder={t("routeForm.suppressKeyPlaceholder")}
            helperText={t("routeForm.suppressKeyHelper")}
            disabled={loading || protectDetection}
            data-testid="route-form-suppress-key"
          />
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

      {/* protect_detection é fail-safe (default true) — desligar exige confirmação
          explícita do risco (perda de detecção), nunca um checkbox neutro. */}
      <ConfirmDialog
        open={confirmUnprotectOpen}
        title={t("routeForm.unprotectDialogTitle")}
        description={t("routeForm.unprotectDialogDescription")}
        confirmLabel={t("routeForm.unprotectDialogConfirm")}
        cancelLabel={t("common:actions.cancel")}
        confirmVariant="danger"
        onConfirm={() => {
          setProtectDetection(false)
          setConfirmUnprotectOpen(false)
        }}
        onClose={() => setConfirmUnprotectOpen(false)}
        data-testid="route-form-unprotect-dialog"
      />
    </form>
  )
}
