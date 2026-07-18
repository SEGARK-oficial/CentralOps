"use client"

import type React from "react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Trans, useTranslation } from "react-i18next"
import {
  CopyIcon,
  EyeIcon,
  FilterIcon,
  PlayIcon,
  RadioIcon,
  RefreshCwIcon,
  SquareIcon,
  Trash2Icon,
} from "lucide-react"
import * as api from "@/services/api"
import { ApiRequestError } from "@/services/api"
import { useAuth } from "@/contexts/AuthContext"
import type { CaptureEvent, CaptureSession, Organization } from "@/types"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"

// Opções de duração da janela de captura (alinhadas ao backend: 1s–3600s).
const DURATION_OPTIONS: Array<{ value: number; labelKey: string }> = [
  { value: 60, labelKey: "capture.durations.1m" },
  { value: 300, labelKey: "capture.durations.5m" },
  { value: 900, labelKey: "capture.durations.15m" },
  { value: 1800, labelKey: "capture.durations.30m" },
  { value: 3600, labelKey: "capture.durations.1h" },
]

// Default = 15 min, NÃO 5 min. Vários coletores rodam em ciclos de 1–5 min: uma
// janela de 5 min pode abrir e fechar entre dois ciclos e não capturar NADA, o
// que faz o usuário concluir "não passa tráfego" quando na verdade a janela é
// que era curta demais. 15 min cobre com folga a cadência típica.
const DEFAULT_DURATION_SECONDS = 900

// Tamanho do ring (quantos eventos a sessão retém). Alinhado ao backend (1–20000).
const RING_OPTIONS = [1000, 5000, 10000, 20000]

// Cadência do polling enquanto há sessão ativa (sessões + eventos da selecionada).
const POLL_INTERVAL_MS = 3000

// ── Desfecho (outcome) ──────────────────────────────────────────────────────
// O objetivo do troubleshooting é "como entrou e como saiu aquele log": além do
// evento, a captura carrega o DESFECHO (entregue / descartado / sem destino /
// quarentena / …). O campo é OPCIONAL e best-effort — eventos antigos no ring
// (gravados antes do backend passar a anotar o desfecho) simplesmente não têm,
// e a UI precisa continuar renderizando sem quebrar.

/** Sentinela do filtro: "todos os desfechos". */
const OUTCOME_ALL = "__all__"
/** Sentinela do agrupamento: evento sem desfecho anotado (ring antigo). */
const OUTCOME_UNKNOWN = "__unknown__"

type BadgeTone = "success" | "warning" | "danger" | "primary" | "default" | "outline"

/**
 * Cor por CATEGORIA de desfecho. Chaves em snake_case minúsculo; sinônimos
 * mapeados de propósito porque o vocabulário exato do backend ainda pode variar
 * (e um desfecho desconhecido cai no neutro em vez de quebrar).
 */
// Espelha o vocabulário FECHADO do backend (``capture_session.OUTCOMES``): exatamente
// estes 9 desfechos são emitidos. Não inventar chaves — um nome que o backend nunca
// manda vira código morto E some do i18n, fazendo a tela exibir a string crua.
const OUTCOME_TONES: Record<string, BadgeTone> = {
  // entregue de fato ao destino
  delivered: "success",
  // FALHOU na entrega (sink recusou, breaker aberto, destino ausente/cross-tenant) —
  // é o desfecho mais importante p/ troubleshooting: "saiu ou morreu no sink?"
  delivery_failed: "danger",
  // retido p/ inspeção (erro de mapping, customer_id ausente, OCSF)
  quarantined: "danger",
  // saiu do fluxo por DECISÃO do pipeline (não é erro, mas não chega ao destino)
  dropped: "warning",
  unrouted: "warning",
  loop_blocked: "warning",
  residency_blocked: "warning",
  sampled_out: "warning",
  suppressed: "warning",
}

function outcomeTone(outcome: string | null): BadgeTone {
  if (!outcome) return "outline"
  return OUTCOME_TONES[outcome] ?? "default"
}

/** Normaliza para a chave canônica (minúscula, sem espaços). `null` se ausente. */
function normalizeOutcome(value: unknown): string | null {
  if (typeof value !== "string") return null
  const v = value.trim().toLowerCase().replace(/[\s-]+/g, "_")
  return v || null
}

/** Coage um valor solto (string|number) em string não-vazia, senão `null`. */
function coerceField(value: unknown): string | null {
  if (typeof value === "string") return value.trim() || null
  if (typeof value === "number" && Number.isFinite(value)) return String(value)
  return null
}

