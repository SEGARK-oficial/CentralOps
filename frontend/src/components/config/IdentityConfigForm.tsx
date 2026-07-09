"use client"

import type React from "react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/Button/Button"
import { Checkbox } from "@/components/ui/Checkbox/Checkbox"
import { Input } from "@/components/ui/Input/Input"
import { Notice } from "@/components/ui/Notice/Notice"
import { Select } from "@/components/ui/Select/Select"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import { EntraSyncPanel } from "@/components/config/EntraSyncPanel"
import type { IdentityConfig, UpdateIdentityConfigRequest } from "@/types"
import { useEntraSync } from "@/hooks/useEntraSync"

type Feedback = { type: "success" | "error"; message: string } | null

interface Props {
  config: IdentityConfig | null
  loading: boolean
  saving: boolean
  testing: boolean
  feedback: Feedback
  onSave: (data: UpdateIdentityConfigRequest) => Promise<boolean>
  onTest: () => Promise<boolean>
}

const ROLE_KEYS = [
  { value: "viewer", labelKey: "identity.roles.viewer" },
  { value: "operator", labelKey: "identity.roles.operator" },
  { value: "engineer", labelKey: "identity.roles.engineer" },
  { value: "admin", labelKey: "identity.roles.admin" },
]

interface FormState {
  entra_enabled: boolean
  entra_tenant_id: string
  entra_client_id: string
  entra_client_secret: string
  entra_redirect_uri: string
  entra_authority: string
  entra_scopes: string
  entra_default_role: string
  entra_default_is_global: boolean
  entra_jit_provisioning: boolean
  entra_allowed_email_domains: string
  entra_role_map: string
  entra_button_label: string
  entra_post_login_redirect: string
  // Fase 2B: toggles de sync Graph
  entra_sync_enabled: boolean
  entra_sync_deprovision: boolean
}

function toForm(c: IdentityConfig | null, defaultButtonLabel: string): FormState {
  return {
    entra_enabled: c?.entra_enabled ?? false,
    entra_tenant_id: c?.entra_tenant_id ?? "",
    entra_client_id: c?.entra_client_id ?? "",
    entra_client_secret: "",
    entra_redirect_uri: c?.entra_redirect_uri ?? "",
    entra_authority: c?.entra_authority ?? "https://login.microsoftonline.com",
    entra_scopes: c?.entra_scopes ?? "openid profile email",
    entra_default_role: c?.entra_default_role ?? "viewer",
    entra_default_is_global: c?.entra_default_is_global ?? false,
    entra_jit_provisioning: c?.entra_jit_provisioning ?? true,
    entra_allowed_email_domains: (c?.entra_allowed_email_domains ?? []).join(", "),
    entra_role_map: JSON.stringify(c?.entra_role_map ?? {}, null, 2),
    entra_button_label: c?.entra_button_label ?? defaultButtonLabel,
    entra_post_login_redirect: c?.entra_post_login_redirect ?? "/",
    entra_sync_enabled: c?.entra_sync_enabled ?? false,
    entra_sync_deprovision: c?.entra_sync_deprovision ?? true,
  }
}

