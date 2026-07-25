"use client"

import type React from "react"
import { useEffect, useState } from "react"
import { useSearchParams } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { AlertTriangleIcon, ShieldCheckIcon, LockIcon, UserIcon, KeyIcon } from "lucide-react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card/Card"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { LanguageSwitcher } from "@/components/ui/LanguageSwitcher/LanguageSwitcher"
import { useAuth } from "@/contexts/AuthContext"
import { useForm } from "@/hooks/useForm"
import { ssoLoginUrl } from "@/services/api"

interface LoginFormValues {
  display_name: string
  username: string
  password: string
  confirm_password: string
}

const initialValues: LoginFormValues = {
  display_name: "",
  username: "",
  password: "",
  confirm_password: "",
}

const MicrosoftIcon: React.FC = () => (
  <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
    <rect x="1" y="1" width="6.4" height="6.4" fill="#F25022" />
    <rect x="8.6" y="1" width="6.4" height="6.4" fill="#7FBA00" />
    <rect x="1" y="8.6" width="6.4" height="6.4" fill="#00A4EF" />
    <rect x="8.6" y="8.6" width="6.4" height="6.4" fill="#FFB900" />
  </svg>
)

/**
 * A marca: três faixas entram, o portão aperta, duas saem.
 *
 * A espessura carrega o argumento — 6.5/5/4 na entrada contra 2.5/2 na saída.
 * Fora do portão nada tem matiz: as faixas são a mesma hairline que separa os
 * painéis do console, e o violeta aparece uma vez só, onde o dado é
 * normalizado. Nenhum gradiente, nenhum brilho, nenhum movimento.
 */
const GateMark: React.FC<{ className?: string }> = ({ className }) => {
  const { t } = useTranslation("auth")
  const lane = "var(--color-border-strong)"
  return (
    <svg
      viewBox="0 0 480 280"
      className={className}
      role="img"
      aria-label={t("brand.gateAriaLabel")}
      fill="none"
    >
      <g stroke={lane} strokeLinecap="round">
        <path d="M0 40 C 130 40 190 126 240 132" strokeWidth="6.5" />
        <path d="M0 140 C 130 140 190 140 240 140" strokeWidth="5" />
        <path d="M0 240 C 130 240 190 154 240 148" strokeWidth="4" />
        <path d="M240 136 C 300 136 350 78 480 68" strokeWidth="2.5" />
        <path d="M240 144 C 300 144 350 202 480 212" strokeWidth="2" />
      </g>
      <g stroke="var(--color-stage-normalize)" strokeWidth="2.5" strokeLinecap="round">
        <line x1="240" y1="24" x2="240" y2="119" />
        <line x1="240" y1="161" x2="240" y2="256" />
      </g>
      <text
        x="0"
        y="272"
        fontFamily="var(--font-mono)"
        fontSize="11"
        letterSpacing="0.16em"
        fill="var(--color-text-tertiary)"
      >
        {t("brand.sources").toUpperCase()}
      </text>
      <text
        x="480"
        y="272"
        textAnchor="end"
        fontFamily="var(--font-mono)"
        fontSize="11"
        letterSpacing="0.16em"
        fill="var(--color-text-tertiary)"
      >
        {t("brand.destinations").toUpperCase()}
      </text>
    </svg>
  )
}

