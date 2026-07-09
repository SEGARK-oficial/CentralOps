"use client"

import type React from "react"
import { useEffect, useState } from "react"
import { useSearchParams } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { ShieldCheckIcon, LockIcon, UserIcon, KeyIcon } from "lucide-react"
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
    <main className="min-h-screen flex items-center justify-center bg-gradient-to-br from-sidebar via-primary-900 to-sidebar p-4">
      {/* Glow effects */}
      <div className="absolute inset-0 overflow-hidden pointer-events-none" aria-hidden="true">
        <div className="absolute top-1/4 -left-20 w-72 h-72 bg-primary-500/20 rounded-full blur-3xl" />
        <div className="absolute bottom-1/4 -right-20 w-72 h-72 bg-primary-400/10 rounded-full blur-3xl" />
      </div>

      <div className="absolute right-4 top-4 z-10">
        <LanguageSwitcher />
      </div>

      <Card className="relative w-full max-w-md shadow-2xl border-border/50">
        <CardHeader>
          <div className="flex items-center gap-3 mb-4">
            <div className="w-10 h-10 rounded-xl bg-primary-600 flex items-center justify-center">
              <ShieldCheckIcon size={20} className="text-white" />
            </div>
            <div className="flex flex-col">
              <span className="text-xs font-medium text-text-secondary uppercase tracking-wider">{companyName}</span>
              <span className="text-sm font-semibold text-text">{companyPortalName}</span>
            </div>
          </div>

          <CardTitle>{setupRequired ? t("setupTitle") : t("loginTitle")}</CardTitle>
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
              <div className="flex items-start gap-2 p-3 rounded-md bg-danger-50 border border-danger-100 text-danger-700 text-sm" role="alert">
                <LockIcon size={16} className="shrink-0 mt-0.5" />
                <div>
                  <strong className="block font-semibold">{t("errors.cannotAuthenticate")}</strong>
                  <p>{feedback}</p>
                </div>
              </div>
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
    </main>
  )
}

export default LoginPage