export const IdentityConfigForm: React.FC<Props> = ({
  config, loading, saving, testing, feedback, onSave, onTest,
}) => {
  const { t } = useTranslation("config")
  const [form, setForm] = useState<FormState>(() => toForm(config, t("identity.defaultButtonLabel")))
  const [roleMapError, setRoleMapError] = useState<string | null>(null)

  // Hook de sync — carrega status ao montar; expõe syncNow e refreshStatus
  const {
    syncStatus,
    loadingStatus: syncLoadingStatus,
    syncing,
    feedback: syncFeedback,
    syncNow,
    refreshStatus,
  } = useEntraSync()

  useEffect(() => {
    setForm(toForm(config, t("identity.defaultButtonLabel")))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config])

  const update = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((f) => ({ ...f, [key]: value }))

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setRoleMapError(null)

    let roleMap: Record<string, string>
    try {
      roleMap = form.entra_role_map.trim() ? JSON.parse(form.entra_role_map) : {}
      if (typeof roleMap !== "object" || roleMap === null || Array.isArray(roleMap)) {
        throw new Error(t("identity.roleMapInvalidNotObject"))
      }
    } catch (err) {
      setRoleMapError(
        t("identity.roleMapInvalid", {
          message: err instanceof Error ? err.message : t("identity.roleMapInvalidGeneric"),
        }),
      )
      return
    }

    const domains = form.entra_allowed_email_domains
      .split(",")
      .map((d) => d.trim().toLowerCase())
      .filter(Boolean)

    const payload: UpdateIdentityConfigRequest = {
      entra_enabled: form.entra_enabled,
      entra_tenant_id: form.entra_tenant_id.trim() || null,
      entra_client_id: form.entra_client_id.trim() || null,
      entra_redirect_uri: form.entra_redirect_uri.trim() || null,
      entra_authority: form.entra_authority.trim(),
      entra_scopes: form.entra_scopes.trim(),
      entra_role_map: roleMap,
      entra_default_role: form.entra_default_role,
      entra_default_is_global: form.entra_default_is_global,
      entra_jit_provisioning: form.entra_jit_provisioning,
      entra_allowed_email_domains: domains,
      entra_button_label: form.entra_button_label.trim() || t("identity.defaultButtonLabel"),
      entra_post_login_redirect: form.entra_post_login_redirect.trim() || "/",
      // Fase 2B
      entra_sync_enabled: form.entra_sync_enabled,
      entra_sync_deprovision: form.entra_sync_deprovision,
    }
    // O secret só é enviado quando digitado (vazio preserva o atual).
    if (form.entra_client_secret.trim()) {
      payload.entra_client_secret = form.entra_client_secret.trim()
    }

    const ok = await onSave(payload)
    if (ok) update("entra_client_secret", "")
  }

  const disabled = loading || saving

  return (
    <form onSubmit={handleSubmit} className="space-y-5">
      {feedback && (
        <Notice variant={feedback.type === "success" ? "success" : "danger"}>
          {feedback.message}
        </Notice>
      )}

      <Checkbox
        label={t("identity.enableEntraLogin")}
        description={t("identity.enableEntraLoginDescription")}
        checked={form.entra_enabled}
        onChange={(e) => update("entra_enabled", e.target.checked)}
        disabled={disabled}
      />

      <div className="grid gap-4 sm:grid-cols-2">
        <Input
          label={t("identity.fields.tenantId")}
          value={form.entra_tenant_id}
          onChange={(e) => update("entra_tenant_id", e.target.value)}
          placeholder="00000000-0000-0000-0000-000000000000"
          disabled={disabled}
        />
        <Input
          label={t("identity.fields.clientId")}
          value={form.entra_client_id}
          onChange={(e) => update("entra_client_id", e.target.value)}
          placeholder="00000000-0000-0000-0000-000000000000"
          disabled={disabled}
        />
      </div>

      <Input
        type="password"
        label={t("identity.fields.clientSecret")}
        value={form.entra_client_secret}
        onChange={(e) => update("entra_client_secret", e.target.value)}
        placeholder={
          config?.entra_client_secret_configured
            ? t("identity.fields.clientSecretPlaceholderConfigured")
            : t("identity.fields.clientSecretPlaceholderEmpty")
        }
        helperText={t("identity.fields.clientSecretHelper")}
        disabled={disabled}
      />

      <Input
        label={t("identity.fields.redirectUri")}
        value={form.entra_redirect_uri}
        onChange={(e) => update("entra_redirect_uri", e.target.value)}
        placeholder={t("identity.fields.redirectUriPlaceholder")}
        helperText={t("identity.fields.redirectUriHelper")}
        disabled={disabled}
      />

      <div className="grid gap-4 sm:grid-cols-2">
        <Input
          label={t("identity.fields.authority")}
          value={form.entra_authority}
          onChange={(e) => update("entra_authority", e.target.value)}
          helperText={t("identity.fields.authorityHelper")}
          disabled={disabled}
        />
        <Input
          label={t("identity.fields.scopes")}
          value={form.entra_scopes}
          onChange={(e) => update("entra_scopes", e.target.value)}
          disabled={disabled}
        />
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <Select
          label={t("identity.fields.defaultRole")}
          options={ROLE_KEYS.map((r) => ({ value: r.value, label: t(r.labelKey) }))}
          value={form.entra_default_role}
          onChange={(v) => update("entra_default_role", String(v))}
          helperText={t("identity.fields.defaultRoleHelper")}
        />
        <Input
          label={t("identity.fields.allowedEmailDomains")}
          value={form.entra_allowed_email_domains}
          onChange={(e) => update("entra_allowed_email_domains", e.target.value)}
          placeholder={t("identity.fields.allowedEmailDomainsPlaceholder")}
          helperText={t("identity.fields.allowedEmailDomainsHelper")}
          disabled={disabled}
        />
      </div>

      <Textarea
        label={t("identity.fields.roleMap")}
        value={form.entra_role_map}
        onChange={(e) => update("entra_role_map", e.target.value)}
        rows={5}
        error={roleMapError ?? undefined}
        helperText={t("identity.fields.roleMapHelper")}
        disabled={disabled}
      />

      <div className="flex flex-col gap-3">
        <Checkbox
          label={t("identity.globalScopeDefault")}
          description={t("identity.globalScopeDefaultDescription")}
          checked={form.entra_default_is_global}
          onChange={(e) => update("entra_default_is_global", e.target.checked)}
          disabled={disabled}
        />
        <Checkbox
          label={t("identity.jitProvisioning")}
          description={t("identity.jitProvisioningDescription")}
          checked={form.entra_jit_provisioning}
          onChange={(e) => update("entra_jit_provisioning", e.target.checked)}
          disabled={disabled}
        />
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <Input
          label={t("identity.fields.buttonLabel")}
          value={form.entra_button_label}
          onChange={(e) => update("entra_button_label", e.target.value)}
          disabled={disabled}
        />
        <Input
          label={t("identity.fields.postLoginRedirect")}
          value={form.entra_post_login_redirect}
          onChange={(e) => update("entra_post_login_redirect", e.target.value)}
          disabled={disabled}
        />
      </div>

      {/* ── Fase 2B: Sincronização de Usuários (Graph) ───────────────── */}
      <div className="border-t border-border pt-5">
        <h4 className="text-sm font-semibold text-text mb-3">
          {t("identity.sync.title")}
        </h4>
        <div className="flex flex-col gap-3">
          <Checkbox
            label={t("identity.sync.enabled")}
            description={t("identity.sync.enabledDescription")}
            checked={form.entra_sync_enabled}
            onChange={(e) => update("entra_sync_enabled", e.target.checked)}
            disabled={disabled}
          />
          <Checkbox
            label={t("identity.sync.deprovision")}
            description={t("identity.sync.deprovisionDescription")}
            checked={form.entra_sync_deprovision}
            onChange={(e) => update("entra_sync_deprovision", e.target.checked)}
            disabled={disabled}
          />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
        <Button type="submit" size="sm" loading={saving} disabled={disabled}>
          {t("identity.saveConfig")}
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          loading={testing}
          disabled={disabled}
          onClick={() => void onTest()}
        >
          {t("identity.testConnection")}
        </Button>
        {config && !config.is_persisted && (
          <span className="text-xs text-text-tertiary">
            {t("identity.envOnlyHint")}
          </span>
        )}
      </div>

      {/* Painel de status de sync — fora do fluxo do form mas dentro do card */}
      <EntraSyncPanel
        syncStatus={syncStatus}
        loadingStatus={syncLoadingStatus}
        syncing={syncing}
        feedback={syncFeedback}
        onSyncNow={syncNow}
        onRefreshStatus={refreshStatus}
      />
    </form>
  )
}

export default IdentityConfigForm