export const LoginPage: React.FC = () => {
  const { t } = useTranslation("auth")
  const { login, bootstrapAdmin, setupRequired, companyName, companyPortalName, ssoEnabled, ssoButtonLabel } = useAuth()
  const [feedback, setFeedback] = useState<string | null>(null)
  const [searchParams] = useSearchParams()

  // Erros vindos do callback SSO chegam como ?sso_error=<code> na URL.
  useEffect(() => {
    const ssoError = searchParams.get("sso_error")
    if (ssoError) {
      setFeedback(t(`sso.${ssoError}`, { defaultValue: t("errors.ssoGeneric") }))
    }
  }, [searchParams, t])

  const { values, errors, touched, handleChange, handleBlur, handleSubmit, isSubmitting } = useForm({
    initialValues,
    validate: (v) => {
      const e: Partial<Record<keyof LoginFormValues, string>> = {}
      if (setupRequired && !v.display_name.trim()) e.display_name = t("validation.adminNameRequired")
      if (!v.username.trim()) e.username = t("validation.usernameRequired")
      if (!v.password.trim()) e.password = t("validation.passwordRequired")
      else if (setupRequired && v.password.trim().length < 10) e.password = t("validation.passwordWeak")
      if (setupRequired && v.confirm_password !== v.password) e.confirm_password = t("validation.passwordMismatch")
      return e
    },
    onSubmit: async (v) => {
      setFeedback(null)
      try {
        if (setupRequired) {
          await bootstrapAdmin({ display_name: v.display_name.trim(), username: v.username.trim(), password: v.password })
          return
        }
        await login({ username: v.username.trim(), password: v.password })
      } catch (error) {
        setFeedback(setupRequired ? (error instanceof Error ? error.message : t("errors.authFailed")) : t("errors.loginFailed"))
      }
    },
  })

  return (
    <main className="relative min-h-screen bg-surface-secondary">
      <div className="absolute right-4 top-4 z-10">
        <LanguageSwitcher />
      </div>

      <div className="mx-auto flex min-h-screen w-full max-w-6xl flex-col items-center justify-center gap-16 px-6 py-16 lg:flex-row lg:justify-between">
        <GateMark className="hidden w-full max-w-lg shrink lg:block" />

        <Card className="w-full max-w-md shrink-0 shadow-xl">
          <CardHeader>
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-xl bg-primary-600 flex items-center justify-center">
                <ShieldCheckIcon size={20} className="text-text-inverse" />
              </div>
              <div className="flex flex-col">
                <span className="font-mono text-xs uppercase tracking-wider text-text-tertiary">{companyName}</span>
                <span className="text-sm font-semibold text-text">{companyPortalName}</span>
              </div>
            </div>

            <CardTitle className="font-display text-2xl">{setupRequired ? t("setupTitle") : t("loginTitle")}</CardTitle>
            <CardDescription>
              {setupRequired ? t("setupDescription") : t("loginDescription")}
            </CardDescription>
          </CardHeader>
          <CardContent>
          <form onSubmit={handleSubmit} className="flex flex-col gap-4" noValidate>
            {setupRequired && (
              <Input
                name="display_name"
                label={t("fields.adminName")}
                placeholder={t("fields.adminNamePlaceholder")}
                value={values.display_name}
                onChange={handleChange}
                onBlur={handleBlur}
                error={touched.display_name ? errors.display_name : undefined}
                leftIcon={<UserIcon size={16} />}
                required
                disabled={isSubmitting}
              />
            )}

            <Input
              name="username"
              label={t("fields.username")}
              placeholder={t("fields.usernamePlaceholder")}
              value={values.username}
              onChange={handleChange}
              onBlur={handleBlur}
              error={touched.username ? errors.username : undefined}
              leftIcon={<UserIcon size={16} />}
              required
              disabled={isSubmitting}
            />

            <Input
              name="password"
              type="password"
              label={t("fields.password")}
              placeholder={setupRequired ? t("fields.passwordCreatePlaceholder") : t("fields.passwordEnterPlaceholder")}
              value={values.password}
              onChange={handleChange}
              onBlur={handleBlur}
              error={touched.password ? errors.password : undefined}
              leftIcon={<LockIcon size={16} />}
              required
              disabled={isSubmitting}
            />

            {setupRequired && (
              <Input
                name="confirm_password"
                type="password"
                label={t("fields.confirmPassword")}
                placeholder={t("fields.confirmPasswordPlaceholder")}
                value={values.confirm_password}
                onChange={handleChange}
                onBlur={handleBlur}
                error={touched.confirm_password ? errors.confirm_password : undefined}
                leftIcon={<KeyIcon size={16} />}
                required
                disabled={isSubmitting}
              />
            )}

            {feedback && (
              <p className="flex items-start gap-2 rounded-md border border-danger-100 bg-danger-50 px-3 py-2.5 text-sm text-danger-700" role="alert">
                <AlertTriangleIcon size={15} className="mt-0.5 shrink-0" aria-hidden="true" />
                <span>{feedback}</span>
              </p>
            )}

            <Button type="submit" loading={isSubmitting} className="w-full mt-2">
              {setupRequired ? t("submit.createAdmin") : t("submit.signIn")}
            </Button>
          </form>

          {ssoEnabled && !setupRequired && (
            <div className="mt-4">
              <div className="flex items-center gap-3 text-xs text-text-secondary" aria-hidden="true">
                <div className="h-px flex-1 bg-border" />
                <span>{t("divider")}</span>
                <div className="h-px flex-1 bg-border" />
              </div>
              <Button
                type="button"
                variant="outline"
                className="w-full mt-4"
                leftIcon={<MicrosoftIcon />}
                onClick={() => {
                  window.location.href = ssoLoginUrl()
                }}
              >
                {ssoButtonLabel}
              </Button>
            </div>
          )}
          </CardContent>
        </Card>
      </div>
    </main>
  )
}

export default LoginPage
