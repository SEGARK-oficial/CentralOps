"use client"

import type React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  BuildingIcon,
  ClockIcon,
  GlobeIcon,
  KeyRoundIcon,
  LogOutIcon,
  ShieldCheckIcon,
  UserCogIcon,
} from "lucide-react"

import * as api from "@/services/api"
import { ApiRequestError } from "@/services/api"
import type { AccountProfile, SelfProfileUpdate } from "@/types"
import { useAuth } from "@/contexts/AuthContext"

import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { Input } from "@/components/ui/Input/Input"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Notice } from "@/components/ui/Notice/Notice"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { Select } from "@/components/ui/Select/Select"
import { formatDateTime } from "@/lib/intl"

// Espelha backend MIN_PASSWORD_LENGTH (routers/auth.py). Só pré-validação de UX;
// o servidor continua sendo a fonte da verdade.
const MIN_PASSWORD_LENGTH = 10

type Feedback = { type: "success" | "error"; message: string }

function errorMessage(e: unknown, fallback: string): string {
  if (e instanceof ApiRequestError) return e.message
  if (e instanceof Error) return e.message
  return fallback
}

function formatMaybeDate(iso: string | null | undefined, fallback: string): string {
  if (!iso) return fallback
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? fallback : formatDateTime(d)
}

// ── Read-only summary ─────────────────────────────────────────────────

const SummaryItem: React.FC<{
  icon: React.ReactNode
  label: string
  children: React.ReactNode
}> = ({ icon, label, children }) => (
  <div className="flex items-start gap-3">
    <span className="mt-0.5 shrink-0 text-text-tertiary" aria-hidden="true">
      {icon}
    </span>
    <div className="min-w-0">
      <dt className="text-xs font-medium uppercase tracking-wider text-text-secondary">{label}</dt>
      <dd className="mt-0.5 break-words text-sm text-text">{children}</dd>
    </div>
  </div>
)

// ── Página ────────────────────────────────────────────────────────────

