"use client"

import type React from "react"
import { useEffect, useMemo, useState } from "react"
import { Trans, useTranslation } from "react-i18next"
import {
  CheckCircle2Icon,
  AlertTriangleIcon,
  DatabaseIcon,
  NetworkIcon,
  PlayIcon,
  PlusIcon,
  TrashIcon,
} from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Notice } from "@/components/ui/Notice/Notice"
import { Select } from "@/components/ui/Select/Select"
import { listCollectorVendors } from "@/services/api"
import type {
  CollectorConfig,
  CollectorConfigTestResponse,
  CollectorRateLimits,
  CollectorVendor,
  UpdateCollectorConfigRequest,
} from "@/types"

interface Props {
  config: CollectorConfig | null
  loading?: boolean
  saving?: boolean
  testing?: boolean
  testResult?: CollectorConfigTestResponse | null
  feedback?: { type: "success" | "error"; message: string } | null
  onSave: (data: UpdateCollectorConfigRequest) => Promise<boolean>
  onTest: () => Promise<boolean>
}

interface FormValues {
  collector_jsonl_dir: string
  collector_batch_size: number
  collector_batch_flush_seconds: number
  dedupe_ttl_seconds: number
  domain_concurrency_limits: Record<string, number>
  rate_limits_by_vendor: Record<string, CollectorRateLimits>
}

/** Piso do TTL de dedupe: 4x o `visibility_timeout` do broker (3600s). Abaixo
 *  disso uma claim órfã expira ANTES de o broker desistir de redeliverar, e dois
 *  workers processam o mesmo evento como independentes. Espelha
 *  `state/dedupe.MIN_TTL_SECONDS` — travado por teste de invariante no backend. */
const DEDUPE_TTL_MIN_HOURS = 4
/** Teto: 31 dias. Acima disso o keyspace do Redis vira o problema. */
const DEDUPE_TTL_MAX_HOURS = 31 * 24

function clampTtlHours(hours: number): number {
  if (!Number.isFinite(hours)) return DEDUPE_TTL_MIN_HOURS
  return Math.max(DEDUPE_TTL_MIN_HOURS, Math.min(Math.round(hours), DEDUPE_TTL_MAX_HOURS))
}

/** Keyspace de dedupe em regime estacionário: `chaves ≈ EPS × TTL`. Não depende
 *  do volume acumulado, só da taxa e da janela — é a fórmula que faltava ao
 *  operador para escolher o TTL com consciência do custo em memória. */
export function estimateDedupeFootprint(eps: number, ttlSeconds: number) {
  const keys = Math.max(0, Math.round(eps * ttlSeconds))
  return { keys, bytes: keys * 115 }
}

function valuesFromConfig(config: CollectorConfig | null): FormValues {
  return {
    collector_jsonl_dir: config?.collector_jsonl_dir ?? "/var/log/centralops/collectors",
    collector_batch_size: config?.collector_batch_size ?? 200,
    collector_batch_flush_seconds: config?.collector_batch_flush_seconds ?? 5,
    // Canônico em SEGUNDOS; a UI edita em HORAS. Linha legada (só dias) é
    // convertida aqui para o operador ver o valor real que está em vigor.
    dedupe_ttl_seconds:
      config?.dedupe_ttl_seconds ?? (config?.dedupe_ttl_days ?? 1) * 86400,
    domain_concurrency_limits: { ...(config?.domain_concurrency_limits ?? {}) },
    rate_limits_by_vendor: deepCloneLimits(config?.rate_limits_by_vendor ?? {}),
  }
}

function deepCloneLimits(m: Record<string, CollectorRateLimits>): Record<string, CollectorRateLimits> {
  const out: Record<string, CollectorRateLimits> = {}
  for (const [k, v] of Object.entries(m)) out[k] = { ...v }
  return out
}

