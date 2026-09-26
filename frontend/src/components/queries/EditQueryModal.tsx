"use client"

import type React from "react"
import { useEffect, useState } from "react"
import { SaveIcon, XIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import type { TFunction } from "i18next"
import * as api from "@/services/api"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"
import Select from "@/components/ui/Select/Select"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import { useForm } from "@/hooks/useForm"
import type {
  Client,
  Query,
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

function getSpecKindOptions(t: TFunction) {
  return [
    { value: "", label: t("queries:shared.specKindDefault") },
    { value: "passthrough", label: t("queries:shared.specKindPassthrough") },
    { value: "sigma", label: t("queries:shared.specKindSigma") },
  ]
}

interface EditQueryModalProps {
  query: Query | null
  clients: Client[]
  open: boolean
  onClose: () => void
  onSubmit: (data: Partial<Query>) => Promise<void>
  loading?: boolean
}

const validateForm = (t: TFunction, values: Partial<Query>) => {
  const errors: Partial<Record<keyof Query, string>> = {}

  if (!values.title?.trim()) {
    errors.title = t("queries:validation.titleRequired")
  }

  if (!values.statement?.trim()) {
    errors.statement = t("queries:validation.statementRequired")
  }

  return errors
}

export const EditQueryModal: React.FC<EditQueryModalProps> = ({
  query,
  clients,
  open,
  onClose,
  onSubmit,
  loading = false,
}) => {
  const { t } = useTranslation("queries")
  const {
    values,
    errors,
    touched,
    handleChange,
    handleBlur,
    handleSubmit,
    setFieldValue,
    resetForm,
    isSubmitting,
    submitError,
  } = useForm({
    initialValues: {
      title: "",
      description: "",
      statement: "",
      table: "xdr_index",
      client_ids: [] as number[],
      dialect: undefined as QueryDialect | undefined,
      spec_kind: undefined as QuerySpecKind | undefined,
      severity_id: DEFAULT_QUERY_SEVERITY as number,
      finding_shape: DEFAULT_QUERY_FINDING_SHAPE as QueryFindingShape,
    },
    validate: (v) => validateForm(t, v),
    onSubmit: async (formData) => {
      // O payload é allow-list COMPLETA: campo fora daqui volta ao default no
      // servidor. Os dois novos entram na mesma lista da hidratação abaixo.
      await onSubmit({
        title: formData.title,
        description: formData.description,
        statement: formData.statement,
        table: "xdr_index",
        client_ids: formData.client_ids,
        dialect: formData.dialect,
        spec_kind: formData.spec_kind,
        severity_id: formData.severity_id,
        finding_shape: formData.finding_shape,
      })
      onClose()
    },
  })

  const [capabilities, setCapabilities] = useState<QueryCapabilityRead[]>([])

  useEffect(() => {
    let cancelled = false
    api.listQueryCapabilities().then((data) => {
      if (!cancelled) setCapabilities(data)
    }).catch(() => {/* silencia: dialeto sem opções */})
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (open && query) {
      setFieldValue("title", query.title)
      setFieldValue("description", query.description || "")
      setFieldValue("statement", query.statement)
      setFieldValue("table", query.table || "xdr_index")
      setFieldValue("client_ids", query.client_ids || [])
      setFieldValue("dialect", query.dialect ?? undefined)
      setFieldValue("spec_kind", query.spec_kind ?? undefined)
      setFieldValue("severity_id", query.severity_id ?? DEFAULT_QUERY_SEVERITY)
      setFieldValue("finding_shape", query.finding_shape ?? DEFAULT_QUERY_FINDING_SHAPE)
      return
    }

    if (!open) {
      resetForm()
    }
  }, [open, query, resetForm, setFieldValue])

  const dialectOptions = [
    { value: "", label: t("queries:shared.dialectPlaceholder") },
    ...capabilities.map((cap) => ({
      value: cap.dialect,
      label: cap.dialect,
    })),
  ]

  if (!query) return null

  const formBusy = loading || isSubmitting
  const specKindOptions = getSpecKindOptions(t)
  const severityOptions = getQuerySeverityOptions(t)
  const findingShapeOptions = getQueryFindingShapeOptions(t)

  return (
    <Modal open={open} onClose={onClose} title={t("queries:editModal.title")} size="xl">
      <form onSubmit={handleSubmit} className="space-y-5" noValidate>
        {submitError && (
          <Notice variant="danger" title={t("queries:form.submitErrorFallback")}>
            {submitError}
          </Notice>
        )}
        <div className="grid gap-4 md:grid-cols-2">
          <div className="md:col-span-2">
            <Input
              name="title"
              label={t("queries:editForm.titleLabel")}
              value={values.title || ""}
              onChange={handleChange}
              onBlur={handleBlur}
              error={touched.title ? errors.title : undefined}
              required
              disabled={formBusy}
            />
          </div>

          <div className="md:col-span-2">
            <Input
              name="description"
              label={t("queries:editForm.descriptionLabel")}
              value={values.description || ""}
              onChange={handleChange}
              onBlur={handleBlur}
              error={touched.description ? errors.description : undefined}
              disabled={formBusy}
            />
          </div>

          <div className="md:col-span-2">
            <Textarea
              id="edit-query-statement"
              name="statement"
              label={t("queries:editForm.statementLabel")}
              value={values.statement || ""}
              onChange={handleChange}
              onBlur={handleBlur}
              error={touched.statement ? errors.statement : undefined}
              helperText={t("queries:editForm.statementHelper")}
              required
              rows={8}
              disabled={formBusy}
            />
          </div>

          <div className="md:col-span-2">
            <Select
              label={t("queries:shared.clientsLabel")}
              multiple
              value={values.client_ids || []}
              options={clients.map((client) => ({
                value: client.id,
                label: client.region ? `${client.name} (${client.region})` : client.name,
              }))}
              onChange={(value) => setFieldValue("client_ids", Array.isArray(value) ? value.map(Number) : [])}
              helperText={t("queries:editForm.clientsHelper")}
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
              helperText={t("queries:editForm.dialectHelper")}
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

        <Notice variant="info" title={t("queries:editForm.reuseNoticeTitle")}>
          {t("queries:editForm.reuseNoticeBody")}
        </Notice>

        <div className="flex flex-wrap justify-end gap-3">
          <Button type="button" variant="outline" onClick={onClose} disabled={formBusy} leftIcon={<XIcon size={16} />}>
            {t("queries:shared.cancel")}
          </Button>
          <Button type="submit" loading={formBusy} leftIcon={<SaveIcon size={16} />}>
            {t("queries:editForm.submit")}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
