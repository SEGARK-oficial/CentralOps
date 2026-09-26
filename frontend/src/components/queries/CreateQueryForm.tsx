"use client"

import type React from "react"
import { useEffect, useState } from "react"
import { CodeIcon, FileTextIcon, Link2Icon, PlusIcon, XIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import type { TFunction } from "i18next"
import * as api from "@/services/api"
import { useForm } from "@/hooks/useForm"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Notice } from "@/components/ui/Notice/Notice"
import Select from "@/components/ui/Select/Select"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import type {
  Client,
  CreateQueryRequest,
  QueryCapabilityRead,
  QueryDialect,
  QueryFindingShape,
  QuerySpecKind,
} from "@/types"
import {
  DEFAULT_QUERY_FINDING_SHAPE,
  DEFAULT_QUERY_SEVERITY,
  getQueryFindingShapeHelp,
  getQueryFindingShapeOptions,
  getQuerySeverityOptions,
} from "./queryOptions"

interface CreateQueryFormProps {
  clients: Client[]
  onSubmit: (data: CreateQueryRequest) => Promise<void>
  onCancel: () => void
  loading?: boolean
}

const initialValues: CreateQueryRequest = {
  title: "",
  description: "",
  statement: "",
  table: "xdr_index",
  client_ids: [],
  dialect: undefined,
  spec_kind: undefined,
  severity_id: DEFAULT_QUERY_SEVERITY,
  finding_shape: DEFAULT_QUERY_FINDING_SHAPE,
}

function getSpecKindOptions(t: TFunction) {
  return [
    { value: "", label: t("queries:shared.specKindDefault") },
    { value: "passthrough", label: t("queries:shared.specKindPassthrough") },
    { value: "sigma", label: t("queries:shared.specKindSigma") },
  ]
}

const validateForm = (t: TFunction, values: CreateQueryRequest) => {
  const errors: Partial<Record<keyof CreateQueryRequest, string>> = {}

  if (!values.title.trim()) {
    errors.title = t("queries:validation.titleRequired")
  } else if (values.title.trim().length < 3) {
    errors.title = t("queries:validation.titleMinLength")
  }

  if (!values.statement.trim()) {
    errors.statement = t("queries:validation.statementRequired")
  }

  return errors
}

