"use client"

import type React from "react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { CheckIcon, CopyIcon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Checkbox } from "@/components/ui/Checkbox/Checkbox"
import { Notice } from "@/components/ui/Notice/Notice"
import { Select } from "@/components/ui/Select/Select"
import { claudeCodeAddCommand, mcpEndpointUrl, mcpServersSnippet } from "@/lib/mcpSnippet"
import type { McpConfig, McpResponseMode, UpdateMcpConfigRequest } from "@/types"

type Feedback = { type: "success" | "error"; message: string } | null

interface Props {
  config: McpConfig | null
  loading: boolean
  saving: boolean
  feedback: Feedback
  onSave: (data: UpdateMcpConfigRequest) => Promise<boolean>
}

interface FormState {
  enabled: boolean
  response_mode: McpResponseMode
}

function toForm(c: McpConfig | null): FormState {
  return {
    enabled: c?.enabled ?? false,
    response_mode: c?.response_mode ?? "json",
  }
}

export const McpConfigForm: React.FC<Props> = ({ config, loading, saving, feedback, onSave }) => {
  const { t } = useTranslation("config")
  const [form, setForm] = useState<FormState>(() => toForm(config))
  const [copied, setCopied] = useState<"json" | "cli" | null>(null)

  useEffect(() => {
    setForm(toForm(config))
  }, [config])

  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(null), 2000)
    return () => clearTimeout(timer)
  }, [copied])

  const endpointPath = config?.endpoint_path ?? "/api/mcp"
  const snippet = mcpServersSnippet(endpointPath)
  const cli = claudeCodeAddCommand(endpointPath)

  const copy = async (kind: "json" | "cli", text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(kind)
    } catch {
      /* clipboard indisponível (http, iframe): o texto continua selecionável */
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    await onSave({ enabled: form.enabled, response_mode: form.response_mode })
  }

  const dirty = form.enabled !== (config?.enabled ?? false) || form.response_mode !== (config?.response_mode ?? "json")

  return (
    <form onSubmit={handleSubmit} className="space-y-6" data-testid="mcp-config-form">
      {feedback && (
        <Notice variant={feedback.type === "success" ? "success" : "danger"}>{feedback.message}</Notice>
      )}

      {/* ── Estado atual ── */}
      <dl className="grid gap-4 sm:grid-cols-3">
        <div>
          <dt className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{t("mcp.stats.status")}</dt>
          <dd className="mt-1">
            <Badge variant={config?.enabled ? "success" : "outline"} data-testid="mcp-status-badge">
              {config?.enabled ? t("mcp.stats.enabled") : t("mcp.stats.disabled")}
            </Badge>
          </dd>
        </div>
        <div>
          <dt className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{t("mcp.stats.tools")}</dt>
          <dd className="mt-1 text-sm font-semibold text-text" data-testid="mcp-tools-count">{config?.tools_count ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{t("mcp.stats.endpoint")}</dt>
          <dd className="mt-1 break-all font-mono text-xs text-text" data-testid="mcp-endpoint">{mcpEndpointUrl(endpointPath)}</dd>
        </div>
      </dl>

      {/* ── Toggle + modo ── */}
      <div className="space-y-4">
        <Checkbox
          id="mcp-enabled"
          name="enabled"
          label={t("mcp.enable")}
          description={t("mcp.enableDescription")}
          checked={form.enabled}
          disabled={loading || saving}
          onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))}
        />
        <Select
          id="mcp-response-mode"
          name="response_mode"
          label={t("mcp.responseMode")}
          helperText={t("mcp.responseModeHelp")}
          disabled={loading || saving}
          value={form.response_mode}
          onValueChange={(v) => setForm((f) => ({ ...f, response_mode: (v as McpResponseMode) ?? "json" }))}
          options={[
            { value: "json", label: t("mcp.modeJson") },
            { value: "sse", label: t("mcp.modeSse") },
          ]}
        />
      </div>

      {/* ── Como funciona ── */}
      <section className="rounded-lg border border-border bg-surface-subtle p-4">
        <h3 className="text-sm font-semibold text-text">{t("mcp.howItWorks.title")}</h3>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-text-secondary">
          <li>{t("mcp.howItWorks.identity")}</li>
          <li>{t("mcp.howItWorks.permission")}</li>
          <li>{t("mcp.howItWorks.contract")}</li>
          <li>{t("mcp.howItWorks.audit")}</li>
          <li>{t("mcp.howItWorks.revoke")}</li>
        </ul>
      </section>

      {/* ── Snippet do cliente ── */}
      <section className="space-y-2">
        <h3 className="text-sm font-semibold text-text">{t("mcp.clientConfig.title")}</h3>
        <p className="text-xs text-text-secondary">{t("mcp.clientConfig.description")}</p>
        {/* Botão de copiar numa linha própria, nunca sobreposto ao <pre>: o
            comando de uma linha só ficava escondido atrás dele. */}
        <div className="space-y-1">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs font-medium text-text-secondary">{t("mcp.clientConfig.jsonLabel")}</span>
            <Button
              type="button"
              variant="outline"
              size="xs"
              leftIcon={copied === "json" ? <CheckIcon size={12} /> : <CopyIcon size={12} />}
              onClick={() => void copy("json", snippet)}
            >
              {copied === "json" ? t("mcp.clientConfig.copied") : t("mcp.clientConfig.copy")}
            </Button>
          </div>
          <pre className="overflow-x-auto rounded-lg border border-border bg-surface-subtle p-3 font-mono text-xs text-text" data-testid="mcp-snippet-json">{snippet}</pre>
        </div>
        <div className="space-y-1">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs font-medium text-text-secondary">{t("mcp.clientConfig.cliLabel")}</span>
            <Button
              type="button"
              variant="outline"
              size="xs"
              leftIcon={copied === "cli" ? <CheckIcon size={12} /> : <CopyIcon size={12} />}
              onClick={() => void copy("cli", cli)}
            >
              {copied === "cli" ? t("mcp.clientConfig.copied") : t("mcp.clientConfig.copy")}
            </Button>
          </div>
          <pre className="overflow-x-auto rounded-lg border border-border bg-surface-subtle p-3 font-mono text-xs text-text" data-testid="mcp-snippet-cli">{cli}</pre>
        </div>
      </section>

      <div className="flex justify-end">
        <Button type="submit" loading={saving} disabled={loading || saving || !dirty}>
          {t("mcp.saveConfig")}
        </Button>
      </div>
    </form>
  )
}
