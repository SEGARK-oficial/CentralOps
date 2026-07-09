import type React from "react"
import { useEffect, useMemo, useState } from "react"
import { Trans, useTranslation } from "react-i18next"
import type {
  AuthFieldRead,
  CreateIntegrationRequest,
  Integration,
  Organization,
  ProviderPlatformRead,
  PlatformType,
  UpdateIntegrationRequest,
} from "@/types"
import * as api from "@/services/api"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { TileGallery, type Tile } from "@/components/shared/TileGallery"
import { Notice } from "@/components/ui/Notice/Notice"
import { brandIconFor } from "@/lib/brand-icons"

// Fonte única da verdade do ícone: o catálogo do backend (PlatformRegistration de
// cada vendor) já declara `icon_id` como SLUG DE MARCA (ex.: "wazuh",
// "crowdstrike", "microsoft"). Não há mais mapa platform→marca duplicado aqui —
// adicionar/ajustar um vendor é só no backend. Sem icon_id (fallback estático
// sophos/wazuh) usamos o próprio platform como slug; brandIconFor faz o fallback
// final para um glifo genérico.
function platformIcon(platform: string, iconId?: string | null, size = 28): React.ReactNode {
  return brandIconFor(iconId ?? platform, { size })
}

// Catálogo de plataformas é 100% plugin-driven: display_name/description/category/
// icon vêm do backend (PlatformRegistration de cada vendor, via GET /providers/
// platforms). Adicionar um vendor NÃO toca esta tela — ele aparece e é agrupado
// pela `category` automaticamente na TileGallery.

type IntegrationFormMode = "create" | "edit"

interface IntegrationFormProps {
  mode: IntegrationFormMode
  organizations?: Organization[]
  integration?: Integration | null
  loading?: boolean
  onCancel?: () => void
  onSubmit: (payload: CreateIntegrationRequest | UpdateIntegrationRequest) => Promise<void>
}

const selectCls =
  "h-9 w-full rounded-md border border-border bg-surface px-3 text-sm text-text transition-colors focus:outline-none focus:border-primary-500 focus:ring-2 focus:ring-primary-500/20 disabled:cursor-not-allowed disabled:opacity-50"

// Platforms with custom form blocks — these bypass the generic DynamicAuthField renderer
const CUSTOM_BLOCK_PLATFORMS = new Set<string>(["sophos", "wazuh"])

function formatScheduleSeconds(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  const mins = Math.floor(seconds / 60)
  if (mins < 60) return `${mins} min`
  const hours = Math.floor(mins / 60)
  return `${hours}h`
}

interface DynamicAuthFieldProps {
  field: AuthFieldRead
  value: string | boolean
  onChange: (val: string | boolean) => void
  disabled?: boolean
}

const DynamicAuthField: React.FC<DynamicAuthFieldProps> = ({ field, value, onChange, disabled }) => {
  const { t } = useTranslation("integrations")
  const inputId = `auth-field-${field.key}`
  const helperId = field.help_text ? `${inputId}-helper` : undefined

  if (field.type === "bool") {
    return (
      <div className="flex flex-col gap-1.5">
        <label className="flex items-center gap-2 text-sm font-medium text-text" htmlFor={inputId}>
          <input
            id={inputId}
            type="checkbox"
            checked={Boolean(value)}
            onChange={(e) => onChange(e.target.checked)}
            disabled={disabled}
            aria-describedby={helperId}
          />
          {field.label}
          {field.required && <span className="text-danger-500" aria-label={t("form.dynamicField.required")}>*</span>}
        </label>
        {field.help_text && (
          <p id={helperId} className="text-xs text-text-tertiary">{field.help_text}</p>
        )}
      </div>
    )
  }

  if (field.type === "select" && field.options) {
    return (
      <div className="flex flex-col gap-1.5">
        <label className="text-sm font-medium text-text" htmlFor={inputId}>
          {field.label}
          {field.required && <span className="ml-1 text-danger-500" aria-label={t("form.dynamicField.required")}>*</span>}
        </label>
        <select
          id={inputId}
          className={selectCls}
          value={String(value)}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
          required={field.required}
          aria-describedby={helperId}
        >
          <option value="">{t("form.dynamicField.selectPlaceholder")}</option>
          {field.options.map((opt) => (
            <option key={opt} value={opt}>{opt}</option>
          ))}
        </select>
        {field.help_text && (
          <p id={helperId} className="text-xs text-text-tertiary">{field.help_text}</p>
        )}
      </div>
    )
  }

  const inputType =
    field.type === "secret" ? "password" :
    field.type === "url" ? "url" :
    "text"

  return (
    <Input
      id={inputId}
      label={field.label}
      type={inputType}
      value={String(value)}
      onChange={(e) => onChange(e.target.value)}
      required={field.required}
      disabled={disabled}
      helperText={field.help_text ?? undefined}
      autoComplete={field.type === "secret" ? "off" : undefined}
    />
  )
}

