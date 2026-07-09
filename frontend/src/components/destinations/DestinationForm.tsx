import type React from "react"
import { useEffect, useMemo, useState } from "react"
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
  const [catalog, setCatalog] = useState<DestinationType[]>([])
  const [catalogError, setCatalogError] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)

  const [name, setName] = useState(destination?.name ?? "")
  const [kind, setKind] = useState(destination?.kind ?? initialKind ?? "")
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
        if (!cancelled) setCatalogError(err instanceof Error ? err.message : "Falha ao carregar catálogo.")
      })
    return () => {
      cancelled = true
    }
  }, [])

  const selectedType = useMemo(() => catalog.find((t) => t.kind === kind), [catalog, kind])
  // campo de credencial DATA-DRIVEN — aparece quando o kind declara
  // QUALQUER ``required_secrets`` (não só "hec_token"), de modo que Elastic
  // (api_key), OTLP (bearer) etc. sejam configuráveis pela mesma UI. O backend
  // cifra o valor para ``secret_ref`` independentemente do kind.
  const requiredSecrets = selectedType?.required_secrets ?? []
  const requiresSecret = requiredSecrets.length > 0
  const secretLabelName = requiredSecrets[0] ?? "credencial"

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitError(null)
    if (!name.trim()) {
      setSubmitError("Informe um nome para o destino.")
      return
    }
    if (mode === "create" && !kind) {
      setSubmitError("Selecione o tipo de destino.")
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
      setSubmitError(err instanceof Error ? err.message : "Falha ao salvar destino.")
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-5">
      {catalogError && (
        <Notice variant="danger" title="Catálogo indisponível">
          {catalogError}
        </Notice>
      )}
      {submitError && (
        <Notice variant="danger" title="Não foi possível salvar">
          {submitError}
        </Notice>
      )}

      <Input
        label="Nome *"
        value={name}
        onChange={(e) => setName(e.target.value)}
        required
        placeholder="ex.: Splunk SOC produção"
        disabled={loading}
      />

      {/* Em modo create o kind vem pré-selecionado pela galeria — exibimos read-only. */}
      <Input
        label="Tipo de destino"
        value={selectedType?.label ?? kind}
        disabled
        readOnly
        data-testid="destination-form-kind"
      />

      {requiresSecret && (
        <Input
          label={
            mode === "create"
              ? `Credencial — ${secretLabelName} *`
              : `Nova credencial (${secretLabelName}) — deixe vazio para manter`
          }
          type="password"
          value={hecToken}
          onChange={(e) => setHecToken(e.target.value)}
          placeholder={destination?.has_secret ? "•••••••• (configurada)" : "Valor da credencial"}
          helperText="Cifrada no cofre — nunca exibida após salvar."
          disabled={loading}
          autoComplete="new-password"
        />
      )}

      {selectedType && (
        <>
          <fieldset className="space-y-3 rounded-lg border border-border p-4">
            <legend className="px-1 text-sm font-semibold text-text">Configuração</legend>
            <JsonSchemaForm
              schema={selectedType.config_schema}
              values={config}
              onChange={setConfig}
              disabled={loading}
              idPrefix="cfg"
            />
          </fieldset>

          <fieldset className="space-y-3 rounded-lg border border-border p-4">
            <legend className="px-1 text-sm font-semibold text-text">Entrega</legend>
            <p className="text-xs text-text-tertiary">
              Política de entrega (concorrência, controle de vazão, pré-visualização). Limites avançados
              (circuit breaker, lote) usam os defaults do tipo.
            </p>
            <JsonSchemaForm
              schema={selectedType.delivery_schema}
              values={delivery}
              onChange={setDelivery}
              disabled={loading}
              idPrefix="dlv"
            />
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
        <span>Habilitado</span>
      </label>

      <div className="flex justify-end gap-3 pt-2">
        <Button type="button" variant="outline" onClick={onCancel} disabled={loading}>
          Cancelar
        </Button>
        <Button type="submit" loading={loading}>
          {mode === "create" ? "Criar destino" : "Salvar alterações"}
        </Button>
      </div>
    </form>
  )
}
