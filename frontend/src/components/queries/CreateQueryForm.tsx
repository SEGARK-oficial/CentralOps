"use client"

import type React from "react"
import { useEffect, useState } from "react"
import { CodeIcon, FileTextIcon, Link2Icon, PlusIcon, XIcon } from "lucide-react"
import * as api from "@/services/api"
import { useForm } from "@/hooks/useForm"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Notice } from "@/components/ui/Notice/Notice"
import Select from "@/components/ui/Select/Select"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import type { Client, CreateQueryRequest, QueryCapabilityRead, QueryDialect, QuerySpecKind } from "@/types"

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
}

const SPEC_KIND_OPTIONS = [
  { value: "", label: "Padrão (passthrough)" },
  { value: "passthrough", label: "Passthrough" },
  { value: "sigma", label: "Sigma" },
]

const validateForm = (values: CreateQueryRequest) => {
  const errors: Partial<Record<keyof CreateQueryRequest, string>> = {}

  if (!values.title.trim()) {
    errors.title = "Título é obrigatório"
  } else if (values.title.trim().length < 3) {
    errors.title = "Use pelo menos 3 caracteres"
  }

  if (!values.statement.trim()) {
    errors.statement = "A consulta SQL é obrigatória"
  }

  return errors
}

export const CreateQueryForm: React.FC<CreateQueryFormProps> = ({ clients, onSubmit, onCancel, loading = false }) => {
  const { values, errors, touched, handleChange, handleBlur, handleSubmit, setFieldValue, isSubmitting } = useForm({
    initialValues,
    validate: validateForm,
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
    { value: "", label: "Sem dialeto específico" },
    ...capabilities.map((cap) => ({
      value: cap.dialect,
      label: cap.dialect,
    })),
  ]

  const formBusy = loading || isSubmitting

  return (
    <form onSubmit={handleSubmit} className="space-y-5" noValidate>
      <div className="grid gap-4 md:grid-cols-2">
        <div className="md:col-span-2">
          <Input
            name="title"
            label="Título da query"
            placeholder="Ex: Processos suspeitos via PowerShell"
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
            label="Descrição"
            placeholder="Contexto, objetivo ou análise esperada"
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
            label="Query SQL"
            placeholder="SELECT * FROM xdr_data WHERE process_name = 'cmd.exe' LIMIT 100"
            value={values.statement}
            onChange={handleChange}
            onBlur={handleBlur}
            error={touched.statement ? errors.statement : undefined}
            helperText="Essa consulta pode ser reutilizada na tela de busca e em agendamentos."
            required
            rows={7}
            disabled={formBusy}
          />
        </div>

        <div className="md:col-span-2">
          <Select
            label="Clientes padrão"
            multiple
            options={clients.map((client) => ({
              value: client.id,
              label: client.region ? `${client.name} (${client.region})` : client.name,
            }))}
            value={values.client_ids || []}
            onChange={(value) => setFieldValue("client_ids", Array.isArray(value) ? value.map(Number) : [])}
            placeholder="Selecione clientes para pré-preencher essa query"
            helperText="Opcional. Ajuda a abrir a consulta com tenants já selecionados."
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
            helperText="Dialeto de query suportado pela plataforma (ex.: opensearch_dsl, fql)."
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

      <Notice variant="info" title="Boas práticas de consulta" icon={<CodeIcon size={16} />}>
        Use filtros específicos e limite de registros para facilitar a leitura e reduzir custo operacional.
      </Notice>

      <div className="flex flex-wrap justify-end gap-3">
        <Button type="button" variant="outline" onClick={onCancel} disabled={formBusy} leftIcon={<XIcon size={16} />}>
          Cancelar
        </Button>
        <Button type="submit" loading={formBusy} leftIcon={<PlusIcon size={16} />}>
          Criar query
        </Button>
      </div>
    </form>
  )
}