/** Dirty-check raso + profundo para os mapas (comparação estrutural). */
function isDirty(a: FormValues, b: FormValues): boolean {
  const keys: Array<keyof FormValues> = [
    "collector_jsonl_dir",
    "collector_batch_size",
    "collector_batch_flush_seconds",
    "dedupe_ttl_seconds",
  ]
  for (const k of keys) {
    if (a[k] !== b[k]) return true
  }
  if (JSON.stringify(a.domain_concurrency_limits) !== JSON.stringify(b.domain_concurrency_limits)) return true
  if (JSON.stringify(a.rate_limits_by_vendor) !== JSON.stringify(b.rate_limits_by_vendor)) return true
  return false
}

export const CollectorConfigForm: React.FC<Props> = ({
  config,
  loading = false,
  saving = false,
  testing = false,
  testResult,
  feedback,
  onSave,
  onTest,
}) => {
  const { t } = useTranslation("config")
  const initial = useMemo(() => valuesFromConfig(config), [config])
  const [values, setValues] = useState<FormValues>(initial)
  const [registeredVendors, setRegisteredVendors] = useState<CollectorVendor[]>([])

  useEffect(() => setValues(initial), [initial])

  // Carrega vendors registrados no backend uma única vez ao montar
  useEffect(() => {
    let cancelled = false
    listCollectorVendors()
      .then((vendors) => { if (!cancelled) setRegisteredVendors(vendors) })
      .catch(() => { /* falha silenciosa — editores degradam para input texto */ })
    return () => { cancelled = true }
  }, [])

  // Extrai plataformas únicas dos vendors registrados
  const availableVendorPlatforms = useMemo(
    () => Array.from(new Set(registeredVendors.map((v) => v.platform))).sort(),
    [registeredVendors],
  )

  const dirty = useMemo(() => isDirty(values, initial), [values, initial])

  const update = <K extends keyof FormValues>(key: K, v: FormValues[K]) =>
    setValues((prev) => ({ ...prev, [key]: v }))

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!dirty) return
    const payload: UpdateCollectorConfigRequest = {
      collector_jsonl_dir: values.collector_jsonl_dir.trim(),
      collector_batch_size: Number(values.collector_batch_size),
      collector_batch_flush_seconds: Number(values.collector_batch_flush_seconds),
      dedupe_ttl_seconds: Number(values.dedupe_ttl_seconds),
      domain_concurrency_limits: values.domain_concurrency_limits,
      rate_limits_by_vendor: values.rate_limits_by_vendor,
    }
    await onSave(payload)
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-8" aria-busy={loading}>
      {/* Banner de estado da config */}
      {config && !config.is_persisted && (
        <Notice variant="warning" title={t("collector.envBanner.title")}>
          <Trans i18nKey="collector.envBanner.body" t={t} values={{ envFile: ".env" }} components={{ code: <code /> }} />
        </Notice>
      )}

      <Notice variant="info" data-testid="destinations-cta">
        <Trans
          i18nKey="collector.destinationsCta"
          t={t}
          components={{ a: <a href="/destinations" className="underline hover:no-underline font-medium" /> }}
        />
      </Notice>

      {feedback && (
        <Notice variant={feedback.type === "success" ? "success" : "danger"}>
          {feedback.message}
        </Notice>
      )}

      {/* Resultados do teste */}
      {testResult && testResult.results.length > 0 && (
        <div className="space-y-2">
          {testResult.results.map((r) => (
            <Notice
              key={r.component}
              variant={r.status === "healthy" ? "success" : "danger"}
              title={`${r.component.toUpperCase()} · ${r.status === "healthy" ? t("collector.testResult.healthy") : t("collector.testResult.error")}`}
              icon={r.status === "healthy" ? <CheckCircle2Icon size={16} /> : <AlertTriangleIcon size={16} />}
            >
              <pre className="whitespace-pre-wrap text-xs">{JSON.stringify(r.details, null, 2)}</pre>
            </Notice>
          ))}
        </div>
      )}

      {/* ── Buffer / Dedupe ─────────────────────────────────────── */}
      <section className="space-y-4">
        <SectionHeader
          icon={<DatabaseIcon size={18} />}
          title={t("collector.sections.bufferDedupe.title")}
          hint={t("collector.sections.bufferDedupe.hint")}
        />

        <div className="grid gap-4 md:grid-cols-3">
          <Input
            label={t("collector.fields.batchSize")}
            type="number"
            min={1}
            max={10000}
            value={values.collector_batch_size}
            onChange={(e) => update("collector_batch_size", Number(e.target.value))}
            helperText={t("collector.fields.batchSizeHelper")}
          />
          <Input
            label={t("collector.fields.flushSeconds")}
            type="number"
            min={1}
            max={600}
            value={values.collector_batch_flush_seconds}
            onChange={(e) => update("collector_batch_flush_seconds", Number(e.target.value))}
            helperText={t("collector.fields.flushSecondsHelper")}
          />
          {/* TTL em HORAS: é a unidade em que o operador raciocina, e o piso
              real desta arquitetura são 4h (4x o visibility_timeout do broker).
              Em dias, 4h era inexpressável — o mínimo virava 1 dia, que a 6k
              ev/min significa ~8,6M chaves de dedupe no Redis. */}
          <Input
            label={t("collector.fields.dedupeTtl")}
            type="number"
            min={DEDUPE_TTL_MIN_HOURS}
            max={DEDUPE_TTL_MAX_HOURS}
            value={Math.round(values.dedupe_ttl_seconds / 3600)}
            onChange={(e) =>
              update("dedupe_ttl_seconds", clampTtlHours(Number(e.target.value)) * 3600)
            }
            helperText={t("collector.fields.dedupeTtlHelper", {
              min: DEDUPE_TTL_MIN_HOURS,
            })}
          />
        </div>
      </section>

      {/* ── Limites por domínio ─────────────────────────────────── */}
      <section className="space-y-4">
        <SectionHeader
          icon={<NetworkIcon size={18} />}
          title={t("collector.sections.domainConcurrency.title")}
          hint={t("collector.sections.domainConcurrency.hint")}
        />
        <DomainConcurrencyEditor
          value={values.domain_concurrency_limits}
          onChange={(v) => update("domain_concurrency_limits", v)}
          availableVendors={availableVendorPlatforms}
        />
      </section>

      {/* ── Rate limits por vendor ──────────────────────────────── */}
      <section className="space-y-4">
        <SectionHeader
          icon={<DatabaseIcon size={18} />}
          title={t("collector.sections.vendorRateLimits.title")}
          hint={t("collector.sections.vendorRateLimits.hint")}
        />
        <VendorLimitsEditor
          value={values.rate_limits_by_vendor}
          onChange={(v) => update("rate_limits_by_vendor", v)}
          availableVendors={availableVendorPlatforms}
        />
      </section>

      {/* ── Ações ───────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
        <div className="flex items-center gap-2 text-xs text-text-tertiary">
          {config?.config_version && (
            <Badge variant="outline" size="sm">
              v {config.config_version}
            </Badge>
          )}
          {dirty && (
            <Badge variant="warning" size="sm">
              {t("collector.unsavedChanges")}
            </Badge>
          )}
        </div>
        <div className="flex gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            leftIcon={<PlayIcon size={14} />}
            loading={testing}
            onClick={() => void onTest()}
            disabled={loading || dirty}
            title={dirty ? t("collector.testTooltipDirty") : t("collector.testTooltipClean")}
          >
            {t("collector.test")}
          </Button>
          <Button
            type="submit"
            size="sm"
            loading={saving}
            disabled={!dirty || loading}
          >
            {t("collector.saveConfig")}
          </Button>
        </div>
      </div>
    </form>
  )
}

// ── Subcomponentes ─────────────────────────────────────────────────

const SectionHeader: React.FC<{
  icon: React.ReactNode
  title: string
  hint: string
}> = ({ icon, title, hint }) => (
  <div className="flex items-start gap-3 border-b border-border pb-2">
    <span className="mt-0.5 text-primary-600">{icon}</span>
    <div>
      <h3 className="text-sm font-semibold text-text">{title}</h3>
      <p className="text-xs text-text-secondary">{hint}</p>
    </div>
  </div>
)

const DomainConcurrencyEditor: React.FC<{
  value: Record<string, number>
  onChange: (v: Record<string, number>) => void
  availableVendors?: string[]
}> = ({ value, onChange, availableVendors = [] }) => {
  const { t } = useTranslation("config")
  const [newVendor, setNewVendor] = useState("")
  const [useCustomInput, setUseCustomInput] = useState(false)
  const entries = Object.entries(value)

  // Vendors registrados que ainda não estão configurados
  const unconfiguredVendors = availableVendors.filter((v) => value[v] === undefined)
  // Vendors registrados que já estão configurados
  const registeredConfigured = availableVendors.filter((v) => value[v] !== undefined)

  const selectOptions = unconfiguredVendors.map((v) => ({ value: v, label: v }))
  const hasSelectOptions = selectOptions.length > 0

  const add = () => {
    const key = newVendor.trim()
    if (!key || value[key] !== undefined) return
    onChange({ ...value, [key]: 10 })
    setNewVendor("")
    setUseCustomInput(false)
  }
  const remove = (key: string) => {
    const next = { ...value }
    delete next[key]
    onChange(next)
  }
  const edit = (key: string, n: number) => onChange({ ...value, [key]: n })

  return (
    <div className="rounded-md border border-border bg-surface">
      <table className="w-full text-sm">
        <thead className="bg-surface-tertiary text-xs uppercase tracking-wider text-text-secondary">
          <tr>
            <th className="px-3 py-2 text-left">{t("collector.table.vendor")}</th>
            <th className="px-3 py-2 text-left">{t("collector.table.concurrentLimit")}</th>
            <th className="w-10" />
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {entries.length === 0 && (
            <tr>
              <td colSpan={3} className="px-3 py-4 text-center text-xs text-text-tertiary">
                {t("collector.table.noVendorsConfigured")}
              </td>
            </tr>
          )}
          {entries.map(([vendor, limit]) => (
            <tr key={vendor}>
              <td className="px-3 py-2 font-mono text-xs text-text">
                {vendor}
                {registeredConfigured.includes(vendor) && (
                  <Badge variant="outline" size="sm" className="ml-2">{t("collector.table.registeredBadge")}</Badge>
                )}
              </td>
              <td className="px-3 py-2">
                <input
                  type="number"
                  min={1}
                  max={1000}
                  value={limit}
                  onChange={(e) => edit(vendor, Number(e.target.value))}
                  className="h-8 w-28 rounded border border-border bg-surface px-2 text-sm"
                  aria-label={t("collector.table.concurrentLimitAriaLabel", { vendor })}
                />
              </td>
              <td className="px-3 py-2 text-right">
                <button
                  type="button"
                  onClick={() => remove(vendor)}
                  className="text-danger-500 hover:text-danger-700"
                  aria-label={t("collector.table.removeAriaLabel", { vendor })}
                >
                  <TrashIcon size={14} />
                </button>
              </td>
            </tr>
          ))}
          {/* Linha de adição */}
          <tr className="bg-surface-tertiary/40">
            <td className="px-3 py-2">
              {hasSelectOptions && !useCustomInput ? (
                <div className="flex items-center gap-2">
                  <Select
                    options={selectOptions}
                    value={newVendor}
                    placeholder={t("collector.table.selectVendorPlaceholder")}
                    onChange={(v) => setNewVendor(String(v))}
                    className="flex-1"
                    aria-label={t("collector.table.selectVendorAriaLabel")}
                  />
                  <button
                    type="button"
                    className="text-xs text-text-tertiary hover:text-text underline whitespace-nowrap"
                    onClick={() => { setNewVendor(""); setUseCustomInput(true) }}
                  >
                    {t("collector.table.other")}
                  </button>
                </div>
              ) : (
                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    value={newVendor}
                    onChange={(e) => setNewVendor(e.target.value)}
                    placeholder={t("collector.table.customVendorPlaceholder")}
                    className="h-8 flex-1 rounded border border-border bg-surface px-2 text-sm"
                    onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), add())}
                    aria-label={t("collector.table.customVendorAriaLabel")}
                  />
                  {hasSelectOptions && (
                    <button
                      type="button"
                      className="text-xs text-text-tertiary hover:text-text underline whitespace-nowrap"
                      onClick={() => { setNewVendor(""); setUseCustomInput(false) }}
                    >
                      {t("collector.table.list")}
                    </button>
                  )}
                </div>
              )}
            </td>
            <td colSpan={2} className="px-3 py-2 text-right">
              <Button
                type="button"
                variant="outline"
                size="xs"
                leftIcon={<PlusIcon size={12} />}
                onClick={add}
                disabled={!newVendor.trim()}
              >
                {t("collector.table.add")}
              </Button>
            </td>
          </tr>
        </tbody>
      </table>
      {/* Vendors disponíveis não configurados */}
      {unconfiguredVendors.length > 0 && (
        <div className="px-3 py-2 border-t border-border bg-surface-tertiary/40 flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-text-tertiary">{t("collector.table.availableDefault10")}</span>
          {unconfiguredVendors.map((v) => (
            <button
              key={v}
              type="button"
              className="inline-flex items-center gap-1 rounded border border-border bg-surface px-2 py-0.5 text-xs text-text-secondary hover:border-primary-400 hover:text-primary-700 transition-colors"
              onClick={() => { onChange({ ...value, [v]: 10 }) }}
              title={t("collector.table.configureDefaultTooltip", { vendor: v })}
            >
              {v}
              <PlusIcon size={10} aria-hidden="true" />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

const VendorLimitsEditor: React.FC<{
  value: Record<string, CollectorRateLimits>
  onChange: (v: Record<string, CollectorRateLimits>) => void
  availableVendors?: string[]
}> = ({ value, onChange, availableVendors = [] }) => {
  const { t } = useTranslation("config")
  const [newVendor, setNewVendor] = useState("")
  const [useCustomInput, setUseCustomInput] = useState(false)
  const entries = Object.entries(value)

  const unconfiguredVendors = availableVendors.filter((v) => value[v] === undefined)
  const registeredConfigured = availableVendors.filter((v) => value[v] !== undefined)
  const selectOptions = unconfiguredVendors.map((v) => ({ value: v, label: v }))
  const hasSelectOptions = selectOptions.length > 0

  const add = () => {
    const key = newVendor.trim()
    if (!key || value[key] !== undefined) return
    onChange({ ...value, [key]: { per_second: 10, per_minute: 100, per_hour: 1000 } })
    setNewVendor("")
    setUseCustomInput(false)
  }
  const remove = (key: string) => {
    const next = { ...value }
    delete next[key]
    onChange(next)
  }
  const edit = (key: string, field: keyof CollectorRateLimits, n: number) => {
    const current = value[key] ?? {}
    onChange({ ...value, [key]: { ...current, [field]: n } })
  }

  return (
    <div className="rounded-md border border-border bg-surface">
      <table className="w-full text-sm">
        <thead className="bg-surface-tertiary text-xs uppercase tracking-wider text-text-secondary">
          <tr>
            <th className="px-3 py-2 text-left">{t("collector.table.vendor")}</th>
            <th className="px-3 py-2 text-left">{t("collector.table.perSecond")}</th>
            <th className="px-3 py-2 text-left">{t("collector.table.perMinute")}</th>
            <th className="px-3 py-2 text-left">{t("collector.table.perHour")}</th>
            <th className="w-10" />
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {entries.length === 0 && (
            <tr>
              <td colSpan={5} className="px-3 py-4 text-center text-xs text-text-tertiary">
                {t("collector.table.noVendorsConfigured")}
              </td>
            </tr>
          )}
          {entries.map(([vendor, limits]) => (
            <tr key={vendor}>
              <td className="px-3 py-2 font-mono text-xs text-text">
                {vendor}
                {registeredConfigured.includes(vendor) && (
                  <Badge variant="outline" size="sm" className="ml-2">{t("collector.table.registeredBadge")}</Badge>
                )}
              </td>
              {(["per_second", "per_minute", "per_hour"] as const).map((field) => (
                <td key={field} className="px-3 py-2">
                  <input
                    type="number"
                    min={0}
                    max={100000}
                    value={limits[field] ?? 0}
                    onChange={(e) => edit(vendor, field, Number(e.target.value))}
                    className="h-8 w-24 rounded border border-border bg-surface px-2 text-sm"
                    aria-label={t("collector.table.perFieldAriaLabel", {
                      field: t(`collector.table.${field === "per_second" ? "perSecond" : field === "per_minute" ? "perMinute" : "perHour"}`),
                      vendor,
                    })}
                  />
                </td>
              ))}
              <td className="px-3 py-2 text-right">
                <button
                  type="button"
                  onClick={() => remove(vendor)}
                  className="text-danger-500 hover:text-danger-700"
                  aria-label={t("collector.table.removeAriaLabel", { vendor })}
                >
                  <TrashIcon size={14} />
                </button>
              </td>
            </tr>
          ))}
          {/* Linha de adição */}
          <tr className="bg-surface-tertiary/40">
            <td className="px-3 py-2" colSpan={4}>
              {hasSelectOptions && !useCustomInput ? (
                <div className="flex items-center gap-2">
                  <Select
                    options={selectOptions}
                    value={newVendor}
                    placeholder={t("collector.table.selectVendorPlaceholder")}
                    onChange={(v) => setNewVendor(String(v))}
                    className="flex-1"
                    aria-label={t("collector.table.selectVendorRateLimitAriaLabel")}
                  />
                  <button
                    type="button"
                    className="text-xs text-text-tertiary hover:text-text underline whitespace-nowrap"
                    onClick={() => { setNewVendor(""); setUseCustomInput(true) }}
                  >
                    {t("collector.table.other")}
                  </button>
                </div>
              ) : (
                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    value={newVendor}
                    onChange={(e) => setNewVendor(e.target.value)}
                    placeholder={t("collector.table.customVendorPlaceholder")}
                    className="h-8 flex-1 rounded border border-border bg-surface px-2 text-sm"
                    onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), add())}
                    aria-label={t("collector.table.customVendorAriaLabel")}
                  />
                  {hasSelectOptions && (
                    <button
                      type="button"
                      className="text-xs text-text-tertiary hover:text-text underline whitespace-nowrap"
                      onClick={() => { setNewVendor(""); setUseCustomInput(false) }}
                    >
                      {t("collector.table.list")}
                    </button>
                  )}
                </div>
              )}
            </td>
            <td className="px-3 py-2 text-right">
              <Button
                type="button"
                variant="outline"
                size="xs"
                leftIcon={<PlusIcon size={12} />}
                onClick={add}
                disabled={!newVendor.trim()}
              >
                {t("collector.table.add")}
              </Button>
            </td>
          </tr>
        </tbody>
      </table>
      {/* Vendors disponíveis não configurados */}
      {unconfiguredVendors.length > 0 && (
        <div className="px-3 py-2 border-t border-border bg-surface-tertiary/40 flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-text-tertiary">{t("collector.table.availableDefaultRates")}</span>
          {unconfiguredVendors.map((v) => (
            <button
              key={v}
              type="button"
              className="inline-flex items-center gap-1 rounded border border-border bg-surface px-2 py-0.5 text-xs text-text-secondary hover:border-primary-400 hover:text-primary-700 transition-colors"
              onClick={() => { onChange({ ...value, [v]: { per_second: 10, per_minute: 100, per_hour: 1000 } }) }}
              title={t("collector.table.configureDefaultRatesTooltip", { vendor: v })}
            >
              {v}
              <PlusIcon size={10} aria-hidden="true" />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export default CollectorConfigForm
