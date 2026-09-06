/**
 * IngestSourcePanel — painel de ingestão para fontes PUSH.
 *
 * Auto-oculta para fontes pull: ao montar, chama `getIngestInfo`; se a integração
 * não for push (422) o painel não renderiza nada. Para fontes push, mostra o
 * endpoint, os streams, a profundidade do buffer, e permite emitir/rotacionar o
 * token de ingestão (mostrado UMA vez) + um snippet pronto de edge-collector.
 */
import type React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { CopyIcon, CheckIcon, KeyRoundIcon, LayersIcon, PlusIcon, RadioTowerIcon, ShieldXIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { Link } from "react-router-dom"
import * as api from "@/services/api"
import type { CustomStream, IngestInfo } from "@/services/api"
import { Card } from "@/components/ui/Card/Card"
import { Button } from "@/components/ui/Button/Button"
import { Badge } from "@/components/ui/Badge/Badge"
import { Input } from "@/components/ui/Input/Input"
import { Notice } from "@/components/ui/Notice/Notice"
import { Select } from "@/components/ui/Select/Select"
import { brandIconFor } from "@/lib/brand-icons"

/** Plataforma push genérica: os streams são criados pelo operador (um mapping cada). */
export const CUSTOM_JSON_PLATFORM = "custom_json"

/**
 * Classes OCSF 1.8 que o backend aceita para um stream novo — espelha
 * `normalize/ocsf/classes.CLASS_NAMES` (o backend é a fonte da verdade e devolve
 * 422 `mapping.invalid_class_uid` para qualquer outra).
 */
export const OCSF_CLASS_OPTIONS: ReadonlyArray<{ value: number; label: string }> = [
  { value: 0, label: "0 · Base Event (log heterogêneo)" },
  { value: 1006, label: "1006 · Scheduled Job Activity" },
  { value: 2004, label: "2004 · Detection Finding" },
  { value: 2005, label: "2005 · Incident Finding" },
  { value: 3001, label: "3001 · Account Change" },
  { value: 3002, label: "3002 · Authentication" },
  { value: 4001, label: "4001 · Network Activity" },
  { value: 6003, label: "6003 · API Activity" },
]

const STREAM_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,62}$/

interface IngestSourcePanelProps {
  integrationId: number
  platform: string
  /** Somente admin pode emitir token. */
  canManage?: boolean
}

function CopyButton({ text }: { text: string }) {
  const { t } = useTranslation("integrations")
  const [copied, setCopied] = useState(false)
  const copy = useCallback(() => {
    void navigator.clipboard?.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }, [text])
  return (
    <Button type="button" variant="outline" size="sm" onClick={copy} aria-label={t("common:actions.copy")}>
      {copied ? <CheckIcon size={14} /> : <CopyIcon size={14} />}
      {copied ? t("ingest.copied") : t("ingest.copy")}
    </Button>
  )
}

