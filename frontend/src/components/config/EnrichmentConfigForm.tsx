"use client"

import type React from "react"
import { useCallback, useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  DatabaseIcon,
  GlobeIcon,
  PlugZapIcon,
  ShieldAlertIcon,
  SparklesIcon,
  TableIcon,
} from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Notice } from "@/components/ui/Notice/Notice"
import { SkeletonCard } from "@/components/ui/Skeleton"
import { useFirstInvalidFocus } from "@/hooks/useFirstInvalidFocus"
import * as api from "@/services/api"
import type {
  EnrichmentConfig,
  EnrichmentConfigUpdateRequest,
  EnrichmentRedisTestResult,
} from "@/services/api"

/**
 * Configuração de INFRAESTRUTURA do enriquecimento — a aba que tira o cache L2
 * do `.env`.
 *
 * O problema que esta tela resolve não é ergonomia: `ENRICH_REDIS_URL` vazia
 * desliga os enrichers por lote (VirusTotal, AbuseIPDB, OTX, GreyNoise) por
 * decisão de projeto, e a variável vivia num arquivo que o operador do console
 * não edita. Cadastrar a fonte, publicar a política e habilitá-la produzia
 * silêncio — o diagnóstico só aparecia na aba de Execução, depois do primeiro
 * ciclo de coleta.
 *
 * Duas decisões de interface que vêm do domínio, não de estilo:
 *
 * 1. **O estado do cache L2 é a primeira coisa da tela, com o nome dos
 *    enrichers parados.** "Redis não configurado" é jargão; "VirusTotal,
 *    AbuseIPDB e OTX não rodam" é a consequência, e é ela que decide se o
 *    operador age agora.
 * 2. **Testar vem antes de salvar.** A sonda aceita a senha em rascunho, então
 *    o erro do provedor (auth, DNS, recusa de conexão) aparece com o formulário
 *    ainda aberto, em vez de virar uma linha de falha um ciclo depois.
 */

interface Props {
  /** Recarrega o cabeçalho da página quando algo muda. */
  onSaved?: (config: EnrichmentConfig) => void
}

/** Campos numéricos ficam como STRING enquanto editados.
 *
 * Guardar `number` obriga a converter a cada tecla, e apagar o campo para
 * digitar outro valor produzia `NaN` — que ou vira 0 (silenciosamente gravando
 * um orçamento zerado) ou trava o formulário. Com string, o vazio é vazio, e a
 * conversão acontece uma vez, no envio.
 */
type Draft = {
  redis_host: string
  redis_port: string
  redis_db: string
  redis_use_tls: boolean
  /** Vazio = manter o segredo atual. Ver o contrato de três estados na API. */
  redis_password: string
  remote_batch_budget_ms: string
  cycle_budget_ms: string
  l1_max_entries: string
  singleflight_wait_ms: string
  breaker_failure_threshold: string
  breaker_window_s: string
  breaker_cooldown_s: string
  breaker_max_cooldown_s: string
  max_table_bytes_mib: string
  lru_bytes_mib: string
}

const MIB = 1024 * 1024

/**
 * R4-8.4: limites REAIS do backend — `_LIMITS` em
 * `backend/app/routers/enrichment_config.py` (`_clamp_or_reject`, PUT
 * rejeita com 422 fora da faixa). Não inventados: copiados de lá. Os dois
 * últimos são em BYTES no backend; a UI edita em MiB, e a comparação
 * converte antes.
 */
const NUMERIC_LIMITS = {
  redis_port: [1, 65_535],
  redis_db: [0, 15],
  remote_batch_budget_ms: [10, 60_000],
  cycle_budget_ms: [100, 600_000],
  l1_max_entries: [100, 1_000_000],
  singleflight_wait_ms: [0, 5_000],
  breaker_failure_threshold: [1, 100],
  breaker_window_s: [10, 86_400],
  breaker_cooldown_s: [5, 86_400],
  breaker_max_cooldown_s: [5, 604_800],
  max_table_bytes_mib: [64 * 1024, 1024 * 1024 * 1024],
  lru_bytes_mib: [64 * 1024, 4 * 1024 * 1024 * 1024],
} as const satisfies Record<string, readonly [number, number]>

type NumericField = keyof typeof NUMERIC_LIMITS

const BYTE_FIELDS = new Set<NumericField>(["max_table_bytes_mib", "lru_bytes_mib"])

