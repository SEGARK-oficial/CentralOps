import type React from "react"
import { useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import * as api from "@/services/api"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Notice } from "@/components/ui/Notice/Notice"
import { JsonSchemaForm } from "./JsonSchemaForm"
import type {
  Destination,
  DestinationCreateRequest,
  DestinationType,
  DestinationUpdateRequest,
} from "@/types"

interface DestinationFormProps {
  mode: "create" | "edit"
  destination?: Destination | null
  /**
   * Em modo `create`, pré-seleciona o kind vindo da galeria.
   * O campo fica read-only — a galeria é quem gerencia a seleção de tipo.
   */
  initialKind?: string
  loading?: boolean
  onCancel: () => void
  onSubmit: (payload: DestinationCreateRequest | DestinationUpdateRequest) => Promise<void>
}

export const DestinationForm: React.FC<DestinationFormProps> = ({
  mode,
  destination,
  initialKind,
  loading,
  onCancel,
  onSubmit,
}) => {
  const { t } = useTranslation("destinations")
  const [catalog, setCatalog] = useState<DestinationType[]>([])
  const [catalogError, setCatalogError] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)

  const [name, setName] = useState(destination?.name ?? "")
  // TS: `kind` é fixado na criação (escolhido no DestinationTypeGallery antes
  // deste form) — não há UI de troca aqui, então não existe setter.
  const [kind] = useState(destination?.kind ?? initialKind ?? "")
  const [enabled, setEnabled] = useState(destination?.enabled ?? true)
  const [config, setConfig] = useState<Record<string, unknown>>(destination?.config ?? {})
  const [delivery, setDelivery] = useState<Record<string, unknown>>(destination?.delivery ?? {})
  const [hecToken, setHecToken] = useState("")

  useEffect(() => {
    let cancelled = false
    api
      .listDestinationTypes()
      .then((types) => {
        if (!cancelled) setCatalog(types)
      })
      .catch((err) => {
        if (!cancelled) setCatalogError(err instanceof Error ? err.message : t("form.catalogErrorFallback"))
      })
    return () => {
      cancelled = true
    }
    // Carga do catálogo é ÚNICA (uma vez por montagem) — só usa `t` no
    // fallback de erro, e re-executar a busca a cada troca de idioma não
    // vale o custo de rede.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const selectedType = useMemo(() => catalog.find((t) => t.kind === kind), [catalog, kind])

  // FinOps (delivery.cost) — objeto aninhado que o JsonSchemaForm não cobre.
  const cost = (delivery.cost ?? {}) as Record<string, unknown>
  const costPerGb = typeof cost.cost_per_gb === "number" ? cost.cost_per_gb : 0
  const currency = typeof cost.currency === "string" ? cost.currency : "USD"
  const setCostField = (field: "cost_per_gb" | "currency", value: number | string) => {
    setDelivery((prev) => ({
      ...prev,
      cost: { ...((prev.cost ?? {}) as Record<string, unknown>), [field]: value },
    }))
  }
  // Campo de credencial DATA-DRIVEN: aparece quando o kind declara QUALQUER
  // segredo, obrigatório OU opcional. O backend cifra o valor em ``secret_ref``
  // independentemente do kind.
  //
  // O ramo "opcional" existe por um defeito concreto: o webhook genérico aceita
  // Bearer e Basic no runtime, mas declarava a lista de segredos vazia. Como a
  // condição olhava só os obrigatórios, o input nunca era desenhado e não havia
  // NENHUM lugar na tela para colar o token. Quem escolhia Bearer salvava um
  // destino que respondia 401 sem explicação.
  const requiredSecrets = selectedType?.required_secrets ?? []
  const optionalSecrets = selectedType?.optional_secrets ?? []
  const secretIsRequired = requiredSecrets.length > 0
  const acceptsSecret = secretIsRequired || optionalSecrets.length > 0
  const secretLabelName = requiredSecrets[0] ?? optionalSecrets[0] ?? t("form.credentialFallbackName")

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitError(null)
    if (!name.trim()) {
      setSubmitError(t("form.nameRequiredError"))
      return
    }
    if (mode === "create" && !kind) {
      setSubmitError(t("form.kindRequiredError"))
      return
    }
    try {
      if (mode === "create") {
        const payload: DestinationCreateRequest = {
          name: name.trim(),
          kind,
          config,
          delivery,
          enabled,
          ...(hecToken ? { hec_token: hecToken } : {}),
        }
        await onSubmit(payload)
      } else {
        const payload: DestinationUpdateRequest = {
          name: name.trim(),
          config,
          delivery,
          enabled,
          ...(hecToken ? { hec_token: hecToken } : {}),
        }
        await onSubmit(payload)
      }
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : t("form.submitErrorFallback"))
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-5">
      {catalogError && (
        <Notice variant="danger" title={t("form.catalogErrorTitle")}>
          {catalogError}
        </Notice>
      )}
      {submitError && (
        // R2-8.2: reação direta ao clique em "Salvar"/"Criar destino" — mantém
        // assertive explícito (o padrão do Notice virou polite).
        <Notice variant="danger" title={t("form.submitErrorTitle")} live="assertive">
          {submitError}
        </Notice>
      )}

      <Input
        label={t("form.nameLabel")}
        value={name}
        onChange={(e) => setName(e.target.value)}
        required
        placeholder={t("form.namePlaceholder")}
        disabled={loading}
      />

      {/* Em modo create o kind vem pré-selecionado pela galeria — exibimos read-only. */}
      <Input
        label={t("form.kindLabel")}
        value={selectedType?.label ?? kind}
        disabled
        readOnly
        data-testid="destination-form-kind"
      />

      {acceptsSecret && (
        <Input
          label={
            mode === "create"
              ? t("form.credentialLabelCreate", { name: secretLabelName }) + (secretIsRequired ? " *" : "")
              : t("form.credentialLabelEdit", { name: secretLabelName })
          }
          type="password"
          value={hecToken}
          onChange={(e) => setHecToken(e.target.value)}
          placeholder={destination?.has_secret ? t("form.credentialPlaceholderConfigured") : t("form.credentialPlaceholderEmpty")}
          helperText={secretIsRequired ? t("form.credentialHelperRequired") : t("form.credentialHelperOptional")}
          disabled={loading}
          autoComplete="new-password"
        />
      )}

      {selectedType && (
        <>
          <fieldset className="space-y-3 rounded-lg border border-border p-4">
            <legend className="px-1 text-sm font-semibold text-text">{t("form.configLegend")}</legend>
            <JsonSchemaForm
              schema={selectedType.config_schema}
              values={config}
              onChange={setConfig}
              disabled={loading}
              idPrefix="cfg"
            />
          </fieldset>

          <fieldset className="space-y-3 rounded-lg border border-border p-4">
            <legend className="px-1 text-sm font-semibold text-text">{t("form.deliveryLegend")}</legend>
            <p className="text-xs text-text-tertiary">{t("form.deliveryHelp")}</p>
            <JsonSchemaForm
              schema={selectedType.delivery_schema}
              values={delivery}
              onChange={setDelivery}
              disabled={loading}
              idPrefix="dlv"
            />
          </fieldset>

          {/* FinOps: o JsonSchemaForm só renderiza escalares de 1º nível e pula
              o objeto aninhado `cost`, então cost_per_gb ficava inatingível pela
              UI — o único caminho era um PATCH manual no JSON. Sem preço, o pricer
              EE devolve US$ 0 e o card "Economia estimada" fica em zero sem
              explicação. A config do preço é Community; só a tradução em US$ é EE. */}
          <fieldset className="space-y-3 rounded-lg border border-border p-4">
            <legend className="px-1 text-sm font-semibold text-text">{t("form.costLegend")}</legend>
            <p className="text-xs text-text-tertiary">{t("form.costHelp")}</p>
            <div className="flex flex-wrap gap-3">
              <label className="flex flex-col gap-1 text-sm text-text">
                <span className="text-xs text-text-secondary">{t("form.costPerGbLabel")}</span>
                <input
                  type="number"
                  min={0}
                  step="0.01"
                  className="h-9 w-40 rounded border border-border-field bg-surface-tertiary px-2 text-sm text-text transition-colors hover:border-border-field-hover focus-ring"
                  value={costPerGb}
                  onChange={(e) => setCostField("cost_per_gb", e.target.value === "" ? 0 : Number(e.target.value))}
                  disabled={loading}
                  aria-label={t("form.costPerGbLabel")}
                />
              </label>
              <label className="flex flex-col gap-1 text-sm text-text">
                <span className="text-xs text-text-secondary">{t("form.currencyLabel")}</span>
                <input
                  type="text"
                  maxLength={3}
                  className="h-9 w-24 rounded border border-border-field bg-surface-tertiary px-2 text-sm uppercase text-text transition-colors hover:border-border-field-hover focus-ring"
                  value={currency}
                  onChange={(e) => setCostField("currency", e.target.value.toUpperCase())}
                  disabled={loading}
                  aria-label={t("form.currencyLabel")}
                />
              </label>
            </div>
          </fieldset>
        </>
      )}

      <label className="flex items-center gap-2 text-sm text-text">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => setEnabled(e.target.checked)}
          className="h-4 w-4 rounded border-border"
          disabled={loading}
        />
        <span>{t("form.enabledLabel")}</span>
      </label>

      <div className="flex justify-end gap-3 pt-2">
        <Button type="button" variant="outline" onClick={onCancel} disabled={loading}>
          {t("form.cancel")}
        </Button>
        <Button type="submit" loading={loading}>
          {mode === "create" ? t("form.createSubmit") : t("form.editSubmit")}
        </Button>
      </div>
    </form>
  )
}
