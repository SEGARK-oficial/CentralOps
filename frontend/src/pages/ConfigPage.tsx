import type React from "react"
import { useEffect, useState } from "react"
import { CrownIcon, ExternalLinkIcon, KeyRoundIcon, MailIcon, RadioIcon, SettingsIcon, ShieldCheckIcon, ZapIcon } from "lucide-react"
import { Link } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { CapturePanel } from "@/components/config/CapturePanel"
import { EditionInfoCard } from "@/components/config/EditionInfoCard"
import { CollectorConfigForm } from "@/components/config/CollectorConfigForm"
import { EmailConfigForm } from "@/components/config/EmailConfigForm"
import { IdentityConfigForm } from "@/components/config/IdentityConfigForm"
import { LicenseActivationForm } from "@/components/config/LicenseActivationForm"
import { Badge } from "@/components/ui/Badge/Badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card/Card"
import { Notice } from "@/components/ui/Notice/Notice"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { Tabs, TabsList, TabsPanel, TabsTrigger } from "@/components/ui/Tabs/Tabs"
import { useCollectorConfig } from "@/hooks/useCollectorConfig"
import { useEmailConfig } from "@/hooks/useEmailConfig"
import { useIdentityConfig } from "@/hooks/useIdentityConfig"
import * as api from "@/services/api"

type ConfigTab = "email" | "collector" | "identity" | "capture" | "licensing"