/** Ordem de checagem = ordem visual do form (decide qual campo foca 1º). */
const NUMERIC_FIELD_ORDER: NumericField[] = [
  "redis_port",
  "redis_db",
  "remote_batch_budget_ms",
  "cycle_budget_ms",
  "l1_max_entries",
  "singleflight_wait_ms",
  "breaker_failure_threshold",
  "breaker_window_s",
  "breaker_cooldown_s",
  "breaker_max_cooldown_s",
  "max_table_bytes_mib",
  "lru_bytes_mib",
]

/** `null` = válido; caso contrário, mensagem de erro pronta pra exibir. */
function validateNumericField(t: (key: string, opts?: Record<string, unknown>) => string, field: NumericField, rawValue: string): string | null {
  const num = Number(rawValue)
  const [low, high] = NUMERIC_LIMITS[field]
  const isByteField = BYTE_FIELDS.has(field)
  const compareValue = isByteField ? num * MIB : num
  if (!Number.isFinite(compareValue) || compareValue < low || compareValue > high) {
    // Campo em MiB: mostra a faixa em MiB (arredondada pra cima no mínimo,
    // pra baixo no máximo — nunca afirma uma faixa mais larga que a real).
    const displayLow = isByteField ? Math.ceil(low / MIB) : low
    const displayHigh = isByteField ? Math.floor(high / MIB) : high
    return t("page.enrichment.validation.outOfRange", { low: displayLow, high: displayHigh })
  }
  return null
}

function draftFrom(cfg: EnrichmentConfig): Draft {
  return {
    redis_host: cfg.redis_host ?? "",
    redis_port: String(cfg.redis_port),
    redis_db: String(cfg.redis_db),
    redis_use_tls: cfg.redis_use_tls,
    redis_password: "",
    remote_batch_budget_ms: String(cfg.remote_batch_budget_ms),
    cycle_budget_ms: String(cfg.cycle_budget_ms),
    l1_max_entries: String(cfg.l1_max_entries),
    singleflight_wait_ms: String(cfg.singleflight_wait_ms),
    breaker_failure_threshold: String(cfg.breaker_failure_threshold),
    breaker_window_s: String(cfg.breaker_window_s),
    breaker_cooldown_s: String(cfg.breaker_cooldown_s),
    breaker_max_cooldown_s: String(cfg.breaker_max_cooldown_s),
    max_table_bytes_mib: String(Math.round(cfg.max_table_bytes / MIB)),
    lru_bytes_mib: String(Math.round(cfg.lru_bytes / MIB)),
  }
}

function fmtBytes(n: number): string {
  if (n >= MIB) return `${(n / MIB).toFixed(0)} MiB`
  if (n >= 1024) return `${(n / 1024).toFixed(0)} KiB`
  return `${n} B`
}