export const CreateQueryForm: React.FC<CreateQueryFormProps> = ({ clients, onSubmit, onCancel, loading = false }) => {
  const { t } = useTranslation("queries")
  const { values, errors, touched, handleChange, handleBlur, handleSubmit, setFieldValue, isSubmitting, submitError } = useForm({
    initialValues,
    validate: (v) => validateForm(t, v),
    onSubmit,
  })

  const [capabilities, setCapabilities] = useState<QueryCapabilityRead[]>([])

  useEffect(() => {
    let cancelled = false
    api.listQueryCapabilities().then((data) => {
      if (!cancelled) setCapabilities(data)
    }).catch(() => {/* silencia: o campo fica com lista vazia */})
    return () => { cancelled = true }
  }, [])

  const dialectOptions = [
    { value: "", label: t("queries:shared.dialectPlaceholder") },
    ...capabilities.map((cap) => ({
      value: cap.dialect,
      label: cap.dialect,
    })),
  ]

  const formBusy = loading || isSubmitting
  const specKindOptions = getSpecKindOptions(t)
  const severityOptions = getQuerySeverityOptions(t)
  const findingShapeOptions = getQueryFindingShapeOptions(t)

  return (
    <form onSubmit={handleSubmit} className="space-y-5" noValidate>
      {/* useForm: rede de segurança — se `onSubmit` rejeitar sem o chamador
          tratar o próprio erro, isto garante que ALGO aparece na tela em vez
          de só um console.error mudo. */}
      {submitError && (
        <Notice variant="danger" title={t("queries:form.submitErrorFallback")}>
          {submitError}
        </Notice>
      )}
      <div className="grid gap-4 md:grid-cols-2">
        <div className="md:col-span-2">
          <Input
            name="title"
            label={t("queries:createForm.titleLabel")}
            placeholder={t("queries:createForm.titlePlaceholder")}
            value={values.title}
            onChange={handleChange}
            onBlur={handleBlur}
            error={touched.title ? errors.title : undefined}
            leftIcon={<FileTextIcon size={16} />}
            required
            disabled={formBusy}
          />
        </div>

        <div className="md:col-span-2">
          <Input
            name="description"
            label={t("queries:createForm.descriptionLabel")}
            placeholder={t("queries:createForm.descriptionPlaceholder")}
            value={values.description}
            onChange={handleChange}
            onBlur={handleBlur}
            error={touched.description ? errors.description : undefined}
            leftIcon={<Link2Icon size={16} />}
            disabled={formBusy}
          />
        </div>

        <div className="md:col-span-2">
          <Textarea
            id="create-query-statement"
            name="statement"
            label={t("queries:createForm.statementLabel")}
            placeholder={t("queries:createForm.statementPlaceholder")}
            value={values.statement}
            onChange={handleChange}
            onBlur={handleBlur}
            error={touched.statement ? errors.statement : undefined}
            helperText={t("queries:createForm.statementHelper")}
            required
            rows={7}
            disabled={formBusy}
          />
        </div>

        <div className="md:col-span-2">
          <Select
            label={t("queries:shared.clientsLabel")}
            multiple
            options={clients.map((client) => ({
              value: client.id,
              label: client.region ? `${client.name} (${client.region})` : client.name,
            }))}
            value={values.client_ids || []}
            onChange={(value) => setFieldValue("client_ids", Array.isArray(value) ? value.map(Number) : [])}
            placeholder={t("queries:createForm.clientsPlaceholder")}
            helperText={t("queries:createForm.clientsHelper")}
            disabled={formBusy}
          />
        </div>

        <div>
          <Select
            label={t("queries:shared.dialectLabel")}
            options={dialectOptions}
            value={values.dialect ?? ""}
            onChange={(value) =>
              setFieldValue("dialect", value === "" ? undefined : (value as QueryDialect))
            }
            placeholder={t("queries:shared.dialectPlaceholder")}
            helperText={t("queries:createForm.dialectHelper")}
            disabled={formBusy || capabilities.length === 0}
          />
        </div>

        <div>
          <Select
            label={t("queries:shared.specKindLabel")}
            options={specKindOptions}
            value={values.spec_kind ?? ""}
            onChange={(value) =>
              setFieldValue("spec_kind", value === "" ? undefined : (value as QuerySpecKind))
            }
            placeholder={t("queries:shared.specKindPlaceholder")}
            helperText={t("queries:shared.specKindHelper")}
            disabled={formBusy}
          />
        </div>

        <div>
          <Select
            label={t("queries:shared.severityLabel")}
            options={severityOptions}
            value={values.severity_id ?? DEFAULT_QUERY_SEVERITY}
            onChange={(value) => setFieldValue("severity_id", Number(value))}
            helperText={t("queries:shared.severityHelper")}
            disabled={formBusy}
          />
        </div>

        <div>
          <Select
            label={t("queries:shared.findingShapeLabel")}
            options={findingShapeOptions}
            value={values.finding_shape ?? DEFAULT_QUERY_FINDING_SHAPE}
            onChange={(value) => setFieldValue("finding_shape", value as QueryFindingShape)}
            helperText={getQueryFindingShapeHelp(t)}
            disabled={formBusy}
          />
        </div>
      </div>

      <Notice variant="info" title={t("queries:createForm.bestPracticesTitle")} icon={<CodeIcon size={16} />}>
        {t("queries:createForm.bestPracticesBody")}
      </Notice>

      <div className="flex flex-wrap justify-end gap-3">
        <Button type="button" variant="outline" onClick={onCancel} disabled={formBusy} leftIcon={<XIcon size={16} />}>
          {t("queries:shared.cancel")}
        </Button>
        <Button type="submit" loading={formBusy} leftIcon={<PlusIcon size={16} />}>
          {t("queries:createForm.submit")}
        </Button>
      </div>
    </form>
  )
}