export const ConfigPage: React.FC = () => {
  const { t } = useTranslation("config")
  const [tab, setTab] = useState<ConfigTab>("email")
  const [activeDestCount, setActiveDestCount] = useState<number | null>(null)

  useEffect(() => {
    let cancelled = false
    api.listDestinations({ include_disabled: false })
      .then((dests) => { if (!cancelled) setActiveDestCount(dests.length) })
      .catch(() => { /* falha não-fatal — card mostra "—" */ })
    return () => { cancelled = true }
  }, [])

  const {
    config,
    recipients,
    loading,
    saving,
    testing,
    addingRecipient,
    removingRecipientId,
    error,
    feedback,
    saveConfig,
    addRecipient,
    removeRecipient,
    sendTest,
  } = useEmailConfig()

  const {
    config: collectorConfig,
    loading: collectorLoading,
    saving: collectorSaving,
    testing: collectorTesting,
    testResult: collectorTestResult,
    error: collectorError,
    feedback: collectorFeedback,
    saveConfig: saveCollectorConfig,
    runTest: runCollectorTest,
  } = useCollectorConfig()

  const {
    config: identityConfig,
    loading: identityLoading,
    saving: identitySaving,
    testing: identityTesting,
    error: identityError,
    feedback: identityFeedback,
    saveConfig: saveIdentityConfig,
    testConnection: testIdentityConnection,
  } = useIdentityConfig()

  return (
    <div className="space-y-6">
      <PageHeader
        icon={<SettingsIcon size={24} />}
        eyebrow={t("page.eyebrow")}
        title={t("page.title")}
        description={t("page.description")}
      />

      {/* ── Edição / licença ───────────────────────────── */}
      <EditionInfoCard />

      {/* ── Stat cards ────────────────────────────────────────────── */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card padding="sm" className="shadow-sm">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{t("page.stats.recipients")}</div>
              <div className="mt-2 text-2xl font-bold text-text">{recipients.length}</div>
            </div>
            <Badge variant="primary" size="lg" className="gap-1.5">
              <MailIcon size={14} />
              {recipients.length}
            </Badge>
          </div>
        </Card>

        <Card padding="sm" className="shadow-sm">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{t("page.stats.tlsSmtp")}</div>
              <div className="mt-2 text-2xl font-bold text-text">{config?.use_tls ? t("page.stats.tlsActive") : t("page.stats.tlsOptional")}</div>
            </div>
            <Badge variant={config?.use_tls ? "success" : "outline"} size="lg">
              {config?.use_tls ? t("page.stats.tlsSecure") : t("page.stats.tlsFree")}
            </Badge>
          </div>
        </Card>

        <Card padding="sm" className="shadow-sm">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{t("page.stats.smtpPassword")}</div>
              <div className="mt-2 text-2xl font-bold text-text">{config?.smtp_password_configured ? t("page.stats.smtpPasswordSaved") : t("page.stats.smtpPasswordNotSaved")}</div>
            </div>
            <Badge variant={config?.smtp_password_configured ? "success" : "warning"} size="lg" className="gap-1.5">
              <ShieldCheckIcon size={14} />
              {config?.smtp_password_configured ? t("page.stats.smtpPasswordProtected") : t("page.stats.smtpPasswordPending")}
            </Badge>
          </div>
        </Card>

        <Card padding="sm" className="shadow-sm">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{t("page.stats.activeDestinations")}</div>
              <div className="mt-2 text-2xl font-bold text-text">
                {activeDestCount !== null ? activeDestCount : "—"}
              </div>
              <Link
                to="/destinations"
                className="mt-1 inline-flex items-center gap-1 text-xs text-primary-600 hover:underline"
              >
                {t("page.stats.viewDestinations")}
                <ExternalLinkIcon size={11} />
              </Link>
            </div>
            <Badge variant="primary" size="lg" className="gap-1.5">
              <ZapIcon size={14} />
              {activeDestCount !== null ? activeDestCount : "—"}
            </Badge>
          </div>
        </Card>
      </div>

      {/* ── Abas (Email / Collector) ──────────────────────────────── */}
      <Tabs value={tab} onValueChange={(v) => setTab(v as ConfigTab)}>
        <TabsList ariaLabel={t("page.tabs.ariaLabel")}>
          <TabsTrigger value="email" icon={<MailIcon size={16} />}>
            {t("page.tabs.email")}
          </TabsTrigger>
          <TabsTrigger value="collector" icon={<ZapIcon size={16} />}>
            {t("page.tabs.collector")}
          </TabsTrigger>
          <TabsTrigger value="identity" icon={<KeyRoundIcon size={16} />}>
            {t("page.tabs.identity")}
          </TabsTrigger>
          <TabsTrigger value="capture" icon={<RadioIcon size={16} />}>
            {t("page.tabs.capture")}
          </TabsTrigger>
          <TabsTrigger value="licensing" icon={<CrownIcon size={16} />}>
            {t("page.tabs.licensing")}
          </TabsTrigger>
        </TabsList>

        <TabsPanel value="email">
          {error && (
            <Notice variant="danger" title={t("page.email.loadError")}>
              {error}
            </Notice>
          )}
          <Card className="shadow-sm">
            <CardHeader>
              <CardTitle>{t("page.email.cardTitle")}</CardTitle>
              <CardDescription>
                {t("page.email.cardDescription")}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <EmailConfigForm
                config={config}
                recipients={recipients}
                loading={loading}
                saving={saving}
                testing={testing}
                addingRecipient={addingRecipient}
                removingRecipientId={removingRecipientId}
                feedback={feedback}
                onSave={saveConfig}
                onAdd={addRecipient}
                onDelete={removeRecipient}
                onTest={sendTest}
              />
            </CardContent>
          </Card>
        </TabsPanel>

        <TabsPanel value="collector">
          {collectorError && (
            <Notice variant="danger" title={t("page.collector.loadError")}>
              {collectorError}
            </Notice>
          )}
          <Card className="shadow-sm">
            <CardHeader>
              <CardTitle>{t("page.tabs.collector")}</CardTitle>
              <CardDescription>
                {t("page.collector.cardDescription")}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <CollectorConfigForm
                config={collectorConfig}
                loading={collectorLoading}
                saving={collectorSaving}
                testing={collectorTesting}
                testResult={collectorTestResult}
                feedback={collectorFeedback}
                onSave={saveCollectorConfig}
                onTest={runCollectorTest}
              />
            </CardContent>
          </Card>
        </TabsPanel>

        <TabsPanel value="identity">
          {identityError && (
            <Notice variant="danger" title={t("page.identity.loadError")}>
              {identityError}
            </Notice>
          )}
          <Card className="shadow-sm">
            <CardHeader>
              <CardTitle>{t("page.identity.cardTitle")}</CardTitle>
              <CardDescription>
                {t("page.identity.cardDescription")}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <IdentityConfigForm
                config={identityConfig}
                loading={identityLoading}
                saving={identitySaving}
                testing={identityTesting}
                feedback={identityFeedback}
                onSave={saveIdentityConfig}
                onTest={testIdentityConnection}
              />
            </CardContent>
          </Card>
        </TabsPanel>

        <TabsPanel value="capture">
          <Card className="shadow-sm">
            <CardHeader>
              <CardTitle>{t("page.capture.cardTitle")}</CardTitle>
              <CardDescription>
                {t("page.capture.cardDescription")}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <CapturePanel />
            </CardContent>
          </Card>
        </TabsPanel>

        <TabsPanel value="licensing">
          <LicenseActivationForm />
        </TabsPanel>
      </Tabs>
    </div>
  )
}

export default ConfigPage
