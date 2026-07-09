"use client"

import type React from "react"
import { useEffect, useState } from "react"
import { SaveIcon, XIcon } from "lucide-react"
import * as api from "@/services/api"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"
import Select from "@/components/ui/Select/Select"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import { useForm } from "@/hooks/useForm"
import type { Client, Query, QueryCapabilityRead, QueryDialect, QuerySpecKind } from "@/types"

const SPEC_KIND_OPTIONS = [
  { value: "", label: "Padrão (passthrough)" },
  { value: "passthrough", label: "Passthrough" },
  { value: "sigma", label: "Sigma" },
]

interface EditQueryModalProps {
  query: Query | null
  clients: Client[]
  open: boolean
  onClose: () => void
  onSubmit: (data: Partial<Query>) => Promise<void>
  loading?: boolean
}

const validateForm = (values: Partial<Query>) => {
  const errors: Partial<Record<keyof Query, string>> = {}

  if (!values.title?.trim()) {
    errors.title = "Título é obrigatório"
  }

  if (!values.statement?.trim()) {
    errors.statement = "A consulta SQL é obrigatória"
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
  } = useForm({
    initialValues: {
      title: "",
      description: "",
      statement: "",
      table: "xdr_index",
      client_ids: [] as number[],
      dialect: undefined as QueryDialect | undefined,
      spec_kind: undefined as QuerySpecKind | undefined,
    },
    validate: validateForm,
    onSubmit: async (formData) => {
      await onSubmit({
        title: formData.title,
        description: formData.description,
        statement: formData.statement,
        table: "xdr_index",
        client_ids: formData.client_ids,
        dialect: formData.dialect,
        spec_kind: formData.spec_kind,
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
      return
    }

    if (!open) {
      resetForm()
    }
  }, [open, query, resetForm, setFieldValue])

  const dialectOptions = [
    { value: "", label: "Sem dialeto específico" },
    ...capabilities.map((cap) => ({
      value: cap.dialect,
      label: cap.dialect,
    })),
  ]

  if (!query) return null

  const formBusy = loading || isSubmitting

  return (
    <Modal open={open} onClose={onClose} title="Editar query" size="xl">
      <form onSubmit={handleSubmit} className="space-y-5" noValidate>
        <div className="grid gap-4 md:grid-cols-2">
          <div className="md:col-span-2">
            <Input
              name="title"
              label="Titulo"
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
              label="Descrição"
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
              label="Query SQL"
              value={values.statement || ""}
              onChange={handleChange}
              onBlur={handleBlur}
              error={touched.statement ? errors.statement : undefined}
              helperText="Atualize a consulta e ajuste os clientes padrão, se necessário."
              required
              rows={8}
              disabled={formBusy}
            />
          </div>

          <div className="md:col-span-2">
            <Select
              label="Clientes padrão"
              multiple
              value={values.client_ids || []}
              options={clients.map((client) => ({
                value: client.id,
                label: client.region ? `${client.name} (${client.region})` : client.name,
              }))}
              onChange={(value) => setFieldValue("client_ids", Array.isArray(value) ? value.map(Number) : [])}
              helperText="Opcional. Define os tenants sugeridos ao reutilizar esta query."
              disabled={formBusy}
            />
          </div>

          <div>
            <Select
              label="Dialeto (opcional)"
              options={dialectOptions}
              value={values.dialect ?? ""}
              onChange={(value) =>
                setFieldValue("dialect", value === "" ? undefined : (value as QueryDialect))
              }
              placeholder="Sem dialeto específico"
              helperText="Dialeto de query da plataforma (ex.: opensearch_dsl, fql)."
              disabled={formBusy || capabilities.length === 0}
            />
          </div>

          <div>
            <Select
              label="Spec kind (opcional)"
              options={SPEC_KIND_OPTIONS}
              value={values.spec_kind ?? ""}
              onChange={(value) =>
                setFieldValue("spec_kind", value === "" ? undefined : (value as QuerySpecKind))
              }
              placeholder="Padrão (passthrough)"
              helperText="passthrough = query literal; sigma = tradução automática via pySigma."
              disabled={formBusy}
            />
          </div>
        </div>

        <Notice variant="info" title="Reuso operacional">
          Alterações aqui impactam a tela de busca e novos agendamentos criados a partir desta consulta.
        </Notice>

        <div className="flex flex-wrap justify-end gap-3">
          <Button type="button" variant="outline" onClick={onClose} disabled={formBusy} leftIcon={<XIcon size={16} />}>
            Cancelar
          </Button>
          <Button type="submit" loading={formBusy} leftIcon={<SaveIcon size={16} />}>
            Salvar alterações
          </Button>
        </div>
      </form>
    </Modal>
  )
}