function edgeSnippet(platform: string, endpoint: string, token: string): string {
  const tok = token || "<SEU_TOKEN_DE_INGESTAO>"
  if (platform === CUSTOM_JSON_PLATFORM) {
    // Genérico: NDJSON (1 objeto por linha) de qualquer edge-collector ou script.
    return [
      "# Qualquer edge-collector: NDJSON (um JSON por linha) + token no header",
      `curl -X POST "${endpoint}" \\`,
      `  -H "Authorization: Bearer ${tok}" \\`,
      '  -H "Content-Type: application/x-ndjson" \\',
      "  --data-binary $'{\"message\":\"login failed\",\"user\":\"alice\"}\\n{\"message\":\"login ok\",\"user\":\"bob\"}'",
      "",
      "# Vector: qualquer source → sink http",
      "[sinks.centralops]",
      'type = "http"',
      'inputs = ["<sua_source>"]',
      `uri = "${endpoint}"`,
      'method = "post"',
      'encoding.codec = "json"',
      'framing.method = "newline_delimited"',
      `request.headers.Authorization = "Bearer ${tok}"`,
    ].join("\n")
  }
  if (platform === "windows_event_log") {
    // Fluent Bit no servidor coletor WEC (input winevtlog → output http).
    let host = "centralops.example.com"
    let port = "443"
    let uri = "/api/ingest/security"
    let tls = "On"
    try {
      const u = new URL(endpoint)
      host = u.hostname
      port = u.port || (u.protocol === "http:" ? "80" : "443")
      uri = u.pathname
      tls = u.protocol === "http:" ? "Off" : "On"
    } catch {
      /* mantém defaults */
    }
    return [
      "# Fluent Bit (no servidor coletor WEC) — encaminha os eventos ao CentralOps",
      "[INPUT]",
      "    Name      winevtlog",
      "    Channels  ForwardedEvents,Security",
      "    Tag       win.events",
      "",
      "[OUTPUT]",
      "    Name      http",
      "    Match     win.events",
      `    Host      ${host}`,
      `    Port      ${port}`,
      `    URI       ${uri}`,
      `    TLS       ${tls}`,
      "    Format    json_lines",
      `    Header    Authorization Bearer ${tok}`,
    ].join("\n")
  }
  // FortiGate: syslog → Vector → endpoint.
  return [
    "# Vector — recebe syslog do FortiGate e encaminha ao CentralOps",
    "[sources.fortigate]",
    'type = "syslog"',
    'address = "0.0.0.0:5514"',
    'mode = "udp"',
    "",
    "[sinks.centralops]",
    'type = "http"',
    'inputs = ["fortigate"]',
    `uri = "${endpoint}"`,
    'method = "post"',
    'encoding.codec = "json"',
    'framing.method = "newline_delimited"',
    `request.headers.Authorization = "Bearer ${tok}"`,
  ].join("\n")
}

