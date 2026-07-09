"use client"

import type React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { RefreshCcwIcon, ShieldCheckIcon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Notice } from "@/components/ui/Notice/Notice"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { Select } from "@/components/ui/Select/Select"
import * as api from "@/services/api"
import type { OcsfCompliance, OcsfEnforcementMode, OcsfPolicy } from "@/types"

const MODES: OcsfEnforcementMode[] = ["tag_and_pass", "quarantine", "fail_closed"]

const MODE_BADGE: Record<OcsfEnforcementMode, "default" | "warning" | "danger"> = {
  tag_and_pass: "default",
  quarantine: "warning",
  fail_closed: "danger",
}

export const OcsfGovernancePage: React.FC = () => {
  const { t } = useTranslation("ocsf")

  const [policies, setPolicies] = useState<OcsfPolicy[]>([])
  const [compliance, setCompliance] = useState<OcsfCompliance | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [savingOrgId, setSavingOrgId] = useState<number | null>(null)
  const [feedback, setFeedback] = useState<{ type: "success" | "error"; message: string } | null>(null)

  const modeLabel = useCallback((m: OcsfEnforcementMode) => t(`modes.${m}`), [t])
  const modeOptions = useMemo(
    () => MODES.map((m) => ({ value: m, label: modeLabel(m) })),
    [modeLabel],
  )

  const load = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const [pol, comp] = await Promise.all([api.listOcsfPolicies(), api.getOcsfCompliance()])
      setPolicies(pol)
      setCompliance(comp)
    } catch {
      setError(t("loadError"))
    } finally {
      setIsLoading(false)
    }
  }, [t])

  useEffect(() => {
    void load()
  }, [load])

  const handleModeChange = useCallback(
    async (orgId: number, mode: OcsfEnforcementMode) => {
      setSavingOrgId(orgId)
      setFeedback(null)
      try {
        const updated = await api.setOcsfPolicy(orgId, mode)
        setPolicies((prev) =>
          prev.map((p) => (p.organization_id === orgId ? updated : p)),
        )
        setFeedback({ type: "success", message: t("policies.saved") })
      } catch {
        setFeedback({ type: "error", message: t("policies.saveError") })
      } finally {
        setSavingOrgId(null)
      }
    },
    [t],
  )

  return (
    <div className="space-y-6">
      <PageHeader
        title={t("title")}
        description={t("subtitle")}
        actions={
          <Button variant="secondary" leftIcon={<RefreshCcwIcon size={16} />} onClick={() => void load()}>
            {t("refresh")}
          </Button>
        }
      />

      {error && <Notice variant="danger">{error}</Notice>}
      {feedback && (
        <Notice variant={feedback.type === "success" ? "success" : "danger"}>
          {feedback.message}
        </Notice>
      )}

      {isLoading ? (
        <div className="flex justify-center py-12">
          <LoadingSpinner />
        </div>
      ) : (
        <>
          {/* Compliance summary + table */}
          <Card>
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold text-text">{t("compliance.title")}</h2>
                <p className="text-sm text-text-secondary">{t("compliance.description")}</p>
              </div>
              {compliance && (
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={compliance.validation_enabled ? "success" : "outline"} dot>
                    {compliance.validation_enabled
                      ? t("compliance.status.enabled")
                      : t("compliance.status.disabled")}
                  </Badge>
                  <Badge variant="outline">
                    {t("compliance.status.version", { version: compliance.ocsf_version })}
                  </Badge>
                  <Badge variant={MODE_BADGE[compliance.global_default]}>
                    {t("compliance.status.globalDefault", { mode: modeLabel(compliance.global_default) })}
                  </Badge>
                </div>
              )}
            </div>

            {compliance && !compliance.validation_enabled && (
              <Notice variant="info" className="mb-4">
                {t("compliance.status.disabledHint")}
              </Notice>
            )}

            {!compliance || compliance.items.length === 0 ? (
              <EmptyState icon={<ShieldCheckIcon size={28} />} title={t("compliance.empty")} />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-text-secondary">
                      <th className="py-2 pr-4 font-medium">{t("compliance.columns.integration")}</th>
                      <th className="py-2 pr-4 font-medium">{t("compliance.columns.mode")}</th>
                      <th className="py-2 pr-4 font-medium text-right">{t("compliance.columns.invalid")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {compliance.items.map((it) => (
                      <tr key={it.integration_id} className="border-b border-border/60">
                        <td className="py-2 pr-4 text-text">
                          {it.integration_name ?? `#${it.integration_id}`}
                        </td>
                        <td className="py-2 pr-4">
                          <Badge variant={MODE_BADGE[it.enforcement_mode]} size="sm">
                            {modeLabel(it.enforcement_mode)}
                          </Badge>
                        </td>
                        <td className="py-2 pr-4 text-right tabular-nums">
                          {it.invalid_quarantined_24h > 0 ? (
                            <Badge variant="danger" size="sm">
                              {it.invalid_quarantined_24h}
                            </Badge>
                          ) : (
                            <span className="text-text-secondary">0</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          {/* Per-org enforcement policy */}
          <Card>
            <div className="mb-4">
              <h2 className="text-lg font-semibold text-text">{t("policies.title")}</h2>
              <p className="text-sm text-text-secondary">{t("policies.description")}</p>
            </div>

            {policies.length === 0 ? (
              <EmptyState icon={<ShieldCheckIcon size={28} />} title={t("policies.empty")} />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-text-secondary">
                      <th className="py-2 pr-4 font-medium">{t("policies.columns.organization")}</th>
                      <th className="py-2 pr-4 font-medium">{t("policies.columns.mode")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {policies.map((p) => (
                      <tr key={p.organization_id} className="border-b border-border/60">
                        <td className="py-2 pr-4">
                          <span className="text-text">
                            {p.organization_name ?? `#${p.organization_id}`}
                          </span>
                          {p.is_default && (
                            <Badge variant="outline" size="sm" className="ml-2">
                              {t("policies.default")}
                            </Badge>
                          )}
                        </td>
                        <td className="py-2 pr-4">
                          <Select
                            options={modeOptions}
                            value={p.enforcement_mode}
                            disabled={savingOrgId === p.organization_id}
                            size="sm"
                            aria-label={t("policies.columns.mode")}
                            data-testid={`ocsf-mode-${p.organization_id}`}
                            onChange={(value) =>
                              void handleModeChange(p.organization_id, value as OcsfEnforcementMode)
                            }
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  )
}

export default OcsfGovernancePage
