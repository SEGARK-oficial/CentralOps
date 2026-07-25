/**
 * PayloadPanel
 * Painel esquerdo do editor de mappings. Dois modos via Tabs:
 *   - "reservoir": amostras reais de eventos brutos do tenant, vindas do ring buffer
 *     no Redis que o pipeline alimenta a cada evento coletado
 *   - "manual": textarea para colar JSON raw e alimentar o dry-run
 *
 * A aba reservoir foi um placeholder estático por um release inteiro: o endpoint
 * `GET /mappings/samples` e o `EmptyState` "amostras indisponíveis" entraram no MESMO
 * commit, e só o segundo foi ligado. O painel nunca chamou API nenhuma, então a tela
 * anunciava indisponibilidade de uma coisa que existia e funcionava.
 */

import type React from "react"
import { useState, useId, useEffect, useCallback } from "react"
import { DatabaseIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"
import { Tabs, TabsList, TabsTrigger, TabsPanel } from "@/components/ui/Tabs/Tabs"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import { Notice } from "@/components/ui/Notice/Notice"
import { Button } from "@/components/ui/Button/Button"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { JsonViewer } from "@/components/shared/JsonViewer"
import { getMappingSamples } from "@/services/api"

type PayloadMode = "reservoir" | "manual"

interface PayloadPanelProps {
  /** Chamada quando o usuário escolhe uma amostra ou cola JSON válido */
  onRawEventChange: (event: Record<string, unknown> | null) => void
  /** Coordenadas do reservoir: ele é indexado por vendor + tipo de evento. */
  vendor?: string
  eventType?: string
  /** Tenant cujas amostras exibir. Admin global precisa nomear um. */
  orgId?: number | null
  /**
   * O reservoir é por tenant, e um admin GLOBAL com o filtro em "todas as
   * organizações" não tem tenant para consultar: o backend resolve org=None e
   * devolve vazio sem tocar no Redis. Sem este sinal, a tela diria "ainda não há
   * amostras" — falso sobre dados que existem, só que em outro escopo. Vem como
   * prop e não de `useAuth` para o painel continuar apresentacional (e testável
   * sem provider de autenticação).
   */
  needsOrgChoice?: boolean
  className?: string
}

export const PayloadPanel: React.FC<PayloadPanelProps> = ({
  onRawEventChange,
  vendor,
  eventType,
  orgId,
  needsOrgChoice = false,
  className,
}) => {
  const { t } = useTranslation("mappings")
  const [mode, setMode] = useState<PayloadMode>("reservoir")
  const [rawText, setRawText] = useState("")
  const [parseError, setParseError] = useState<string | null>(null)
  const [parsedJson, setParsedJson] = useState<Record<string, unknown> | null>(null)

  // ── Reservoir ──────────────────────────────────────────────────────────────
  const [samples, setSamples] = useState<Record<string, unknown>[]>([])
  const [samplesLoading, setSamplesLoading] = useState(false)
  const [samplesError, setSamplesError] = useState<string | null>(null)
  const [selectedSample, setSelectedSample] = useState<number | null>(null)
  const [reloadToken, setReloadToken] = useState(0)

  const canQueryReservoir = Boolean(vendor && eventType) && !needsOrgChoice

  useEffect(() => {
    if (!canQueryReservoir) return
    const controller = new AbortController()
    setSamplesLoading(true)
    setSamplesError(null)
    getMappingSamples(
      { vendor: vendor as string, event_type: eventType as string, limit: 10, org_id: orgId },
      { signal: controller.signal },
    )
      .then((res) => {
        setSamples(res.items ?? [])
        setSelectedSample(null)
      })
      .catch((err: unknown) => {
        // AbortError é troca de mapping/desmonte, não falha: mostrar erro aqui
        // faria a tela piscar vermelho a cada navegação.
        if (err instanceof DOMException && err.name === "AbortError") return
        setSamplesError(err instanceof Error ? err.message : t("payloadPanel.reservoir.error"))
      })
      .finally(() => {
        if (!controller.signal.aborted) setSamplesLoading(false)
      })
    return () => controller.abort()
  }, [canQueryReservoir, vendor, eventType, orgId, reloadToken, t])

  const handleSelectSample = useCallback(
    (index: number) => {
      const picked = samples[index]
      if (!picked) return
      setSelectedSample(index)
      onRawEventChange(picked)
    },
    [samples, onRawEventChange],
  )

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
          {needsOrgChoice ? (
            <EmptyState
              icon={<DatabaseIcon size={32} />}
              title={t("payloadPanel.reservoir.pickOrgTitle")}
              description={t("payloadPanel.reservoir.pickOrgDescription")}
            />
          ) : samplesLoading ? (
            <div className="flex justify-center py-8">
              <LoadingSpinner />
            </div>
          ) : samplesError ? (
            <div className="flex flex-col gap-3">
              <Notice variant="danger">{samplesError}</Notice>
              <Button variant="secondary" size="sm" onClick={() => setReloadToken((n) => n + 1)}>
                {t("payloadPanel.reservoir.retry")}
              </Button>
            </div>
          ) : samples.length === 0 ? (
            /* Vazio não é falha: o reservoir só enche com tráfego real. A copy diz o
               que fazer enquanto isso, em vez de anunciar indisponibilidade. */
            <EmptyState
              icon={<DatabaseIcon size={32} />}
              title={t("payloadPanel.reservoir.title")}
              description={t("payloadPanel.reservoir.description")}
            />
          ) : (
            <ul className="flex flex-col gap-2" data-testid="reservoir-samples">
              {samples.map((sample, index) => (
                <li key={index}>
                  <button
                    type="button"
                    onClick={() => handleSelectSample(index)}
                    aria-pressed={selectedSample === index}
                    className={cn(
                      "w-full rounded-md border p-2 text-left transition-colors focus-ring",
                      selectedSample === index
                        ? "border-primary-500 bg-surface-tertiary"
                        : "border-border hover:border-border-hover",
                    )}
                  >
                    <span className="mb-1 block font-mono text-xs text-text-tertiary">
                      {t("payloadPanel.reservoir.sampleIndex", { index: index + 1 })}
                    </span>
                    {/* collapseLevel=1: a lista é para ESCOLHER uma amostra, não para
                        ler o evento inteiro. O JSON completo aparece no dry-run. */}
                    <JsonViewer data={sample} collapseLevel={1} />
                  </button>
                </li>
              ))}
            </ul>
          )}
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
