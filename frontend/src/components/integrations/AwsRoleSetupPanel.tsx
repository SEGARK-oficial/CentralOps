/**
 * AwsRoleSetupPanel — onboarding do papel AWS cross-account (AssumeRole).
 *
 * Mostra o que o cliente precisa para criar, na conta AWS dele, o papel
 * só-leitura que o CentralOps assume: o External ID da organização (gerado pelo
 * servidor, imutável), o principal do CentralOps em que a trust policy confia e
 * o template CloudFormation já preenchido. Escolhido pelo catálogo
 * (`auth_setup === "aws_assume_role"`), nunca pelo nome da plataforma.
 */
import { useCallback, useEffect, useMemo, useState } from "react"
import { CheckIcon, CopyIcon, DownloadIcon } from "lucide-react"
import { Trans, useTranslation } from "react-i18next"
import * as api from "@/services/api"
import type { AwsRoleSetup } from "@/types"
import { Button } from "@/components/ui/Button/Button"
import { Notice } from "@/components/ui/Notice/Notice"

export const AWS_ROLE_SETUP_KEY = "aws_assume_role"

const STACK_NAME = "centralops-readonly"

function CopyValue({ text, label }: { text: string; label: string }) {
  const { t } = useTranslation("integrations")
  const [copied, setCopied] = useState(false)
  const copy = useCallback(() => {
    void navigator.clipboard?.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }, [text])
  return (
    <Button type="button" variant="outline" size="xs" onClick={copy} aria-label={label}>
      {copied ? <CheckIcon size={14} /> : <CopyIcon size={14} />}
      {copied ? t("ingest.copied") : t("ingest.copy")}
    </Button>
  )
}

function ValueRow({ label, value, help }: { label: string; value: string; help?: string }) {
  return (
    <div className="space-y-1">
      <div className="text-xs font-medium text-text-secondary">{label}</div>
      <div className="flex items-center gap-2">
        <code className="min-w-0 flex-1 break-all rounded bg-surface-tertiary px-2 py-1 text-xs text-text">
          {value}
        </code>
        <CopyValue text={value} label={label} />
      </div>
      {help && <p className="text-xs text-text-tertiary">{help}</p>}
    </div>
  )
}

interface AwsRoleSetupPanelProps {
  organizationId: number | ""
}

export function AwsRoleSetupPanel({ organizationId }: AwsRoleSetupPanelProps) {
  const { t } = useTranslation("integrations")
  const [setup, setSetup] = useState<AwsRoleSetup | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setSetup(null)
    setError(null)
    if (!organizationId) return
    let cancelled = false
    setLoading(true)
    api
      .getAwsRoleSetup(Number(organizationId))
      .then((res) => {
        if (!cancelled) setSetup(res)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : t("form.awsRole.loadFailed"))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [organizationId, t])

  const cliCommand = useMemo(() => {
    const file = setup?.template_filename ?? "centralops-readonly-role.json"
    return [
      "aws cloudformation deploy \\",
      `  --template-file ${file} \\`,
      `  --stack-name ${STACK_NAME} \\`,
      "  --capabilities CAPABILITY_NAMED_IAM \\",
      "  --parameter-overrides CloudTrailBucketName=<bucket-do-cloudtrail>",
      "",
      `aws cloudformation describe-stacks --stack-name ${STACK_NAME} \\`,
      '  --query "Stacks[0].Outputs[?OutputKey==\'RoleArn\'].OutputValue" --output text',
    ].join("\n")
  }, [setup])

  const trustPolicy = useMemo(() => {
    if (!setup) return ""
    return JSON.stringify(
      {
        Version: "2012-10-17",
        Statement: [
          {
            Effect: "Allow",
            Principal: { AWS: setup.trust_principal_arn ?? "<ARN-do-principal-do-CentralOps>" },
            Action: "sts:AssumeRole",
            Condition: { StringEquals: { "sts:ExternalId": setup.external_id } },
          },
        ],
      },
      null,
      2,
    )
  }, [setup])

  const downloadTemplate = useCallback(() => {
    if (!setup) return
    const blob = new Blob([JSON.stringify(setup.template, null, 2)], { type: "application/json" })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = setup.template_filename
    a.click()
    URL.revokeObjectURL(url)
  }, [setup])

  return (
    <section
      aria-labelledby="aws-role-setup-title"
      className="mb-4 space-y-3 rounded-lg border border-border bg-surface p-4"
      data-testid="aws-role-setup"
    >
      <div>
        <h4 id="aws-role-setup-title" className="text-sm font-semibold text-text">
          {t("form.awsRole.title")}
        </h4>
        <p className="mt-1 text-xs text-text-secondary">{t("form.awsRole.intro")}</p>
      </div>

      {!organizationId && <Notice variant="info">{t("form.awsRole.selectOrganization")}</Notice>}
      {loading && <p className="text-xs text-text-secondary">{t("form.awsRole.loading")}</p>}
      {error && (
        <Notice variant="warning" title={t("form.awsRole.loadFailed")}>
          {error}
        </Notice>
      )}

      {setup && (
        <>
          <ValueRow
            label={t("form.awsRole.externalId")}
            value={setup.external_id}
            help={t("form.awsRole.externalIdHelp")}
          />
          {setup.trust_principal_arn ? (
            <ValueRow
              label={t("form.awsRole.principal")}
              value={setup.trust_principal_arn}
              help={t("form.awsRole.principalHelp")}
            />
          ) : (
            <Notice variant="warning" title={t("form.awsRole.principalMissingTitle")}>
              {t("form.awsRole.principalMissing")}
              {setup.principal_error && (
                <span className="mt-1 block text-xs text-text-secondary">{setup.principal_error}</span>
              )}
            </Notice>
          )}

          <ol className="list-decimal space-y-2 pl-5 text-xs text-text">
            <li>
              <Trans
                i18nKey="form.awsRole.step1"
                t={t}
                components={{ strong: <strong /> }}
              />
              <div className="mt-2 flex flex-wrap gap-2">
                <Button type="button" variant="outline" size="sm" onClick={downloadTemplate}>
                  <DownloadIcon size={14} />
                  {t("form.awsRole.downloadTemplate")}
                </Button>
              </div>
              <div className="mt-2 flex items-start gap-2">
                <pre className="min-w-0 flex-1 overflow-x-auto rounded bg-surface-tertiary p-2 text-[11px] leading-relaxed">
                  {cliCommand}
                </pre>
                <CopyValue text={cliCommand} label={t("form.awsRole.cliLabel")} />
              </div>
            </li>
            <li>
              <Trans
                i18nKey="form.awsRole.step2"
                t={t}
                components={{ strong: <strong />, code: <code className="rounded bg-surface-tertiary px-1" /> }}
              />
            </li>
            <li>{t("form.awsRole.step3")}</li>
          </ol>

          <details className="text-xs">
            <summary className="cursor-pointer text-text-secondary">{t("form.awsRole.manualSummary")}</summary>
            <p className="mt-2 text-text-secondary">{t("form.awsRole.manualHelp")}</p>
            <div className="mt-2 flex items-start gap-2">
              <pre className="min-w-0 flex-1 overflow-x-auto rounded bg-surface-tertiary p-2 text-[11px] leading-relaxed">
                {trustPolicy}
              </pre>
              <CopyValue text={trustPolicy} label={t("form.awsRole.manualSummary")} />
            </div>
          </details>

          <p className="text-xs text-text-tertiary">{t("form.awsRole.legacyNote")}</p>
        </>
      )}
    </section>
  )
}