export const AccountSettingsPage: React.FC = () => {
  const { t } = useTranslation("account")
  const { updateUser } = useAuth()

  const [profile, setProfile] = useState<AccountProfile | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<Feedback | null>(null)

  // Profile form
  const [displayName, setDisplayName] = useState("")
  const [email, setEmail] = useState("")
  const [locale, setLocale] = useState("")
  const [emailPassword, setEmailPassword] = useState("")
  const [savingProfile, setSavingProfile] = useState(false)

  // Password form
  const [currentPassword, setCurrentPassword] = useState("")
  const [newPassword, setNewPassword] = useState("")
  const [confirmPassword, setConfirmPassword] = useState("")
  const [pwError, setPwError] = useState<string | null>(null)
  const [changingPassword, setChangingPassword] = useState(false)

  // Sessions
  const [confirmSignOut, setConfirmSignOut] = useState(false)
  const [signingOut, setSigningOut] = useState(false)

  const seedForm = useCallback((p: AccountProfile) => {
    setDisplayName(p.display_name ?? "")
    setEmail(p.email ?? "")
    setLocale(p.locale ?? "")
    setEmailPassword("")
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      const p = await api.getMyProfile()
      setProfile(p)
      seedForm(p)
    } catch (e) {
      setLoadError(errorMessage(e, t("load.error")))
    } finally {
      setLoading(false)
    }
  }, [seedForm, t])

  useEffect(() => {
    void load()
  }, [load])

  // Auto-dismiss success feedback; errors persist until the next action.
  useEffect(() => {
    if (feedback?.type !== "success") return
    const timer = setTimeout(() => setFeedback(null), 5000)
    return () => clearTimeout(timer)
  }, [feedback])

  const isLocal = (profile?.auth_provider ?? "local") === "local"

  const localeOptions = useMemo(
    () => [
      { value: "pt", label: t("locales.pt") },
      { value: "en", label: t("locales.en") },
      { value: "es", label: t("locales.es") },
    ],
    [t],
  )

  const displayNameChanged = displayName.trim() !== (profile?.display_name ?? "")
  const emailChanged = email.trim() !== (profile?.email ?? "")
  const localeChanged = locale !== (profile?.locale ?? "")
  const profileDirty = displayNameChanged || emailChanged || localeChanged

  const handleSaveProfile = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!profile || !profileDirty) return

    // Trocar e-mail é sensível: só para contas locais e com reautenticação.
    if (emailChanged && !isLocal) {
      setFeedback({ type: "error", message: t("profile.emailManagedByIdp") })
      return
    }
    if (emailChanged && !emailPassword.trim()) {
      setFeedback({ type: "error", message: t("profile.emailPasswordRequired") })
      return
    }

    const payload: SelfProfileUpdate = {}
    if (displayNameChanged) payload.display_name = displayName.trim() || null
    if (localeChanged) payload.locale = locale || null
    if (emailChanged) {
      payload.email = email.trim() || null
      payload.current_password = emailPassword
    }

    setSavingProfile(true)
    setFeedback(null)
    try {
      const updated = await api.updateMyProfile(payload)
      setProfile(updated)
      seedForm(updated)
      updateUser({
        display_name: updated.display_name,
        email: updated.email,
        locale: updated.locale,
      })
      setFeedback({ type: "success", message: t("profile.saved") })
    } catch (err) {
      setFeedback({ type: "error", message: errorMessage(err, t("profile.saveError")) })
    } finally {
      setSavingProfile(false)
      // Nunca reter a senha atual (credencial sensível) em memória após a
      // tentativa — no sucesso o seedForm já limpou; aqui cobrimos o erro.
      setEmailPassword("")
    }
  }

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault()
    setPwError(null)

    if (newPassword.length < MIN_PASSWORD_LENGTH) {
      setPwError(t("password.tooShort", { min: MIN_PASSWORD_LENGTH }))
      return
    }
    if (newPassword !== confirmPassword) {
      setPwError(t("password.mismatch"))
      return
    }
    if (newPassword === currentPassword) {
      setPwError(t("password.reuse"))
      return
    }

    setChangingPassword(true)
    setFeedback(null)
    try {
      const result = await api.changeMyPassword({
        current_password: currentPassword,
        new_password: newPassword,
      })
      setCurrentPassword("")
      setNewPassword("")
      setConfirmPassword("")
      setFeedback({
        type: "success",
        message:
          result.revoked_other_sessions > 0
            ? t("password.changedWithSessions", { count: result.revoked_other_sessions })
            : t("password.changed"),
      })
    } catch (err) {
      setPwError(errorMessage(err, t("password.error")))
    } finally {
      setChangingPassword(false)
    }
  }

  const handleSignOutOthers = async () => {
    setSigningOut(true)
    setFeedback(null)
    try {
      const result = await api.revokeMyOtherSessions()
      setFeedback({
        type: "success",
        message:
          result.revoked > 0
            ? t("sessions.revoked", { count: result.revoked })
            : t("sessions.noneToRevoke"),
      })
    } catch (err) {
      setFeedback({ type: "error", message: errorMessage(err, t("sessions.error")) })
    } finally {
      setSigningOut(false)
      setConfirmSignOut(false)
    }
  }

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <LoadingSpinner />
      </div>
    )
  }

  if (loadError || !profile) {
    return (
      <div className="space-y-6" data-testid="account-page">
        <PageHeader
          icon={<UserCogIcon size={24} />}
          eyebrow={t("eyebrow")}
          title={t("title")}
          description={t("description")}
        />
        <Notice
          variant="danger"
          action={
            <Button variant="ghost" size="sm" onClick={load}>
              {t("common:actions.retry")}
            </Button>
          }
        >
          {loadError ?? t("load.error")}
        </Notice>
      </div>
    )
  }

  const scopeLabel = profile.is_global
    ? t("summary.scopeGlobal")
    : profile.organization_name
      ? t("summary.scopeOrg", { org: profile.organization_name })
      : t("summary.scopeNone")

  return (
    <div className="space-y-6" data-testid="account-page">
      <PageHeader
        icon={<UserCogIcon size={24} />}
        eyebrow={t("eyebrow")}
        title={t("title")}
        description={t("description")}
      />

      {feedback && (
        <Notice
          variant={feedback.type === "success" ? "success" : "danger"}
          action={
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setFeedback(null)}
              aria-label={t("common:actions.close")}
            >
              {t("common:actions.close")}
            </Button>
          }
        >
          {feedback.message}
        </Notice>
      )}

      {/* ── Identity summary (read-only) ── */}
      <Card>
        <div className="border-b px-4 py-3">
          <h2 className="text-sm font-semibold text-text">{t("summary.title")}</h2>
          <p className="text-xs text-text-secondary">{t("summary.subtitle")}</p>
        </div>
        <dl className="grid grid-cols-1 gap-4 p-4 sm:grid-cols-2 lg:grid-cols-3">
          <SummaryItem icon={<UserCogIcon size={16} />} label={t("summary.username")}>
            <span className="font-mono">{profile.username}</span>
          </SummaryItem>
          <SummaryItem icon={<ShieldCheckIcon size={16} />} label={t("summary.role")}>
            <span className="capitalize">{profile.role}</span>
          </SummaryItem>
          <SummaryItem icon={<GlobeIcon size={16} />} label={t("summary.scope")}>
            {scopeLabel}
          </SummaryItem>
          <SummaryItem icon={<BuildingIcon size={16} />} label={t("summary.organization")}>
            {profile.organization_name ?? "—"}
          </SummaryItem>
          <SummaryItem icon={<KeyRoundIcon size={16} />} label={t("summary.signInMethod")}>
            {isLocal ? t("summary.methodLocal") : t("summary.methodSso")}
          </SummaryItem>
          <SummaryItem icon={<ClockIcon size={16} />} label={t("summary.memberSince")}>
            {formatMaybeDate(profile.created_at, "—")}
          </SummaryItem>
          <SummaryItem icon={<ClockIcon size={16} />} label={t("summary.lastSignIn")}>
            {formatMaybeDate(profile.last_login_at, t("summary.never"))}
          </SummaryItem>
        </dl>
      </Card>

      {/* ── Editable profile ── */}
      <Card>
        <div className="border-b px-4 py-3">
          <h2 className="text-sm font-semibold text-text">{t("profile.title")}</h2>
          <p className="text-xs text-text-secondary">{t("profile.subtitle")}</p>
        </div>
        <form onSubmit={handleSaveProfile} className="space-y-4 p-4">
          <Input
            id="account-display-name"
            label={t("profile.displayName")}
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            maxLength={120}
            helperText={t("profile.displayNameHelp")}
            autoComplete="name"
          />

          <Input
            id="account-email"
            type="email"
            label={t("profile.email")}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            maxLength={254}
            disabled={!isLocal}
            helperText={isLocal ? t("profile.emailHelp") : t("profile.emailManagedByIdp")}
            autoComplete="email"
          />

          {isLocal && emailChanged && (
            <Input
              id="account-email-password"
              type="password"
              label={t("profile.confirmPassword")}
              value={emailPassword}
              onChange={(e) => setEmailPassword(e.target.value)}
              helperText={t("profile.confirmPasswordHelp")}
              autoComplete="current-password"
            />
          )}

          <Select
            id="account-locale"
            label={t("profile.language")}
            options={localeOptions}
            value={locale}
            onChange={(v) => setLocale(String(v))}
            helperText={t("profile.languageHelp")}
          />

          <div className="flex justify-end">
            <Button
              type="submit"
              disabled={!profileDirty || savingProfile}
              data-testid="account-save-profile"
            >
              {savingProfile ? t("common:actions.save") + "…" : t("common:actions.save")}
            </Button>
          </div>
        </form>
      </Card>

      {/* ── Security ── */}
      <Card>
        <div className="border-b px-4 py-3">
          <h2 className="text-sm font-semibold text-text">{t("security.title")}</h2>
          <p className="text-xs text-text-secondary">{t("security.subtitle")}</p>
        </div>

        <div className="space-y-6 p-4">
          {isLocal ? (
            <form onSubmit={handleChangePassword} className="space-y-4">
              <h3 className="text-sm font-medium text-text">{t("password.title")}</h3>
              <Input
                id="account-current-password"
                type="password"
                label={t("password.current")}
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
              <Input
                id="account-new-password"
                type="password"
                label={t("password.new")}
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                autoComplete="new-password"
                helperText={t("password.rule", { min: MIN_PASSWORD_LENGTH })}
                required
              />
              <Input
                id="account-confirm-password"
                type="password"
                label={t("password.confirm")}
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                autoComplete="new-password"
                required
              />
              {pwError && <Notice variant="danger">{pwError}</Notice>}
              <div className="flex justify-end">
                <Button
                  type="submit"
                  data-testid="account-change-password"
                  disabled={
                    changingPassword ||
                    !currentPassword ||
                    !newPassword ||
                    !confirmPassword
                  }
                >
                  {changingPassword ? t("password.submit") + "…" : t("password.submit")}
                </Button>
              </div>
            </form>
          ) : (
            <Notice variant="info">{t("password.ssoManaged")}</Notice>
          )}

          <div className="border-t pt-4">
            <h3 className="text-sm font-medium text-text">{t("sessions.title")}</h3>
            <p className="mt-1 text-xs text-text-secondary">{t("sessions.subtitle")}</p>
            <div className="mt-3">
              <Button
                variant="outline"
                leftIcon={<LogOutIcon size={16} />}
                onClick={() => setConfirmSignOut(true)}
                disabled={signingOut}
                data-testid="account-signout-others"
              >
                {t("sessions.action")}
              </Button>
            </div>
          </div>
        </div>
      </Card>

      <ConfirmDialog
        open={confirmSignOut}
        title={t("sessions.confirmTitle")}
        description={t("sessions.confirmDescription")}
        confirmLabel={t("sessions.action")}
        cancelLabel={t("common:actions.cancel")}
        confirmVariant="danger"
        loading={signingOut}
        onConfirm={handleSignOutOthers}
        onClose={() => setConfirmSignOut(false)}
      />
    </div>
  )
}

export default AccountSettingsPage