export const IngestSourcePanel: React.FC<IngestSourcePanelProps> = ({ integrationId, platform, canManage = true }) => {
  const { t } = useTranslation("integrations")
  const [info, setInfo] = useState<IngestInfo | null>(null)
  const [hidden, setHidden] = useState(false)
  const [issuing, setIssuing] = useState(false)
  const [revoking, setRevoking] = useState(false)
  const [token, setToken] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // ── Streams da fonte genérica (custom_json) ──────────────────────────
  const isCustom = platform === CUSTOM_JSON_PLATFORM
  const [customStreams, setCustomStreams] = useState<CustomStream[]>([])
  const [streamName, setStreamName] = useState("")
  const [streamClass, setStreamClass] = useState<number>(0)
  const [streamDescription, setStreamDescription] = useState("")
  const [creating, setCreating] = useState(false)
  const [streamError, setStreamError] = useState<string | null>(null)
  const [streamNotice, setStreamNotice] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    // `await` tolera mock/retorno indefinido (testes) sem quebrar; 422 (fonte
    // pull) → oculta o painel.
    void (async () => {
      try {
        const i = await api.getIngestInfo(integrationId)
        if (cancelled) return
        if (i && i.transport === "push") setInfo(i)
        else setHidden(true)
      } catch {
        if (!cancelled) setHidden(true)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [integrationId])

  // Depende de "tem info", não do objeto: `setInfo` após criar um stream
  // re-disparava a busca e sobrescrevia a lista recém-atualizada.
  const hasInfo = info !== null
  useEffect(() => {
    if (!isCustom || !hasInfo) return
    let cancelled = false
    void (async () => {
      try {
        const list = await api.listCustomStreams()
        if (!cancelled && Array.isArray(list)) setCustomStreams(list)
      } catch {
        if (!cancelled) setStreamError(t("ingest.customStreams.loadError"))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [isCustom, hasInfo, t])

  const streamNameValid = STREAM_NAME_RE.test(streamName.trim())

  const handleCreateStream = useCallback(async () => {
    const name = streamName.trim()
    if (!STREAM_NAME_RE.test(name)) return
    setCreating(true)
    setStreamError(null)
    setStreamNotice(null)
    try {
      const created = await api.createCustomStream({
        stream: name,
        ocsf_class_uid: streamClass,
        description: streamDescription.trim() || undefined,
      })
      setCustomStreams((prev) => [...prev.filter((s) => s.stream !== created.stream), created])
      // O endpoint passa a aceitar o stream na hora: reflete nos badges/endpoint.
      setInfo((prev) => (prev && !prev.streams.includes(created.stream) ? { ...prev, streams: [...prev.streams, created.stream] } : prev))
      setStreamName("")
      setStreamDescription("")
      setStreamNotice(t("ingest.customStreams.created", { stream: created.stream }))
    } catch (e) {
      setStreamError(e instanceof Error ? e.message : t("ingest.customStreams.createError"))
    } finally {
      setCreating(false)
    }
  }, [streamName, streamClass, streamDescription, t])

  const origin = typeof window !== "undefined" ? window.location.origin : ""
  const primaryStream = info?.streams?.[0] ?? (isCustom ? "<stream>" : "events")
  const endpoint = `${origin}${info?.endpoint_base ?? "/api/ingest"}/${primaryStream}`

  const snippet = useMemo(
    () => edgeSnippet(platform, endpoint, token ?? ""),
    [platform, endpoint, token],
  )

  const handleIssue = useCallback(async () => {
    setIssuing(true)
    setError(null)
    setNotice(null)
    try {
      const res = await api.issueIngestToken(integrationId)
      setToken(res.token)
      setInfo((prev) => (prev ? { ...prev, has_token: true } : prev))
    } catch (e) {
      setError(e instanceof Error ? e.message : t("ingest.issueError"))
    } finally {
      setIssuing(false)
    }
  }, [integrationId, t])

  const handleRevoke = useCallback(async () => {
    // Revogação é destrutiva (o edge-collector para de ingerir na hora): confirma.
    if (typeof window !== "undefined" && !window.confirm(t("ingest.revokeConfirm"))) return
    setRevoking(true)
    setError(null)
    setNotice(null)
    try {
      await api.revokeIngestToken(integrationId)
      setToken(null)
      setInfo((prev) => (prev ? { ...prev, has_token: false } : prev))
      setNotice(t("ingest.revokeSuccess"))
    } catch (e) {
      setError(e instanceof Error ? e.message : t("ingest.revokeError"))
    } finally {
      setRevoking(false)
    }
  }, [integrationId, t])

  if (hidden) return null
  if (!info) return null

  return (
    <Card className="space-y-4 p-5">
      <div className="flex items-center gap-3">
        <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-white ring-1 ring-black/5">
          {/* Plugin-driven: icon_id vem do catálogo (backend), sem hardcode por plataforma. */}
          {brandIconFor(info.icon_id ?? platform, { size: 22 })}
        </span>
        <div>
          <h3 className="flex items-center gap-2 text-sm font-semibold text-text">
            <RadioTowerIcon size={15} className="text-primary-600" /> {t("ingest.title")}
          </h3>
          <p className="text-xs text-text-secondary">
            {t("ingest.bufferDepth", { count: info.buffer_depth })}
          </p>
        </div>
        <div className="ml-auto flex flex-wrap gap-1.5">
          {info.streams.map((s) => (
            <Badge key={s} variant="default" size="sm">{s}</Badge>
          ))}
        </div>
      </div>

      {/* Endpoint */}
      <div className="space-y-1.5">
        <span className="text-xs font-medium text-text-secondary">{t("ingest.endpointLabel")}</span>
        <div className="flex items-center gap-2">
          <code className="flex-1 truncate rounded-md border border-border bg-surface-tertiary px-3 py-2 text-xs text-text">
            POST {endpoint}
          </code>
          <CopyButton text={endpoint} />
        </div>
      </div>

      {/* Token */}
      <div className="space-y-1.5">
        <span className="text-xs font-medium text-text-secondary">{t("ingest.tokenLabel")}</span>
        {token ? (
          <Notice variant="warning" title={t("ingest.tokenWarningTitle")}>
            <div className="mt-1 flex items-center gap-2">
              <code className="flex-1 break-all rounded-md border border-border bg-surface px-3 py-2 text-xs">{token}</code>
              <CopyButton text={token} />
            </div>
          </Notice>
        ) : (
          <div className="flex items-center gap-2">
            <span className="text-xs text-text-tertiary">
              {info.has_token ? t("ingest.tokenAlreadyIssued") : t("ingest.tokenNotIssued")}
            </span>
            {canManage && (
              <div className="ml-auto flex items-center gap-2">
                {info.has_token && (
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={handleRevoke}
                    loading={revoking}
                    disabled={issuing}
                    className="text-danger-600 hover:bg-danger-50"
                  >
                    <ShieldXIcon size={14} /> {t("ingest.revoke")}
                  </Button>
                )}
                <Button type="button" size="sm" onClick={handleIssue} loading={issuing} disabled={revoking}>
                  <KeyRoundIcon size={14} /> {info.has_token ? t("ingest.rotateToken") : t("ingest.issueToken")}
                </Button>
              </div>
            )}
          </div>
        )}
        {notice && <p className="text-xs text-text-secondary">{notice}</p>}
        {error && <p className="text-xs text-danger-600">{error}</p>}
      </div>

      {/* Streams da fonte genérica: criados pelo operador, um mapping cada. */}
      {isCustom && (
        <div className="space-y-2" data-testid="custom-streams">
          <div className="flex items-center gap-2">
            <LayersIcon size={14} className="text-primary-600" aria-hidden />
            <span className="text-xs font-medium text-text-secondary">{t("ingest.customStreams.title")}</span>
          </div>
          <p className="text-xs text-text-tertiary">{t("ingest.customStreams.help")}</p>
          {customStreams.length === 0 ? (
            <Notice variant="warning">{t("ingest.customStreams.empty")}</Notice>
          ) : (
            <ul className="divide-y divide-border rounded-md border border-border">
              {customStreams.map((s) => (
                <li key={s.stream} className="flex items-center gap-3 px-3 py-2 text-xs">
                  <code className="font-medium text-text">{s.stream}</code>
                  <span className="text-text-tertiary">{s.ocsf_class_name} ({s.ocsf_class_uid})</span>
                  <code className="ml-auto truncate text-text-tertiary">POST {s.endpoint}</code>
                  <Link to={`/mappings/${s.definition_id}`} className="text-primary-600 hover:underline">
                    {t("ingest.customStreams.openMapping")}
                  </Link>
                </li>
              ))}
            </ul>
          )}
          {canManage && (
            <form
              className="grid gap-2 sm:grid-cols-[1fr_1fr_1fr_auto] sm:items-end"
              noValidate
              onSubmit={(e) => {
                e.preventDefault()
                void handleCreateStream()
              }}
            >
              <Input
                label={t("ingest.customStreams.nameLabel")}
                placeholder={t("ingest.customStreams.namePlaceholder")}
                helperText={t("ingest.customStreams.nameHelper")}
                value={streamName}
                onChange={(e) => setStreamName(e.target.value)}
                aria-label={t("ingest.customStreams.nameLabel")}
                data-testid="custom-stream-name"
              />
              <Select
                label={t("ingest.customStreams.classLabel")}
                options={OCSF_CLASS_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
                value={streamClass}
                onChange={(v) => setStreamClass(Number(v))}
                data-testid="custom-stream-class"
              />
              <Input
                label={t("ingest.customStreams.descriptionLabel")}
                value={streamDescription}
                onChange={(e) => setStreamDescription(e.target.value)}
                aria-label={t("ingest.customStreams.descriptionLabel")}
              />
              <Button type="submit" size="sm" loading={creating} disabled={!streamNameValid} data-testid="custom-stream-create">
                <PlusIcon size={14} /> {t("ingest.customStreams.create")}
              </Button>
            </form>
          )}
          {streamNotice && <p className="text-xs text-text-secondary">{streamNotice}</p>}
          {streamError && <p className="text-xs text-danger-600">{streamError}</p>}
        </div>
      )}

      {/* Snippet do edge-collector */}
      <div className="space-y-1.5">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-text-secondary">{isCustom ? t("ingest.edgeCollectorConfigGeneric") : t("ingest.edgeCollectorConfig")}</span>
          <CopyButton text={snippet} />
        </div>
        <pre className="max-h-72 overflow-auto rounded-md border border-border bg-surface-tertiary p-3 text-[11px] leading-relaxed text-text">
{snippet}
        </pre>
      </div>
    </Card>
  )
}