export const EnrichmentConfigForm: React.FC<Props> = ({ onSaved }) => {
  const { t } = useTranslation("config")
  const [config, setConfig] = useState<EnrichmentConfig | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [feedback, setFeedback] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<EnrichmentRedisTestResult | null>(null)
  const errorId = "enrichment-config-error"
  // R4-8.4: `error` cobre TANTO falha de API (load/toggle/save, sem campo
  // específico — `failGeneral`) QUANTO validação de campo (`failField`),
  // mesmo padrão já usado em outros forms da posse.
  const { error, errorField, registerField, failField, failGeneral, clearError } =
    useFirstInvalidFocus<NumericField>()

  const load = useCallback(async () => {
    setLoading(true)
    clearError()
    try {
      const cfg = await api.getEnrichmentConfig()
      setConfig(cfg)
      setDraft(draftFrom(cfg))
    } catch (err) {
      failGeneral(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  function set<K extends keyof Draft>(key: K, value: Draft[K]) {
    setDraft((prev) => (prev ? { ...prev, [key]: value } : prev))
    // Uma edição invalida o resultado da sonda: o "conectado" na tela passaria
    // a se referir a um endereço que não é mais o que está no formulário.
    setTestResult(null)
    setFeedback(null)
  }

  async function handleToggleEnabled() {
    if (!config) return
    setSaving(true)
    clearError()
    try {
      const next = await api.updateEnrichmentConfig({ enabled: !config.enabled })
      setConfig(next)
      setDraft(draftFrom(next))
      onSaved?.(next)
    } catch (err) {
      failGeneral(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  async function handleTest() {
    if (!draft) return
    setTesting(true)
    setTestResult(null)
    try {
      setTestResult(
        await api.testEnrichmentRedis({
          redis_host: draft.redis_host.trim(),
          redis_port: Number(draft.redis_port) || 6379,
          redis_db: Number(draft.redis_db) || 0,
          redis_use_tls: draft.redis_use_tls,
          // Só manda a senha se foi digitada agora. Vazio ⇒ o servidor usa a
          // gravada, e o operador testa sem redigitar.
          ...(draft.redis_password ? { redis_password: draft.redis_password } : {}),
        }),
      )
    } catch (err) {
      setTestResult({
        ok: false,
        message: err instanceof Error ? err.message : String(err),
        warnings: [],
      })
    } finally {
      setTesting(false)
    }
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault()
    if (!draft) return

    // R4-8.4: valida os 12 campos numéricos contra os limites REAIS do
    // backend (`NUMERIC_LIMITS`, copiados de `_LIMITS` em
    // `enrichment_config.py`) antes de gastar uma volta ao servidor. Foca o
    // 1º inválido, na ordem visual do form.
    for (const field of NUMERIC_FIELD_ORDER) {
      const message = validateNumericField(t, field, draft[field])
      if (message) {
        failField(field, message)
        return
      }
    }

    setSaving(true)
    clearError()
    setFeedback(null)
    try {
      const payload: EnrichmentConfigUpdateRequest = {
        redis_host: draft.redis_host.trim() || null,
        redis_port: Number(draft.redis_port) || 6379,
        redis_db: Number(draft.redis_db) || 0,
        redis_use_tls: draft.redis_use_tls,
        remote_batch_budget_ms: Number(draft.remote_batch_budget_ms),
        cycle_budget_ms: Number(draft.cycle_budget_ms),
        l1_max_entries: Number(draft.l1_max_entries),
        singleflight_wait_ms: Number(draft.singleflight_wait_ms),
        breaker_failure_threshold: Number(draft.breaker_failure_threshold),
        breaker_window_s: Number(draft.breaker_window_s),
        breaker_cooldown_s: Number(draft.breaker_cooldown_s),
        breaker_max_cooldown_s: Number(draft.breaker_max_cooldown_s),
        max_table_bytes: Number(draft.max_table_bytes_mib) * MIB,
        lru_bytes: Number(draft.lru_bytes_mib) * MIB,
      }
      // Só inclui a senha quando foi digitada: mandar "" REMOVERIA a gravada,
      // e o campo vem vazio em toda abertura da tela.
      if (draft.redis_password) payload.redis_password = draft.redis_password

      const next = await api.updateEnrichmentConfig(payload)
      setConfig(next)
      setDraft(draftFrom(next))
      setFeedback(t("page.enrichment.saved", { seconds: next.propagation_worst_case_s }))
      onSaved?.(next)
    } catch (err) {
      failGeneral(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <SkeletonCard />
  if (!config || !draft) {
    return (
      <Notice variant="danger" title={t("page.enrichment.loadError")}>
        {error}
      </Notice>
    )
  }

  const remoteNames = config.remote_enrichers.join(", ")
  const forksHint = t("page.enrichment.tables.forkHint", {
    per: draft.max_table_bytes_mib || "0",
    total: (Number(draft.max_table_bytes_mib) || 0) * 8,
  })

  return (
    <form onSubmit={handleSave} className="space-y-6" noValidate>
      {/* R2-8.3/R2-8.2: reação direta ao clique em "Salvar"/"Habilitar" —
          mantém assertive explícito (o padrão do Notice virou polite).
          R4-8.4: agora também cobre erro POR CAMPO (`failField`). */}
      {error && <Notice id={errorId} variant="danger" title={error} live="assertive" />}
      {feedback && <Notice variant="success" title={feedback} />}

      {/* ── Subsistema ──────────────────────────────────────────────── */}
      <section className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-4">
        <div className="flex items-start gap-3">
          <SparklesIcon size={18} className="mt-0.5 text-text-tertiary" aria-hidden />
          <div>
            <p className="text-sm font-medium">{t("page.enrichment.subsystem.title")}</p>
            <p className="text-xs text-text-tertiary">
              {config.enabled
                ? t("page.enrichment.subsystem.onHint")
                : t("page.enrichment.subsystem.offHint")}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <Badge variant={config.enabled ? "success" : "default"}>
            {config.enabled
              ? t("page.enrichment.subsystem.on")
              : t("page.enrichment.subsystem.off")}
          </Badge>
          <Button
            type="button"
            variant={config.enabled ? "outline" : "primary"}
            loading={saving}
            onClick={handleToggleEnabled}
          >
            {config.enabled
              ? t("page.enrichment.subsystem.disable")
              : t("page.enrichment.subsystem.enable")}
          </Button>
        </div>
      </section>

      {/* ── Cache L2 ────────────────────────────────────────────────── */}
      <section className="space-y-4 rounded-lg border border-border p-4">
        <div className="flex items-start gap-3">
          <DatabaseIcon size={18} className="mt-0.5 text-text-tertiary" aria-hidden />
          <div>
            <h3 className="text-sm font-semibold">{t("page.enrichment.cache.title")}</h3>
            <p className="text-xs text-text-tertiary">{t("page.enrichment.cache.description")}</p>
          </div>
        </div>

        {/* O estado vem PRIMEIRO e nomeia os enrichers parados: "não
            configurado" é jargão, "VirusTotal não roda" é a consequência. */}
        {config.redis_configured ? (
          <Notice variant="success" title={t("page.enrichment.cache.configured")}>
            {config.redis_url_masked}
          </Notice>
        ) : (
          <Notice variant="warning" title={t("page.enrichment.cache.missingTitle")}>
            {remoteNames
              ? t("page.enrichment.cache.missingBody", { names: remoteNames })
              : t("page.enrichment.cache.missingBodyNoNames")}
          </Notice>
        )}

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Input
            label={t("page.enrichment.cache.host")}
            value={draft.redis_host}
            onChange={(e) => set("redis_host", e.target.value)}
            placeholder="redis-enrich"
            className="font-mono text-sm"
          />
          <Input
            ref={registerField("redis_port")}
            label={t("page.enrichment.cache.port")}
            value={draft.redis_port}
            onChange={(e) => set("redis_port", e.target.value)}
            inputMode="numeric"
            className="font-mono text-sm"
            error={errorField === "redis_port" ? error ?? undefined : undefined}
          />
          <Input
            ref={registerField("redis_db")}
            label={t("page.enrichment.cache.db")}
            value={draft.redis_db}
            onChange={(e) => set("redis_db", e.target.value)}
            inputMode="numeric"
            className="font-mono text-sm"
            error={errorField === "redis_db" ? error ?? undefined : undefined}
          />
          <Input
            label={t("page.enrichment.cache.password")}
            type="password"
            value={draft.redis_password}
            onChange={(e) => set("redis_password", e.target.value)}
            // SEC-02/SEC-10: `autocomplete="off"` em campo de senha é
            // largamente IGNORADO pelos navegadores (continuam oferecendo
            // autofill/gerenciador). `new-password` é o valor do próprio
            // spec pra "não é login" — e o `Input` já teria isso por
            // padrão; explícito aqui só documenta a intenção.
            autoComplete="new-password"
            placeholder={
              config.redis_secret_configured
                ? t("page.enrichment.cache.passwordKeep")
                : undefined
            }
            helperText={t("page.enrichment.cache.passwordHint")}
          />
        </div>

        <label className="flex w-fit items-center gap-2 text-sm text-text-secondary">
          <input
            type="checkbox"
            checked={draft.redis_use_tls}
            onChange={(e) => set("redis_use_tls", e.target.checked)}
          />
          {t("page.enrichment.cache.tls")}
        </label>

        <div className="flex flex-wrap items-center gap-3">
          <Button
            type="button"
            variant="outline"
            loading={testing}
            disabled={!draft.redis_host.trim()}
            onClick={handleTest}
            leftIcon={<PlugZapIcon size={14} />}
          >
            {t("page.enrichment.cache.test")}
          </Button>
          <p className="text-xs text-text-tertiary">{t("page.enrichment.cache.testHint")}</p>
        </div>

        {testResult && (
          <Notice
            variant={testResult.ok ? "success" : "danger"}
            title={testResult.message}
            live={testResult.ok ? undefined : "assertive"}
          >
            <div className="flex flex-wrap gap-2 pt-1">
              {testResult.latency_ms != null && (
                <Badge variant="default">{testResult.latency_ms.toFixed(1)} ms</Badge>
              )}
              {testResult.maxmemory_policy && (
                <Badge
                  variant={
                    testResult.maxmemory_policy.startsWith("allkeys")
                      ? "success"
                      : "warning"
                  }
                >
                  {testResult.maxmemory_policy}
                </Badge>
              )}
              {testResult.maxmemory_bytes != null && testResult.maxmemory_bytes > 0 && (
                <Badge variant="default">{fmtBytes(testResult.maxmemory_bytes)}</Badge>
              )}
              {/* Três estados, e a diferença importa: confirmado distinto,
                  confirmado IGUAL, e "não deu para confirmar". */}
              {testResult.distinct_from_main === true && (
                <Badge variant="success">
                  {t("page.enrichment.cache.distinctOk")}
                </Badge>
              )}
              {testResult.distinct_from_main === false && (
                <Badge variant="danger">
                  {t("page.enrichment.cache.distinctBad")}
                </Badge>
              )}
            </div>
            {testResult.warnings.length > 0 && (
              <ul className="mt-2 list-disc space-y-1 pl-4 text-xs">
                {testResult.warnings.map((w, i) => (
                  <li key={`w-${i}`}>{w}</li>
                ))}
              </ul>
            )}
          </Notice>
        )}

        {!config.redis_configured && (
          <p className="text-xs text-text-tertiary">{t("page.enrichment.cache.composeHint")}</p>
        )}
      </section>

      {/* ── Orçamentos ──────────────────────────────────────────────── */}
      <section className="space-y-3 rounded-lg border border-border p-4">
        <div>
          <h3 className="text-sm font-semibold">{t("page.enrichment.budgets.title")}</h3>
          <p className="text-xs text-text-tertiary">{t("page.enrichment.budgets.description")}</p>
        </div>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Input
            ref={registerField("remote_batch_budget_ms")}
            label={t("page.enrichment.budgets.batch")}
            value={draft.remote_batch_budget_ms}
            onChange={(e) => set("remote_batch_budget_ms", e.target.value)}
            inputMode="numeric"
            helperText={t("page.enrichment.default", { value: "300 ms" })}
            error={errorField === "remote_batch_budget_ms" ? error ?? undefined : undefined}
          />
          <Input
            ref={registerField("cycle_budget_ms")}
            label={t("page.enrichment.budgets.cycle")}
            value={draft.cycle_budget_ms}
            onChange={(e) => set("cycle_budget_ms", e.target.value)}
            inputMode="numeric"
            helperText={t("page.enrichment.default", { value: "30000 ms" })}
            error={errorField === "cycle_budget_ms" ? error ?? undefined : undefined}
          />
          <Input
            ref={registerField("l1_max_entries")}
            label={t("page.enrichment.budgets.l1")}
            value={draft.l1_max_entries}
            onChange={(e) => set("l1_max_entries", e.target.value)}
            inputMode="numeric"
            helperText={t("page.enrichment.default", { value: "10000" })}
            error={errorField === "l1_max_entries" ? error ?? undefined : undefined}
          />
          <Input
            ref={registerField("singleflight_wait_ms")}
            label={t("page.enrichment.budgets.singleflight")}
            value={draft.singleflight_wait_ms}
            onChange={(e) => set("singleflight_wait_ms", e.target.value)}
            inputMode="numeric"
            helperText={t("page.enrichment.default", { value: "50 ms" })}
            error={errorField === "singleflight_wait_ms" ? error ?? undefined : undefined}
          />
        </div>
      </section>

      {/* ── Breaker ─────────────────────────────────────────────────── */}
      <section className="space-y-3 rounded-lg border border-border p-4">
        <div className="flex items-start gap-3">
          <ShieldAlertIcon size={18} className="mt-0.5 text-text-tertiary" aria-hidden />
          <div>
            <h3 className="text-sm font-semibold">{t("page.enrichment.breaker.title")}</h3>
            <p className="text-xs text-text-tertiary">
              {t("page.enrichment.breaker.description")}
            </p>
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Input
            ref={registerField("breaker_failure_threshold")}
            label={t("page.enrichment.breaker.threshold")}
            value={draft.breaker_failure_threshold}
            onChange={(e) => set("breaker_failure_threshold", e.target.value)}
            inputMode="numeric"
            helperText={t("page.enrichment.default", { value: "3" })}
            error={errorField === "breaker_failure_threshold" ? error ?? undefined : undefined}
          />
          <Input
            ref={registerField("breaker_window_s")}
            label={t("page.enrichment.breaker.window")}
            value={draft.breaker_window_s}
            onChange={(e) => set("breaker_window_s", e.target.value)}
            inputMode="numeric"
            helperText={t("page.enrichment.default", { value: "600 s" })}
            error={errorField === "breaker_window_s" ? error ?? undefined : undefined}
          />
          <Input
            ref={registerField("breaker_cooldown_s")}
            label={t("page.enrichment.breaker.cooldown")}
            value={draft.breaker_cooldown_s}
            onChange={(e) => set("breaker_cooldown_s", e.target.value)}
            inputMode="numeric"
            helperText={t("page.enrichment.default", { value: "120 s" })}
            error={errorField === "breaker_cooldown_s" ? error ?? undefined : undefined}
          />
          <Input
            ref={registerField("breaker_max_cooldown_s")}
            label={t("page.enrichment.breaker.maxCooldown")}
            value={draft.breaker_max_cooldown_s}
            onChange={(e) => set("breaker_max_cooldown_s", e.target.value)}
            inputMode="numeric"
            helperText={t("page.enrichment.default", { value: "1920 s" })}
            error={errorField === "breaker_max_cooldown_s" ? error ?? undefined : undefined}
          />
        </div>
      </section>

      {/* ── Tabelas ─────────────────────────────────────────────────── */}
      <section className="space-y-3 rounded-lg border border-border p-4">
        <div className="flex items-start gap-3">
          <TableIcon size={18} className="mt-0.5 text-text-tertiary" aria-hidden />
          <div>
            <h3 className="text-sm font-semibold">{t("page.enrichment.tables.title")}</h3>
            <p className="text-xs text-text-tertiary">{t("page.enrichment.tables.description")}</p>
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <Input
            ref={registerField("max_table_bytes_mib")}
            label={t("page.enrichment.tables.perTable")}
            value={draft.max_table_bytes_mib}
            onChange={(e) => set("max_table_bytes_mib", e.target.value)}
            inputMode="numeric"
            // O teto é por FORK e o serviço roda 8 — mostrar a conta evita que
            // "32" pareça o consumo total do container.
            helperText={forksHint}
            error={errorField === "max_table_bytes_mib" ? error ?? undefined : undefined}
          />
          <Input
            ref={registerField("lru_bytes_mib")}
            label={t("page.enrichment.tables.lru")}
            value={draft.lru_bytes_mib}
            onChange={(e) => set("lru_bytes_mib", e.target.value)}
            inputMode="numeric"
            helperText={t("page.enrichment.default", { value: "64 MiB" })}
            error={errorField === "lru_bytes_mib" ? error ?? undefined : undefined}
          />
        </div>
      </section>

      {/* ── GeoIP (somente leitura) ─────────────────────────────────── */}
      <section className="space-y-3 rounded-lg border border-border p-4">
        <div className="flex items-start gap-3">
          <GlobeIcon size={18} className="mt-0.5 text-text-tertiary" aria-hidden />
          <div>
            <h3 className="text-sm font-semibold">{t("page.enrichment.geoip.title")}</h3>
            <p className="text-xs text-text-tertiary">{t("page.enrichment.geoip.description")}</p>
          </div>
        </div>
        <p className="font-mono text-xs text-text-tertiary">{config.geoip_dir || "—"}</p>
        {config.geoip_files.length === 0 ? (
          <p className="text-sm text-text-tertiary">{t("page.enrichment.geoip.empty")}</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {config.geoip_files.map((f) => (
              <li key={f.name} className="flex items-center gap-2">
                <Badge variant="success">{fmtBytes(f.size_bytes)}</Badge>
                <span className="font-mono text-xs">{f.name}</span>
                <span className="text-xs text-text-tertiary">
                  {new Date(f.modified_at * 1000).toLocaleDateString()}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="flex items-center justify-between gap-3">
        <p className="text-xs text-text-tertiary">
          {t("page.enrichment.propagation", { seconds: config.propagation_worst_case_s })}
        </p>
        <div className="flex gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={() => {
              setDraft(draftFrom(config))
              setTestResult(null)
              setFeedback(null)
            }}
            disabled={saving}
          >
            {t("page.enrichment.discard")}
          </Button>
          <Button type="submit" variant="primary" loading={saving}>
            {t("page.enrichment.save")}
          </Button>
        </div>
      </div>
    </form>
  )
}

export default EnrichmentConfigForm