/**
 * Lê um campo de metadado do evento capturado, tolerando duas formas: no
 * envelope da captura (``ev.outcome``) ou dentro do namespace interno do
 * evento (``ev.event._centralops.outcome``). NUNCA lê campos crus do log do
 * vendor — um log do Windows tem "Outcome" próprio, que não é o nosso.
 */
function metaField(ev: CaptureEvent, key: string): string | null {
  const direct = coerceField((ev as unknown as Record<string, unknown>)[key])
  if (direct != null) return direct
  const payload = ev.event as Record<string, unknown> | undefined
  if (!payload || typeof payload !== "object") return null
  const meta = payload["_centralops"]
  if (!meta || typeof meta !== "object") return null
  return coerceField((meta as Record<string, unknown>)[key])
}

function eventOutcome(ev: CaptureEvent): string | null {
  return normalizeOutcome(metaField(ev, "outcome"))
}

/**
 * Contadores por desfecho expostos (opcionalmente) pelo backend na sessão.
 * Permite distinguir "a sessão não viu NADA" de "viu N eventos" mesmo quando a
 * lista renderizada está vazia (ring podado, filtro ativo, etc.).
 */
function sessionOutcomeCounts(session: CaptureSession | null): Record<string, number> {
  const raw = (session as unknown as Record<string, unknown> | null)?.["outcome_counts"]
  if (!raw || typeof raw !== "object") return {}
  const out: Record<string, number> = {}
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    const n = typeof value === "number" ? value : Number(value)
    const norm = normalizeOutcome(key)
    if (norm && Number.isFinite(n) && n > 0) out[norm] = n
  }
  return out
}

/** Converte epoch-seconds em hora local legível (== null, não falsy: epoch 0 é válido). */
function formatEpoch(seconds?: number | null): string {
  if (seconds == null) return "—"
  return new Date(seconds * 1000).toLocaleString()
}

/** Segundos restantes até expirar (>=0), ou null se sem expiração. */
function remainingSeconds(expiresAt?: number | null): number | null {
  if (expiresAt == null) return null
  return Math.max(0, Math.round(expiresAt - Date.now() / 1000))
}

function statusVariant(status: string): "success" | "outline" | "warning" {
  if (status === "active") return "success"
  if (status === "expired") return "warning"
  return "outline"
}

function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text)
  return new Promise((resolve) => {
    const el = document.createElement("textarea")
    el.value = text
    el.style.position = "fixed"
    el.style.opacity = "0"
    document.body.appendChild(el)
    el.select()
    document.execCommand("copy")
    document.body.removeChild(el)
    resolve()
  })
}