const normalizeOptional = (value: string) => {
  const normalized = value.trim()
  return normalized.length > 0 ? normalized : null
}

export const IntegrationForm: React.FC<IntegrationFormProps> = ({
  mode,
  organizations = [],
  integration,
  loading = false,
  onCancel,
  onSubmit,
}) => {
  const { t } = useTranslation("integrations")
  const [organizationId, setOrganizationId] = useState<number | "">("")
  const [name, setName] = useState("")
  const [platform, setPlatform] = useState<PlatformType>("sophos")
  const [isActive, setIsActive] = useState(true)

  const [clientId, setClientId] = useState("")
  const [clientSecret, setClientSecret] = useState("")
  const [region, setRegion] = useState("")
  // Sophos: o card base "sophos" é sempre TENANT único. Partner/Organization são
  // tiles próprios (sophos_partner/sophos_organization) — o backend deriva
  // base_platform + kind no create. Por isso não há mais toggle de
  // "tipo de conta" aqui.

  // Wazuh: o INDEXER é a fonte (alertas/detecções/states em wazuh-alerts-*) e é
  // sempre obrigatório. O MANAGER é opcional (saúde do servidor + inventário de
  // agentes; não coleta nada) e fica atrás de `managerEnabled`.
  const [managerEnabled, setManagerEnabled] = useState(false)
  const [managerUrl, setManagerUrl] = useState("")
  const [managerApiUsername, setManagerApiUsername] = useState("")
  const [managerApiPassword, setManagerApiPassword] = useState("")
  const [indexerUrl, setIndexerUrl] = useState("")
  const [indexerUsername, setIndexerUsername] = useState("")
  const [indexerPassword, setIndexerPassword] = useState("")
  const [verifySsl, setVerifySsl] = useState(true)
  const [validationError, setValidationError] = useState<string | null>(null)

  // ── Provider platform catalog ────────────────────────────────────
  const [providerPlatforms, setProviderPlatforms] = useState<ProviderPlatformRead[]>([])
  // Dynamic field values for platforms NOT handled by custom blocks (e.g. ninjaone, microsoft_defender)
  const [dynamicFieldValues, setDynamicFieldValues] = useState<Record<string, string | boolean>>({})
  // Teste de conexão pré-save (stateless): valida as credenciais digitadas.
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string } | null>(null)

  useEffect(() => {
    api.getProviderPlatforms()
      .then(setProviderPlatforms)
      .catch(() => {
        // Non-critical: fall back to static platform options (sophos/wazuh)
      })
  }, [])

  // Active platform descriptor from catalog
  const activePlatformDescriptor = useMemo(
    () => providerPlatforms.find((p) => p.platform === platform) ?? null,
    [platform, providerPlatforms],
  )

  // Tiles do grid de seleção de plataforma (substitui o <select>). Usa o
  // catálogo do backend; fallback estático sophos/wazuh enquanto carrega/falha.
  const platformTiles = useMemo<Tile[]>(() => {
    // Catálogo 100% plugin-driven: label/description/category vêm do backend
    // (PlatformRegistration de cada vendor). A TileGallery agrupa/filtra por
    // `category` automaticamente — vendor novo aparece sem tocar nesta tela.
    if (providerPlatforms.length === 0) {
      // Fallback estático mínimo só enquanto o catálogo carrega/falha.
      return ([
        { id: "sophos", label: "Sophos", category: "EDR / XDR" },
        { id: "wazuh", label: "Wazuh", category: "SIEM" },
      ] as const).map((p) => ({
        id: p.id,
        label: p.label,
        category: p.category,
        icon: platformIcon(p.id),
      }))
    }
    return providerPlatforms.map((p) => ({
      id: p.platform,
      label: p.display_name,
      description: p.description || undefined,
      category: p.category || undefined,
      icon: platformIcon(p.platform, p.icon_id),
    }))
  }, [providerPlatforms])

  // Reset dynamic fields + resultado do teste ao trocar de plataforma
  useEffect(() => {
    setDynamicFieldValues({})
    setTestResult(null)
  }, [platform])

  useEffect(() => {
    if (!integration) {
      setOrganizationId("")
      setName("")
      setPlatform("sophos")
      setIsActive(true)
      setClientId("")
      setClientSecret("")
      setRegion("")
      setManagerEnabled(false)
      setManagerUrl("")
      setManagerApiUsername("")
      setManagerApiPassword("")
      setIndexerUrl("")
      setIndexerUsername("")
      setIndexerPassword("")
      setVerifySsl(true)
      setValidationError(null)
      return
    }

    setOrganizationId(integration.organization_id)
    setName(integration.name)
    setPlatform(integration.platform)
    setIsActive(integration.is_active)
    setClientId(integration.client_id ?? "")
    setClientSecret("")
    setRegion(integration.region ?? "")
    setManagerEnabled(Boolean(integration.manager_url || integration.manager_api_username || integration.manager_api_password_configured))
    setManagerUrl(integration.manager_url ?? "")
    setManagerApiUsername(integration.manager_api_username ?? "")
    setManagerApiPassword("")
    setIndexerUrl(integration.indexer_url ?? "")
    setIndexerUsername(integration.indexer_username ?? "")
    setIndexerPassword("")
    setVerifySsl(integration.verify_ssl ?? true)
    setValidationError(null)
  }, [integration])

  // Manager é opcional: só exige senha quando habilitado e com URL preenchida.
  const requiresManagerPassword = useMemo(() => {
    if (!managerEnabled || !managerUrl.trim()) return false
    if (mode === "create") return true
    return !integration?.manager_api_password_configured
  }, [managerEnabled, managerUrl, integration, mode])

  // Indexer é sempre obrigatório no Wazuh — no edit dispensa a senha quando já há
  // uma cifrada no store.
  const requiresIndexerPassword = useMemo(() => {
    if (mode === "create") return true
    return !integration?.indexer_password_configured
  }, [integration, mode])

  // Config de AUTH (cruas) para o teste pré-save — espelha o assembly do submit.
  const buildTestConfig = (): Record<string, unknown> => {
    if (platform === "sophos") {
      return {
        client_id: clientId.trim(),
        client_secret: clientSecret.trim(),
        region: region.trim() || undefined,
      }
    }
    // Plataformas dinâmicas (ninjaone, defender, …): valores em dynamicFieldValues.
    return { ...dynamicFieldValues }
  }

  const handleTestConnection = async () => {
    setTestResult(null)
    setTesting(true)
    try {
      const res = await api.testProviderConnection(platform, buildTestConfig())
      setTestResult({
        ok: res.ok,
        detail: res.detail || (res.ok ? t("form.testResult.ok") : t("form.testResult.fail")),
      })
    } catch (e) {
      setTestResult({
        ok: false,
        detail: e instanceof Error ? e.message : t("form.validation.testConnectionFailed"),
      })
    } finally {
      setTesting(false)
    }
  }

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    setValidationError(null)

    const trimmedName = name.trim()
    if (!trimmedName) {
      setValidationError(t("form.validation.nameRequired"))
      return
    }

    if (mode === "create" && !organizationId) {
      setValidationError(t("form.validation.organizationRequired"))
      return
    }

    // Validate dynamic fields for platforms without custom blocks
    if (!CUSTOM_BLOCK_PLATFORMS.has(platform) && activePlatformDescriptor) {
      for (const field of activePlatformDescriptor.auth_fields) {
        const val = dynamicFieldValues[field.key]
        const isEmpty = val === undefined || val === "" || val === null
        if (field.required && isEmpty) {
          setValidationError(t("form.validation.fieldRequired", { label: field.label }))
          return
        }
        // Campo select: o valor enviado deve ser uma das opções declaradas
        // (defesa contra valor forjado / opção removida do catálogo).
        if (field.type === "select" && field.options && !isEmpty && !field.options.includes(String(val))) {
          setValidationError(t("form.validation.fieldInvalidOption", { label: field.label }))
          return
        }
      }
    }

    if (platform === "sophos") {
      if (!clientId.trim()) {
        setValidationError(t("form.validation.sophosClientIdRequired"))
        return
      }
      if (mode === "create" && !clientSecret.trim()) {
        setValidationError(t("form.validation.sophosClientSecretRequired"))
        return
      }
    }

    if (platform === "wazuh") {
      // Indexer é a fonte de alertas/detecções — sempre obrigatório.
      if (!indexerUrl.trim()) {
        setValidationError(t("form.validation.wazuhIndexerUrlRequired"))
        return
      }
      if (!indexerUsername.trim()) {
        setValidationError(t("form.validation.wazuhIndexerUserRequired"))
        return
      }
      if (requiresIndexerPassword && !indexerPassword.trim()) {
        setValidationError(t("form.validation.wazuhIndexerPasswordRequired"))
        return
      }
      // Manager é opcional; se habilitado, o par precisa estar completo.
      if (managerEnabled) {
        if (!managerUrl.trim()) {
          setValidationError(t("form.validation.wazuhManagerUrlRequired"))
          return
        }
        if (!managerApiUsername.trim()) {
          setValidationError(t("form.validation.wazuhManagerUserRequired"))
          return
        }
        if (requiresManagerPassword && !managerApiPassword.trim()) {
          setValidationError(t("form.validation.wazuhManagerPasswordRequired"))
          return
        }
      }
    }

    if (mode === "create") {
      const payload: CreateIntegrationRequest & Record<string, unknown> = {
        organization_id: Number(organizationId),
        name: trimmedName,
        platform,
      }

      if (platform === "sophos") {
        // Card base "sophos" = tenant único. Os tiles Partner/Organization são
        // platforms próprias (sophos_partner/sophos_organization) e caem no ramo
        // genérico abaixo — o backend resolve base_platform + kind.
        payload.client_id = clientId.trim()
        payload.client_secret = clientSecret.trim()
        payload.region = normalizeOptional(region) ?? undefined
      } else if (platform === "wazuh") {
        // Indexer = fonte obrigatória; Manager = opcional (saúde/agentes).
        payload.indexer_url = indexerUrl.trim()
        payload.indexer_username = indexerUsername.trim()
        payload.indexer_password = indexerPassword.trim()
        if (managerEnabled && managerUrl.trim()) {
          payload.manager_url = managerUrl.trim()
          payload.manager_api_username = managerApiUsername.trim()
          payload.manager_api_password = managerApiPassword.trim()
        }
        payload.verify_ssl = verifySsl
      } else {
        // Dynamic fields for other platforms (ninjaone, microsoft_defender, etc.)
        for (const [key, val] of Object.entries(dynamicFieldValues)) {
          payload[key] = val
        }
      }

      await onSubmit(payload as CreateIntegrationRequest)
      return
    }

    const payload: UpdateIntegrationRequest = {
      name: trimmedName,
      is_active: isActive,
    }

    if (platform === "sophos") {
      payload.client_id = clientId.trim()
      payload.region = normalizeOptional(region)
      if (clientSecret.trim()) {
        payload.client_secret = clientSecret.trim()
      }
    } else {
      // Indexer é a fonte — sempre enviado no Wazuh.
      payload.indexer_url = indexerUrl.trim()
      payload.indexer_username = normalizeOptional(indexerUsername)
      if (indexerPassword.trim()) {
        payload.indexer_password = indexerPassword.trim()
      }

      // Manager é opcional; desabilitar (ou limpar a URL) revoga a config — o
      // backend apaga as credenciais do Manager no store.
      if (!managerEnabled || !managerUrl.trim()) {
        payload.manager_url = null
        payload.manager_api_username = null
        payload.manager_api_password = null
      } else {
        payload.manager_url = managerUrl.trim()
        payload.manager_api_username = normalizeOptional(managerApiUsername)
        if (managerApiPassword.trim()) {
          payload.manager_api_password = managerApiPassword.trim()
        }
      }

      payload.verify_ssl = verifySsl
    }

    await onSubmit(payload)
  }

  return (
    <form className="space-y-5" onSubmit={handleSubmit} noValidate>
      <div className="grid gap-4 md:grid-cols-2">
        {mode === "create" ? (
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-text" htmlFor="integration-org">
              {t("form.organization")}
            </label>
            <select
              id="integration-org"
              className={selectCls}
              value={organizationId}
              onChange={(event) => setOrganizationId(event.target.value ? Number(event.target.value) : "")}
              disabled={loading}
            >
              <option value="">{t("form.selectPlaceholder")}</option>
              {organizations.map((organization) => (
                <option key={organization.id} value={organization.id}>
                  {organization.name}
                </option>
              ))}
            </select>
          </div>
        ) : (
          <div className="space-y-1.5">
            <span className="text-sm font-medium text-text">{t("form.organization")}</span>
            <div className="flex h-9 items-center rounded-md border border-border bg-surface px-3 text-sm text-text-secondary">
              {integration?.organization_name || t("form.notInformed")}
            </div>
          </div>
        )}

        <Input
          label={t("form.name")}
          value={name}
          onChange={(event) => setName(event.target.value)}
          required
          disabled={loading}
        />

        <div className="space-y-2 md:col-span-2">
          <label className="text-sm font-medium text-text">{t("form.platform")}</label>
          {mode === "edit" ? (
            // Edição: a plataforma é imutável — exibe a selecionada, read-only.
            <div className="inline-flex items-center gap-2 rounded-lg border border-border bg-surface-secondary px-3 py-2 text-sm">
              <Badge variant="primary" size="sm">
                {activePlatformDescriptor?.display_name ?? platform}
              </Badge>
              <span className="text-text-tertiary">{t("form.notEditable")}</span>
            </div>
          ) : (
            <TileGallery
              tiles={platformTiles}
              value={platform}
              onChange={(id) => setPlatform(id as PlatformType)}
              disabled={loading}
              searchPlaceholder={t("form.searchPlatformPlaceholder")}
              ariaLabel={t("form.selectPlatformAriaLabel")}
              emptyLabel={t("form.noPlatformFound")}
            />
          )}
          {activePlatformDescriptor?.docs_url && (
            <p className="text-xs text-text-secondary">
              <a
                href={activePlatformDescriptor.docs_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-primary-600 underline hover:text-primary-700"
              >
                {t("form.viewConfigDocs")}
              </a>
            </p>
          )}
        </div>

        {mode === "edit" && (
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-text" htmlFor="integration-status">
              {t("form.integrationStatus")}
            </label>
            <select
              id="integration-status"
              className={selectCls}
              value={isActive ? "true" : "false"}
              onChange={(event) => setIsActive(event.target.value === "true")}
              disabled={loading}
            >
              <option value="true">{t("form.statusActive")}</option>
              <option value="false">{t("form.statusInactive")}</option>
            </select>
          </div>
        )}
      </div>

      {activePlatformDescriptor?.transport === "push" && (
        <Notice variant="info" title={t("form.pushSource.title")}>
          <Trans i18nKey="form.pushSource.intro" t={t} components={{ strong: <strong /> }} />
          {mode === "create" ? (
            <Trans
              i18nKey="form.pushSource.createHint"
              t={t}
              components={{
                strong: <strong />,
                code: <code className="rounded bg-surface-tertiary px-1" />,
              }}
            />
          ) : (
            <>{t("form.pushSource.editHint")}</>
          )}
        </Notice>
      )}

      {platform === "sophos" && (
        // Card base "sophos" = tenant único. Partner/Organization são tiles
        // próprios (sophos_partner/sophos_organization) — sem toggle aqui.
        <div className="grid gap-4 md:grid-cols-3">
          <Input label={t("form.sophos.clientId")} value={clientId} onChange={(event) => setClientId(event.target.value)} required disabled={loading} />
          <Input
            label={mode === "edit" ? t("form.sophos.newClientSecret") : t("form.sophos.clientSecret")}
            type="password"
            value={clientSecret}
            onChange={(event) => setClientSecret(event.target.value)}
            required={mode === "create"}
            helperText={mode === "edit" ? t("form.sophos.clientSecretHelper") : undefined}
            disabled={loading}
          />
          <Input
            label={t("form.sophos.region")}
            value={region}
            onChange={(event) => setRegion(event.target.value)}
            helperText={t("form.sophos.regionHelper")}
            disabled={loading}
          />
        </div>
      )}

      {platform === "wazuh" && (
        <div className="space-y-5">
          {/* Indexer = fonte de alertas/detecções/states (wazuh-alerts-*). É o que
              alimenta o pipeline — sempre obrigatório. */}
          <div className="rounded-xl border border-border bg-surface-tertiary/40 p-4">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold text-text">{t("form.wazuh.indexerTitle")}</h3>
              <Badge variant="primary" size="sm">
                {t("form.wazuh.required")}
              </Badge>
            </div>
            <p className="mt-1 text-xs text-text-secondary">
              {t("form.wazuh.indexerDescription")}
            </p>
            <div className="mt-4 grid gap-4 md:grid-cols-3">
              <Input label={t("form.wazuh.indexerUrl")} type="url" value={indexerUrl} onChange={(event) => setIndexerUrl(event.target.value)} required disabled={loading} />
              <Input label={t("form.wazuh.indexerUser")} value={indexerUsername} onChange={(event) => setIndexerUsername(event.target.value)} required disabled={loading} />
              <Input
                label={mode === "edit" ? t("form.wazuh.newIndexerPassword") : t("form.wazuh.indexerPassword")}
                type="password"
                value={indexerPassword}
                onChange={(event) => setIndexerPassword(event.target.value)}
                required={requiresIndexerPassword}
                helperText={mode === "edit" ? t("form.wazuh.passwordHelper") : undefined}
                disabled={loading}
              />
            </div>
          </div>

          {/* Manager = opcional: saúde do servidor + inventário de agentes. NÃO
              coleta alertas/detecções. */}
          <div className="rounded-xl border border-border bg-surface-tertiary/40 p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-sm font-semibold text-text">{t("form.wazuh.managerTitle")}</h3>
                  <Badge variant="outline" size="sm">
                    {t("form.wazuh.optional")}
                  </Badge>
                </div>
                <p className="mt-1 text-xs text-text-secondary">
                  {t("form.wazuh.managerDescription")}
                </p>
              </div>
              <label className="flex items-center gap-2 text-sm text-text">
                <input
                  type="checkbox"
                  checked={managerEnabled}
                  onChange={(event) => setManagerEnabled(event.target.checked)}
                  disabled={loading}
                />
                {t("form.wazuh.enableManager")}
              </label>
            </div>

            {managerEnabled && (
              <div className="mt-4 grid gap-4 md:grid-cols-3">
                <Input label={t("form.wazuh.managerUrl")} type="url" value={managerUrl} onChange={(event) => setManagerUrl(event.target.value)} disabled={loading} />
                <Input label={t("form.wazuh.managerUser")} value={managerApiUsername} onChange={(event) => setManagerApiUsername(event.target.value)} disabled={loading} />
                <Input
                  label={mode === "edit" ? t("form.wazuh.newManagerPassword") : t("form.wazuh.managerPassword")}
                  type="password"
                  value={managerApiPassword}
                  onChange={(event) => setManagerApiPassword(event.target.value)}
                  required={requiresManagerPassword}
                  helperText={mode === "edit" ? t("form.wazuh.passwordHelper") : undefined}
                  disabled={loading}
                />
              </div>
            )}
          </div>

          <label className="flex items-center gap-2 text-sm text-text">
            <input
              type="checkbox"
              checked={verifySsl}
              onChange={(event) => setVerifySsl(event.target.checked)}
              disabled={loading}
            />
            {t("form.wazuh.verifySslCerts")}
          </label>
        </div>
      )}

      {/* Dynamic auth fields for platforms without a custom block (e.g. ninjaone, microsoft_defender) */}
      {activePlatformDescriptor &&
        !CUSTOM_BLOCK_PLATFORMS.has(platform) &&
        activePlatformDescriptor.auth_fields.length > 0 && (
          <div className="space-y-4">
            <div className="rounded-xl border border-border bg-surface-tertiary/40 p-4">
              <h3 className="mb-3 text-sm font-semibold text-text">{t("form.credentialsTitle")}</h3>
              {platform.startsWith("sophos_") && (
                <div className="mb-3 rounded-md border border-primary-200 bg-primary-50/40 px-3 py-2 text-xs text-text">
                  <strong>{t("form.sophos.importNoticeTitle")}</strong>
                  <Trans i18nKey="form.sophos.importNoticeBody" t={t} components={{ em: <em /> }} />
                </div>
              )}
              <div className="grid gap-4 md:grid-cols-2">
                {activePlatformDescriptor.auth_fields.map((field) => (
                  <DynamicAuthField
                    key={field.key}
                    field={field}
                    value={dynamicFieldValues[field.key] ?? ""}
                    onChange={(val) =>
                      setDynamicFieldValues((prev) => ({ ...prev, [field.key]: val }))
                    }
                    disabled={loading}
                  />
                ))}
              </div>
            </div>
          </div>
        )}

      {/* Streams info — collapsible, informational only */}
      {activePlatformDescriptor && activePlatformDescriptor.streams.length > 0 && (
        <details className="rounded-xl border border-border">
          <summary className="cursor-pointer list-none px-4 py-3 text-sm font-medium text-text">
            {t("form.streamsInfo.summary", { count: activePlatformDescriptor.streams.length })}
          </summary>
          <div className="border-t border-border px-4 py-3">
            <div className="space-y-1.5">
              {activePlatformDescriptor.streams.map((stream) => (
                <div key={stream.stream} className="flex items-center justify-between text-sm">
                  <span className="font-medium text-text">{stream.stream}</span>
                  <span className="text-xs text-text-secondary">
                    {t("form.streamsInfo.every", { schedule: formatScheduleSeconds(stream.schedule_seconds) })}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </details>
      )}

      {validationError && (
        <div
          className="rounded-md border border-danger-200 bg-danger-50 px-3 py-2 text-sm text-danger-700"
          role="alert"
          aria-live="polite"
        >
          {validationError}
        </div>
      )}

      {testResult && (
        <div
          className={`rounded-md border px-3 py-2 text-sm ${
            testResult.ok
              ? "border-success-200 bg-success-50 text-success-700"
              : "border-danger-200 bg-danger-50 text-danger-700"
          }`}
          role="status"
          aria-live="polite"
        >
          {testResult.ok ? "✓ " : "✗ "}
          {testResult.detail}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          {mode === "create" && activePlatformDescriptor?.supports_test && (
            <Button
              type="button"
              variant="outline"
              onClick={handleTestConnection}
              loading={testing}
              disabled={loading}
            >
              {t("form.testConnection")}
            </Button>
          )}
        </div>
        <div className="flex gap-3">
          {onCancel && (
            <Button type="button" variant="outline" onClick={onCancel} disabled={loading}>
              {t("common:actions.cancel")}
            </Button>
          )}
          <Button type="submit" loading={loading}>
            {mode === "create" ? t("form.createSubmit") : t("form.editSubmit")}
          </Button>
        </div>
      </div>
    </form>
  )
}
