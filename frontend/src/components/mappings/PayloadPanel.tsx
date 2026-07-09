/**
 * PayloadPanel
 * Painel esquerdo do editor de mappings.
 * Sprint 1: suporte a dois modos via Tabs:
 *   - "reservoir": placeholder informativo (endpoint de samples ainda não existe no backend)
 *   - "manual": textarea para colar JSON raw e alimentar o dry-run
 */

import type React from "react"
import { useState, useId } from "react"
import { DatabaseIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"
import { Tabs, TabsList, TabsTrigger, TabsPanel } from "@/components/ui/Tabs/Tabs"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import { Notice } from "@/components/ui/Notice/Notice"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { JsonViewer } from "@/components/shared/JsonViewer"

type PayloadMode = "reservoir" | "manual"

interface PayloadPanelProps {
  /** Chamada quando o usuário cola JSON válido no modo manual */
  onRawEventChange: (event: Record<string, unknown> | null) => void
  className?: string
}

export const PayloadPanel: React.FC<PayloadPanelProps> = ({
  onRawEventChange,
  className,
}) => {
  const { t } = useTranslation("mappings")
  const [mode, setMode] = useState<PayloadMode>("reservoir")
  const [rawText, setRawText] = useState("")
  const [parseError, setParseError] = useState<string | null>(null)
  const [parsedJson, setParsedJson] = useState<Record<string, unknown> | null>(null)

  const headingId = useId()

  const handleTextChange = (value: string) => {
    setRawText(value)
    if (!value.trim()) {
      setParseError(null)
      setParsedJson(null)
      onRawEventChange(null)
      return
    }
    try {
      const parsed = JSON.parse(value) as unknown
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        setParseError(t("payloadPanel.errors.notObject"))
        setParsedJson(null)
        onRawEventChange(null)
        return
      }
      setParseError(null)
      const typedParsed = parsed as Record<string, unknown>
      setParsedJson(typedParsed)
      onRawEventChange(typedParsed)
    } catch {
      setParseError(t("payloadPanel.errors.invalidJson"))
      setParsedJson(null)
      onRawEventChange(null)
    }
  }

  return (
    <section
      role="region"
      aria-labelledby={headingId}
      data-testid="payload-panel"
      className={cn(
        "flex flex-col gap-3 rounded-lg border border-border bg-surface p-4 min-h-0",
        className,
      )}
    >
      <h2
        id={headingId}
        className="text-sm font-semibold text-text"
      >
        {t("payloadPanel.heading")}
      </h2>

      <Tabs value={mode} onValueChange={(v) => setMode(v as PayloadMode)}>
        <TabsList ariaLabel={t("payloadPanel.modeAriaLabel")}>
          <TabsTrigger value="reservoir">{t("payloadPanel.tabs.reservoir")}</TabsTrigger>
          <TabsTrigger value="manual">{t("payloadPanel.tabs.manual")}</TabsTrigger>
        </TabsList>

        <TabsPanel value="reservoir">
          <EmptyState
            icon={<DatabaseIcon size={32} />}
            title={t("payloadPanel.reservoir.title")}
            description={t("payloadPanel.reservoir.description")}
          />
        </TabsPanel>

        <TabsPanel value="manual">
          <div className="flex flex-col gap-3">
            <Textarea
              data-testid="payload-manual-input"
              label={t("payloadPanel.manual.inputLabel")}
              placeholder='{ "action": "login", "user": "joao" }'
              value={rawText}
              rows={10}
              error={parseError ?? undefined}
              onChange={(e) => handleTextChange(e.target.value)}
              aria-label={t("payloadPanel.manual.inputAriaLabel")}
            />

            {parsedJson !== null && !parseError && (
              <div className="rounded-md border border-border bg-surface-secondary p-3 overflow-auto max-h-64">
                <p className="text-xs font-medium text-text-secondary mb-2">
                  {t("payloadPanel.manual.previewLabel")}
                </p>
                <JsonViewer data={parsedJson} collapseLevel={2} />
              </div>
            )}

            {!rawText && (
              <Notice variant="info">
                {t("payloadPanel.manual.hint")}
              </Notice>
            )}
          </div>
        </TabsPanel>
      </Tabs>
    </section>
  )
}

export default PayloadPanel