export const CapturePanel: React.FC = () => {
  const { t } = useTranslation("config")
  const { user } = useAuth()
  const [vendor, setVendor] = useState<string>("")
  const [duration, setDuration] = useState<number>(DEFAULT_DURATION_SECONDS)
  const [ringSize, setRingSize] = useState<number>(5000)
  const [starting, setStarting] = useState(false)

  // Captura ao vivo é POR-TENANT. Admin ESCOPADO herda a própria org (backend
  // resolve implicitamente) → nenhum seletor. Admin GLOBAL (is_global ou sem org)
  // precisa escolher a org de destino, senão o backend rejeita com 400
  // "org_id é obrigatório para admin global". Detectamos pelo AuthContext.
  const isGlobalAdmin = useMemo(
    () => !!user && (user.is_global === true || user.organization_id == null),
    [user],
  )
  const [organizations, setOrganizations] = useState<Organization[]>([])
  const [selectedOrgId, setSelectedOrgId] = useState<number | null>(null)
  // Escopo efetivo passado às chamadas de captura: só o admin global manda org_id
  // (e apenas depois de escolher uma). Admin escopado sempre passa undefined.
  const orgScope = useMemo<number | undefined>(
    () => (isGlobalAdmin ? (selectedOrgId ?? undefined) : undefined),
    [isGlobalAdmin, selectedOrgId],
  )
  // Admin global sem org escolhida não pode capturar (evita o 400 do backend).
  const captureBlocked = isGlobalAdmin && selectedOrgId == null

  const [sessions, setSessions] = useState<CaptureSession[]>([])
  const [loadingSessions, setLoadingSessions] = useState(true)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [events, setEvents] = useState<CaptureEvent[]>([])
  const [loadingEvents, setLoadingEvents] = useState(false)
  // Filtro por desfecho (client-side: o payload já vem inteiro no ring).
  const [outcomeFilter, setOutcomeFilter] = useState<string>(OUTCOME_ALL)

  const [busyId, setBusyId] = useState<string | null>(null) // stop/delete em andamento
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [inspected, setInspected] = useState<CaptureEvent | null>(null)
  const [vendorCatalog, setVendorCatalog] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<
    { type: "success" | "error"; message: string } | null
  >(null)

  // Guard de unmount: evita setState em componente desmontado (a request em
  // voo pode resolver depois que o usuário trocou de aba).
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])
  // Guard de poll: pula o tick se o anterior ainda não resolveu (numa conexão
  // lenta, intervalos de 3s acumulariam requests concorrentes que se
  // sobrescrevem em ordem não-determinística).
  const pollingRef = useRef(false)

  // ``silent`` (usado pelo poll) não mexe no spinner — senão o botão
  // "Atualizar" piscaria a cada 3s.
  const loadSessions = useCallback(async (opts?: { silent?: boolean }) => {
    if (!opts?.silent) setLoadingSessions(true)
    try {
      const data = await api.listCaptureSessions(orgScope)
      if (!mountedRef.current) return
      setSessions(data.sessions)
      setError(null)
    } catch (err) {
      if (mountedRef.current)
        setError(err instanceof Error ? err.message : t("capture.loadSessionsError"))
    } finally {
      if (mountedRef.current) setLoadingSessions(false)
    }
  }, [t, orgScope])

  const loadEvents = useCallback(async (sessionId: string, opts?: { silent?: boolean }) => {
    if (!opts?.silent) setLoadingEvents(true)
    try {
      const data = await api.getCaptureEvents(sessionId, 500, orgScope)
      if (!mountedRef.current) return
      setEvents(data.events)
    } catch (err) {
      if (mountedRef.current)
        setError(err instanceof Error ? err.message : t("capture.loadEventsError"))
    } finally {
      if (mountedRef.current) setLoadingEvents(false)
    }
  }, [t, orgScope])

  // Carga inicial: sessões + catálogo de vendors (para o select de escopo).
  useEffect(() => {
    void loadSessions()
  }, [loadSessions])

  useEffect(() => {
    let cancelled = false
    api
      .listPlatformsStreams()
      .then((resp) => {
        if (!cancelled) setVendorCatalog(Object.keys(resp.platforms ?? {}).sort())
      })
      .catch(() => {
        /* não-fatal — o select fica só com "Todos os vendors" */
      })
    return () => {
      cancelled = true
    }
  }, [])

  // Só o admin global escolhe a org — carrega o catálogo (mesmo endpoint do
  // /admin/users). Admin escopado não vê o seletor, então nem lista.
  useEffect(() => {
    if (!isGlobalAdmin) return
    let cancelled = false
    api
      .listOrganizations()
      .then((orgs) => {
        if (!cancelled) setOrganizations(orgs)
      })
      .catch(() => {
        /* não-fatal — o seletor fica vazio e a captura segue bloqueada */
      })
    return () => {
      cancelled = true
    }
  }, [isGlobalAdmin])

  // Trocar de org (admin global) descarta seleção/eventos da org anterior; a
  // lista recarrega sozinha porque loadSessions depende de orgScope.
  const handleOrgChange = (value: string) => {
    setSelectedOrgId(value === "" ? null : Number(value))
    setSelectedId(null)
    setEvents([])
    setOutcomeFilter(OUTCOME_ALL)
    setFeedback(null)
    // Nova org ⇒ nova auto-seleção (a sessão da org anterior não vale mais).
    autoSelectedRef.current = false
  }

  const hasActive = useMemo(() => sessions.some((s) => s.status === "active"), [sessions])
  const selected = useMemo(
    () => sessions.find((s) => s.id === selectedId) ?? null,
    [sessions, selectedId],
  )

  // Polling: enquanto houver sessão ativa, atualiza a lista; se a sessão
  // selecionada estiver ativa, atualiza também os eventos. Para quando nada
  // está ativo (evita bater no backend à toa). ``silent`` p/ não piscar os
  // spinners; ``pollingRef`` evita ticks concorrentes sobrepostos.
  useEffect(() => {
    if (!hasActive) return
    const handle = window.setInterval(() => {
      if (pollingRef.current) return
      pollingRef.current = true
      const tasks = [loadSessions({ silent: true })]
      if (selectedId && selected?.status === "active") {
        tasks.push(loadEvents(selectedId, { silent: true }))
      }
      void Promise.all(tasks).finally(() => {
        pollingRef.current = false
      })
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(handle)
  }, [hasActive, selectedId, selected?.status, loadSessions, loadEvents])

  // Se a sessão selecionada some do poll (expirou e saiu da listagem, ou foi
  // excluída por outro caminho), limpa a seleção pendente e os eventos stale.
  useEffect(() => {
    if (selectedId && !selected) {
      setSelectedId(null)
      setEvents([])
    }
  }, [selectedId, selected])

  // Auto-seleção ao montar: sem isto, recarregar a página deixava a tela vazia
  // mesmo com os eventos VIVOS no Redis — o usuário lia isso como "não capturei
  // nada". Seleciona a sessão ativa mais recente (senão a mais recente de
  // todas) e já busca os eventos. Roda UMA vez por escopo (o ref é resetado na
  // troca de org), para não sequestrar a seleção manual do usuário depois.
  const autoSelectedRef = useRef(false)
  useEffect(() => {
    if (autoSelectedRef.current || selectedId || sessions.length === 0) return
    const preferred = sessions.find((s) => s.status === "active") ?? sessions[0]
    if (!preferred) return
    autoSelectedRef.current = true
    setSelectedId(preferred.id)
    void loadEvents(preferred.id, { silent: true })
  }, [sessions, selectedId, loadEvents])

  // Busca FINAL ao encerrar: quando a sessão selecionada sai de "active"
  // (expirou ou foi parada), o polling para — e sem um último fetch os eventos
  // gravados no fim da janela nunca chegariam à tela. Dispara exatamente na
  // transição (guardando id+status anteriores), não a cada render.
  const lastStatusRef = useRef<{ id: string; status: string } | null>(null)
  useEffect(() => {
    const prev = lastStatusRef.current
    if (!selectedId || !selected) {
      lastStatusRef.current = null
      return
    }
    lastStatusRef.current = { id: selectedId, status: selected.status }
    if (prev && prev.id === selectedId && prev.status === "active" && selected.status !== "active") {
      void loadEvents(selectedId, { silent: true })
    }
  }, [selectedId, selected, loadEvents])

  const handleStart = async () => {
    try {
      setStarting(true)
      setFeedback(null)
      const session = await api.startCaptureSession(
        {
          vendor: vendor || undefined,
          duration_seconds: duration,
          ring_size: ringSize,
        },
        orgScope,
      )
      setFeedback({
        type: "success",
        message: t("capture.startSuccess", {
          vendorSuffix: session.vendor ? t("capture.startVendorSuffix", { vendor: session.vendor }) : "",
        }),
      })
      setSelectedId(session.id)
      setEvents([])
      setOutcomeFilter(OUTCOME_ALL)
      autoSelectedRef.current = true // seleção explícita vence a auto-seleção
      await loadSessions()
    } catch (err) {
      const isLimit = err instanceof ApiRequestError && err.statusCode === 429
      setFeedback({
        type: "error",
        message: isLimit
          ? t("capture.limitReached")
          : err instanceof Error
            ? err.message
            : t("capture.startError"),
      })
    } finally {
      setStarting(false)
    }
  }

  const handleSelect = (sessionId: string) => {
    setSelectedId(sessionId)
    setOutcomeFilter(OUTCOME_ALL)
    autoSelectedRef.current = true
    void loadEvents(sessionId)
  }

  const handleStop = async (sessionId: string) => {
    try {
      setBusyId(sessionId)
      await api.stopCaptureSession(sessionId, orgScope)
      setFeedback({ type: "success", message: t("capture.stopSuccess") })
      await loadSessions()
    } catch (err) {
      setFeedback({ type: "error", message: err instanceof Error ? err.message : t("capture.stopError") })
    } finally {
      setBusyId(null)
    }
  }

  const handleDelete = async (sessionId: string) => {
    try {
      setBusyId(sessionId)
      await api.deleteCaptureSession(sessionId, orgScope)
      if (selectedId === sessionId) {
        setSelectedId(null)
        setEvents([])
      }
      setConfirmDelete(null)
      setFeedback({ type: "success", message: t("capture.deleteSuccess") })
      await loadSessions()
    } catch (err) {
      setFeedback({ type: "error", message: err instanceof Error ? err.message : t("capture.deleteError") })
    } finally {
      setBusyId(null)
    }
  }

  const handleCopyJson = async (ev: CaptureEvent) => {
    await copyToClipboard(JSON.stringify(ev.event, null, 2))
    setFeedback({ type: "success", message: t("capture.copyJsonSuccess") })
  }

  // ── Desfechos da sessão selecionada ───────────────────────────────────────
  /** Rótulo traduzido do desfecho; desconhecido cai no próprio código cru. */
  const outcomeLabel = useCallback(
    (key: string) =>
      key === OUTCOME_UNKNOWN
        ? t("capture.outcomes.unknown")
        : t(`capture.outcomes.${key}`, { defaultValue: key }),
    [t],
  )

  const outcomeCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const ev of events) {
      const key = eventOutcome(ev) ?? OUTCOME_UNKNOWN
      counts[key] = (counts[key] ?? 0) + 1
    }
    return counts
  }, [events])

  // Só mostra chips/filtro/coluna se ALGUM evento trouxe desfecho. Num ring
  // antigo (sem o campo) a UI volta a ser exatamente a de antes.
  const hasOutcomeData = useMemo(
    () => Object.keys(outcomeCounts).some((k) => k !== OUTCOME_UNKNOWN),
    [outcomeCounts],
  )

  const filteredEvents = useMemo(() => {
    if (outcomeFilter === OUTCOME_ALL) return events
    return events.filter((ev) => (eventOutcome(ev) ?? OUTCOME_UNKNOWN) === outcomeFilter)
  }, [events, outcomeFilter])

  // Contadores vindos do backend (opcionais): distinguem "a sessão não viu
  // nada" de "viu N eventos" mesmo com a lista renderizada vazia.
  const selectedCounts = useMemo(() => sessionOutcomeCounts(selected), [selected])
  const selectedCountsTotal = useMemo(
    () => Object.values(selectedCounts).reduce((acc, n) => acc + n, 0),
    [selectedCounts],
  )

  /** Tamanho da janela da sessão, em minutos (para o texto do estado vazio). */
  const selectedWindowMinutes = useMemo(() => {
    if (selected?.created_at == null || selected?.expires_at == null) return null
    const minutes = Math.round((selected.expires_at - selected.created_at) / 60)
    return minutes > 0 ? minutes : null
  }, [selected])

  return (
    <div className="space-y-4">
      <Notice variant="info" title={t("capture.intro.title")}>
        <Trans i18nKey="capture.intro.body" t={t} components={{ strong: <strong /> }} />
      </Notice>

      {feedback && (
        <Notice variant={feedback.type === "success" ? "success" : "danger"}>
          {feedback.message}
        </Notice>
      )}
      {error && <Notice variant="danger">{error}</Notice>}

      {/* Formulário de início */}
      <div className="flex flex-wrap items-end gap-3 rounded-md border border-border bg-surface p-3">
        {isGlobalAdmin && (
          <label className="flex flex-col gap-1 text-xs font-medium text-text-secondary">
            {t("capture.form.orgScope")}
            <select
              className="h-8 rounded border border-border bg-surface px-2 text-sm text-text"
              value={selectedOrgId ?? ""}
              onChange={(e) => handleOrgChange(e.target.value)}
              aria-label={t("capture.form.orgAriaLabel")}
            >
              <option value="">{t("capture.form.orgPlaceholder")}</option>
              {organizations.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="flex flex-col gap-1 text-xs font-medium text-text-secondary">
          {t("capture.form.vendorScope")}
          <select
            className="h-8 rounded border border-border bg-surface px-2 text-sm text-text"
            value={vendor}
            onChange={(e) => setVendor(e.target.value)}
            aria-label={t("capture.form.vendorAriaLabel")}
          >
            <option value="">{t("capture.form.allVendors")}</option>
            {vendorCatalog.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-text-secondary">
          {t("capture.form.duration")}
          <select
            className="h-8 rounded border border-border bg-surface px-2 text-sm text-text"
            value={duration}
            onChange={(e) => setDuration(Number(e.target.value))}
            aria-label={t("capture.form.durationAriaLabel")}
          >
            {DURATION_OPTIONS.map((d) => (
              <option key={d.value} value={d.value}>
                {t(d.labelKey)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-text-secondary">
          {t("capture.form.bufferSize")}
          <select
            className="h-8 rounded border border-border bg-surface px-2 text-sm text-text"
            value={ringSize}
            onChange={(e) => setRingSize(Number(e.target.value))}
            aria-label={t("capture.form.bufferSizeAriaLabel")}
          >
            {RING_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {t("capture.form.eventsUnit", { count: n })}
              </option>
            ))}
          </select>
        </label>
        <Button
          variant="primary"
          size="sm"
          leftIcon={<PlayIcon size={14} />}
          onClick={() => void handleStart()}
          loading={starting}
          disabled={captureBlocked}
        >
          {t("capture.form.start")}
        </Button>
        <div className="ml-auto">
          <Button
            variant="outline"
            size="sm"
            leftIcon={<RefreshCwIcon size={14} />}
            onClick={() => void loadSessions()}
            loading={loadingSessions}
          >
            {t("capture.form.refresh")}
          </Button>
        </div>
      </div>

      {/* Janela x cadência dos coletores: uma janela curta pode fechar entre
          dois ciclos de coleta e não capturar nada — dizer isso ANTES evita a
          conclusão errada de "não passa tráfego". */}
      <p className="text-xs text-text-tertiary" role="note">
        {t("capture.form.durationHint")}
      </p>

      {/* Admin global precisa escolher a org antes de capturar. */}
      {captureBlocked && (
        <p className="text-xs text-text-tertiary" role="note">
          {t("capture.form.orgRequiredHint")}
        </p>
      )}

      {/* Lista de sessões */}
      {loadingSessions && sessions.length === 0 ? (
        <div className="flex justify-center py-10">
          <LoadingSpinner size="md" text={t("capture.sessionsLoading")} />
        </div>
      ) : sessions.length === 0 ? (
        <EmptyState
          icon={<RadioIcon size={32} />}
          title={t("capture.sessionsEmptyTitle")}
          description={t("capture.sessionsEmptyDescription")}
        />
      ) : (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-sm">
            <thead className="bg-surface-tertiary text-xs uppercase tracking-wider text-text-secondary">
              <tr>
                <th className="px-3 py-2 text-left">{t("capture.table.vendor")}</th>
                <th className="px-3 py-2 text-left">{t("capture.table.status")}</th>
                <th className="px-3 py-2 text-left">{t("capture.table.events")}</th>
                <th className="px-3 py-2 text-left">{t("capture.table.started")}</th>
                <th className="px-3 py-2 text-left">{t("capture.table.expires")}</th>
                <th className="px-3 py-2 text-right">{t("capture.table.actions")}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {sessions.map((s) => {
                const remain = remainingSeconds(s.expires_at)
                const isSelected = s.id === selectedId
                return (
                  <tr
                    key={s.id}
                    className={isSelected ? "bg-primary-50/40 dark:bg-primary-900/10" : undefined}
                  >
                    <td className="px-3 py-2 text-text">{s.vendor ?? t("capture.table.allVendors")}</td>
                    <td className="px-3 py-2">
                      <Badge variant={statusVariant(s.status)} size="sm">
                        {s.status}
                      </Badge>
                    </td>
                    <td className="px-3 py-2 text-text">{s.event_count}</td>
                    <td className="px-3 py-2 text-text-secondary">
                      <code className="text-xs">{formatEpoch(s.created_at)}</code>
                    </td>
                    <td className="px-3 py-2 text-text-secondary">
                      {s.status === "active" && remain != null ? (
                        <span className="text-xs">{t("capture.table.expiresIn", { seconds: remain })}</span>
                      ) : (
                        <code className="text-xs">{formatEpoch(s.expires_at)}</code>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex justify-end gap-1">
                        <Button
                          size="xs"
                          variant="ghost"
                          leftIcon={<EyeIcon size={12} />}
                          onClick={() => handleSelect(s.id)}
                          title={t("capture.table.viewEventsTooltip")}
                        >
                          {t("capture.table.viewEvents")}
                        </Button>
                        {s.status === "active" && (
                          <Button
                            size="xs"
                            variant="outline"
                            leftIcon={<SquareIcon size={12} />}
                            onClick={() => void handleStop(s.id)}
                            loading={busyId === s.id}
                            title={t("capture.table.stopTooltip")}
                          >
                            {t("capture.table.stop")}
                          </Button>
                        )}
                        <Button
                          size="xs"
                          variant="ghost"
                          leftIcon={<Trash2Icon size={12} />}
                          onClick={() => setConfirmDelete(s.id)}
                          disabled={busyId === s.id}
                          title={t("capture.table.deleteTooltip")}
                        >
                          {t("capture.table.delete")}
                        </Button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Eventos da sessão selecionada */}
      {selected && (
        <div className="space-y-2 rounded-md border border-border p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-2 text-sm font-semibold text-text">
              <RadioIcon size={16} className="text-primary-600" />
              {t("capture.events.title", { vendor: selected.vendor ?? t("capture.events.allVendors") })}
              <Badge variant={statusVariant(selected.status)} size="sm">
                {selected.status}
              </Badge>
              {selected.status === "active" && (
                <span className="text-xs font-normal text-text-tertiary">
                  {t("capture.events.liveUpdating")}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              {/* Filtro por desfecho: só existe se algum evento trouxe o campo. */}
              {hasOutcomeData && (
                <select
                  className="h-7 rounded border border-border bg-surface px-2 text-xs text-text"
                  value={outcomeFilter}
                  onChange={(e) => setOutcomeFilter(e.target.value)}
                  aria-label={t("capture.events.outcomeFilterAriaLabel")}
                >
                  <option value={OUTCOME_ALL}>
                    {t("capture.events.outcomeFilterAll", { total: events.length })}
                  </option>
                  {Object.entries(outcomeCounts)
                    .sort((a, b) => b[1] - a[1])
                    .map(([key, count]) => (
                      <option key={key} value={key}>
                        {`${outcomeLabel(key)} (${count})`}
                      </option>
                    ))}
                </select>
              )}
              <Button
                size="xs"
                variant="ghost"
                leftIcon={<RefreshCwIcon size={12} />}
                onClick={() => void loadEvents(selected.id)}
                loading={loadingEvents}
              >
                {t("capture.events.refresh")}
              </Button>
            </div>
          </div>

          {/* Resumo "como saiu": um chip por desfecho, clicável = filtro. */}
          {hasOutcomeData && (
            <div className="flex flex-wrap items-center gap-1">
              {Object.entries(outcomeCounts)
                .sort((a, b) => b[1] - a[1])
                .map(([key, count]) => {
                  const activeChip = outcomeFilter === key
                  return (
                    <button
                      key={key}
                      type="button"
                      onClick={() => setOutcomeFilter(activeChip ? OUTCOME_ALL : key)}
                      aria-pressed={activeChip}
                      className={
                        activeChip
                          ? "rounded-full ring-2 ring-primary-500 ring-offset-1 ring-offset-surface"
                          : "rounded-full"
                      }
                      title={t("capture.events.outcomeChipTooltip")}
                    >
                      <Badge variant={outcomeTone(key === OUTCOME_UNKNOWN ? null : key)} size="sm">
                        {outcomeLabel(key)} · {count}
                      </Badge>
                    </button>
                  )
                })}
            </div>
          )}

          {loadingEvents && events.length === 0 ? (
            <div className="flex justify-center py-6">
              <LoadingSpinner size="sm" text={t("capture.events.loading")} />
            </div>
          ) : filteredEvents.length === 0 && events.length > 0 ? (
            /* Não é "sem tráfego": é o FILTRO que escondeu tudo. */
            <EmptyState
              icon={<FilterIcon size={28} />}
              title={t("capture.events.filteredEmptyTitle", {
                outcome: outcomeLabel(outcomeFilter),
              })}
              description={t("capture.events.filteredEmptyDescription", { total: events.length })}
              action={
                <Button size="xs" variant="outline" onClick={() => setOutcomeFilter(OUTCOME_ALL)}>
                  {t("capture.events.clearOutcomeFilter")}
                </Button>
              }
            />
          ) : events.length === 0 && selected.status === "active" ? (
            /* ESTADO VAZIO HONESTO: sessão ativa e nada ainda. Sem explicação,
               o usuário conclui "não capturei nada" sem distinguir "não houve
               tráfego" de "ainda não rodou um ciclo de coleta". */
            <EmptyState
              icon={<RadioIcon size={28} />}
              title={t("capture.events.waitingTitle")}
              description={t("capture.events.waitingDescription")}
              action={
                <div className="max-w-md space-y-1 text-left text-xs text-text-tertiary">
                  <p>{t("capture.events.waitingWhyPipeline")}</p>
                  <p>{t("capture.events.waitingWhyCadence")}</p>
                  {selectedWindowMinutes != null && (
                    <p>
                      {t("capture.events.waitingWindow", { minutes: selectedWindowMinutes })}
                    </p>
                  )}
                  {selected.vendor && (
                    <p>{t("capture.events.waitingVendorFilter", { vendor: selected.vendor })}</p>
                  )}
                  {/* Contadores do backend (se existirem) desmentem o "vazio":
                      houve tráfego, ele só não está no ring renderizado. */}
                  {selectedCountsTotal > 0 && (
                    <p className="text-text-secondary">
                      {t("capture.events.waitingServerCounts", { total: selectedCountsTotal })}
                    </p>
                  )}
                </div>
              }
            />
          ) : filteredEvents.length === 0 ? (
            /* Janela encerrada sem nenhum evento. */
            <EmptyState
              icon={<RadioIcon size={28} />}
              title={t("capture.events.emptyTitle")}
              description={t("capture.events.emptyDescription")}
              action={
                <div className="max-w-md space-y-1 text-left text-xs text-text-tertiary">
                  {selectedWindowMinutes != null && (
                    <p>{t("capture.events.emptyWindow", { minutes: selectedWindowMinutes })}</p>
                  )}
                  <p>{t("capture.events.emptyRetryHint")}</p>
                  {selectedCountsTotal > 0 && (
                    <p className="text-text-secondary">
                      {t("capture.events.waitingServerCounts", { total: selectedCountsTotal })}
                    </p>
                  )}
                </div>
              }
            />
          ) : (
            <div className="overflow-x-auto rounded border border-border">
              <table className="w-full text-sm">
                <thead className="bg-surface-tertiary text-xs uppercase tracking-wider text-text-secondary">
                  <tr>
                    <th className="px-3 py-2 text-left">{t("capture.events.table.capturedAt")}</th>
                    <th className="px-3 py-2 text-left">{t("capture.events.table.vendor")}</th>
                    {hasOutcomeData && (
                      <th className="px-3 py-2 text-left">{t("capture.events.table.outcome")}</th>
                    )}
                    <th className="px-3 py-2 text-left">{t("capture.events.table.preview")}</th>
                    <th className="px-3 py-2 text-right">{t("capture.events.table.actions")}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {filteredEvents.map((ev, idx) => {
                    const outcome = eventOutcome(ev)
                    const destination = metaField(ev, "destination_id")
                    const detail = metaField(ev, "detail")
                    return (
                    <tr key={`${selected.id}-${ev.captured_at ?? idx}-${ev.vendor ?? ""}-${idx}`}>
                      <td className="px-3 py-2 text-text-secondary">
                        <code className="text-xs">{formatEpoch(ev.captured_at)}</code>
                      </td>
                      <td className="px-3 py-2 text-text">{ev.vendor ?? "—"}</td>
                      {hasOutcomeData && (
                        <td className="px-3 py-2">
                          {outcome ? (
                            <div className="flex flex-col gap-0.5">
                              <Badge variant={outcomeTone(outcome)} size="sm" title={detail ?? undefined}>
                                {outcomeLabel(outcome)}
                              </Badge>
                              {destination && (
                                <span className="text-[10px] text-text-tertiary">
                                  {t("capture.events.destinationShort", { destination })}
                                </span>
                              )}
                            </div>
                          ) : (
                            /* Evento antigo no ring (gravado antes do desfecho
                               existir): não quebra, só não sabemos o desfecho. */
                            <span className="text-xs text-text-tertiary" title={t("capture.outcomes.unknownTooltip")}>
                              —
                            </span>
                          )}
                        </td>
                      )}
                      <td className="px-3 py-2">
                        <code className="block max-w-[420px] truncate text-xs text-text-secondary">
                          {JSON.stringify(ev.event)}
                        </code>
                      </td>
                      <td className="px-3 py-2">
                        <div className="flex justify-end gap-1">
                          <Button
                            size="xs"
                            variant="ghost"
                            leftIcon={<EyeIcon size={12} />}
                            onClick={() => setInspected(ev)}
                            title={t("capture.events.inspectTooltip")}
                          >
                            {t("capture.events.inspect")}
                          </Button>
                          <Button
                            size="xs"
                            variant="ghost"
                            leftIcon={<CopyIcon size={12} />}
                            onClick={() => void handleCopyJson(ev)}
                            title={t("capture.events.jsonTooltip")}
                          >
                            {t("capture.events.json")}
                          </Button>
                        </div>
                      </td>
                    </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Modal de inspeção */}
      <Modal
        open={inspected !== null}
        onClose={() => setInspected(null)}
        title={inspected ? t("capture.inspectModal.title", { vendor: inspected.vendor ?? "?" }) : t("capture.inspectModal.defaultTitle")}
        size="lg"
      >
        {inspected && (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2 text-xs">
              {inspected.vendor && <Badge variant="outline">{inspected.vendor}</Badge>}
              {inspected.captured_at && (
                <Badge variant="outline">{formatEpoch(inspected.captured_at)}</Badge>
              )}
              {/* "Como saiu": desfecho + destino + motivo, quando o backend anota. */}
              {(() => {
                const outcome = eventOutcome(inspected)
                return outcome ? (
                  <Badge variant={outcomeTone(outcome)}>
                    {t("capture.inspectModal.outcome", { outcome: outcomeLabel(outcome) })}
                  </Badge>
                ) : null
              })()}
              {(() => {
                const destination = metaField(inspected, "destination_id")
                return destination ? (
                  <Badge variant="outline">
                    {t("capture.inspectModal.destination", { destination })}
                  </Badge>
                ) : null
              })()}
            </div>
            {(() => {
              const detail = metaField(inspected, "detail")
              return detail ? (
                <p className="text-xs text-text-secondary">
                  {t("capture.inspectModal.detail", { detail })}
                </p>
              ) : null
            })()}
            <div className="rounded bg-surface-tertiary p-3">
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-all text-xs text-text">
                {JSON.stringify(inspected.event, null, 2)}
              </pre>
              <div className="mt-2 flex justify-end">
                <Button
                  size="xs"
                  variant="outline"
                  leftIcon={<CopyIcon size={12} />}
                  onClick={() => void handleCopyJson(inspected)}
                >
                  {t("capture.inspectModal.copyJson")}
                </Button>
              </div>
            </div>
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={confirmDelete !== null}
        title={t("capture.deleteDialog.title")}
        description={t("capture.deleteDialog.description")}
        confirmLabel={t("capture.deleteDialog.confirm")}
        confirmVariant="danger"
        loading={busyId === confirmDelete}
        onConfirm={() => confirmDelete && void handleDelete(confirmDelete)}
        onClose={() => setConfirmDelete(null)}
      />
    </div>
  )
}

export default CapturePanel
