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
import { CopyIcon, CheckIcon, KeyRoundIcon, RadioTowerIcon, ShieldXIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import * as api from "@/services/api"
import type { IngestInfo } from "@/services/api"
import { Card } from "@/components/ui/Card/Card"
import { Button } from "@/components/ui/Button/Button"
import { Badge } from "@/components/ui/Badge/Badge"
import { Notice } from "@/components/ui/Notice/Notice"
import { brandIconFor } from "@/lib/brand-icons"

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

  const origin = typeof window !== "undefined" ? window.location.origin : ""
  const primaryStream = info?.streams?.[0] ?? "events"
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

      {/* Snippet do edge-collector */}
      <div className="space-y-1.5">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-text-secondary">{t("ingest.edgeCollectorConfig")}</span>
          <CopyButton text={snippet} />
        </div>
        <pre className="max-h-72 overflow-auto rounded-md border border-border bg-surface-tertiary p-3 text-[11px] leading-relaxed text-text">
{snippet}
        </pre>
      </div>
    </Card>
  )
}
