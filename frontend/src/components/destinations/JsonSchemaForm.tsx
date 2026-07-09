import type React from "react"
import { Input } from "@/components/ui/Input/Input"
import { Select } from "@/components/ui/Select/Select"
import type { JsonSchema, JsonSchemaProperty } from "@/types"

/**
 * Renderiza um formulário a partir de um JSON Schema (Pydantic
 * `model_json_schema`) — usado para `config` e `delivery` dos destinos.
 * Cobre os campos ESCALARES de 1º nível: string, integer/number,
 * boolean e enum (Literal). Objetos aninhados (ex.: `delivery.breaker`,
 * referenciados via `$ref`) NÃO são renderizados aqui — o backend aplica os
 * defaults por kind e valida, então omiti-los é seguro. Mantém o componente
 * simples e o form enxuto; o tuning avançado (breaker/batch/retry) fica para a
 * UI ou a API.
 */

interface JsonSchemaFormProps {
  schema: JsonSchema
  values: Record<string, unknown>
  onChange: (next: Record<string, unknown>) => void
  disabled?: boolean
  /** Prefixo de id para acessibilidade (config vs delivery no mesmo form). */
  idPrefix?: string
}

type Scalar = "string" | "number" | "integer" | "boolean" | "enum"

interface ResolvedField {
  scalar: Scalar | null // null = nested/unsupported → não renderiza
  enumValues?: unknown[]
  description?: string
  minimum?: number
  maximum?: number
  default?: unknown
}

/** Resolve o descritor efetivo, desembrulhando anyOf(...|null) do Pydantic. */
function resolveField(prop: JsonSchemaProperty): ResolvedField {
  // Optional[X] → anyOf: [{type:X}, {type:"null"}]
  let effective: JsonSchemaProperty = prop
  if (Array.isArray(prop.anyOf)) {
    const nonNull = prop.anyOf.find((b) => b.type !== "null")
    if (nonNull) effective = { ...nonNull, default: prop.default ?? nonNull.default, description: prop.description }
  }

  if (Array.isArray(effective.enum) && effective.enum.length > 0) {
    return {
      scalar: "enum",
      enumValues: effective.enum,
      description: prop.description ?? effective.description,
      default: effective.default,
    }
  }

  // Objeto aninhado ($ref / allOf-ref / type:object) → não suportado aqui.
  if (effective.$ref || Array.isArray(effective.allOf) || effective.type === "object") {
    return { scalar: null }
  }

  const t = effective.type
  if (t === "boolean") {
    return { scalar: "boolean", description: prop.description ?? effective.description, default: effective.default }
  }
  if (t === "integer" || t === "number") {
    return {
      scalar: t,
      description: prop.description ?? effective.description,
      minimum: effective.minimum,
      maximum: effective.maximum,
      default: effective.default,
    }
  }
  if (t === "string") {
    return { scalar: "string", description: prop.description ?? effective.description, default: effective.default }
  }
  return { scalar: null }
}

export const JsonSchemaForm: React.FC<JsonSchemaFormProps> = ({
  schema,
  values,
  onChange,
  disabled,
  idPrefix = "f",
}) => {
  const properties = schema.properties ?? {}
  const required = new Set(schema.required ?? [])
  const keys = Object.keys(properties)

  if (keys.length === 0) {
    return <p className="text-sm text-text-tertiary">Sem campos configuráveis.</p>
  }

  const set = (key: string, value: unknown) => onChange({ ...values, [key]: value })

  return (
    <div className="space-y-4">
      {keys.map((key) => {
        const prop = properties[key]
        const field = resolveField(prop)
        if (field.scalar === null) return null // nested → server defaults

        const label = (prop.title as string) || key
        const isRequired = required.has(key)
        const id = `${idPrefix}-${key}`
        const current = values[key] !== undefined ? values[key] : field.default

        if (field.scalar === "boolean") {
          return (
            <label key={key} className="flex items-center gap-2 text-sm text-text">
              <input
                id={id}
                type="checkbox"
                disabled={disabled}
                checked={Boolean(current)}
                onChange={(e) => set(key, e.target.checked)}
                className="h-4 w-4 rounded border-border"
              />
              <span>{label}</span>
              {field.description && (
                <span className="text-xs text-text-tertiary">— {field.description}</span>
              )}
            </label>
          )
        }

        if (field.scalar === "enum") {
          return (
            <Select
              key={key}
              id={id}
              label={label + (isRequired ? " *" : "")}
              value={current != null ? String(current) : ""}
              options={(field.enumValues ?? []).map((v) => ({ value: String(v), label: String(v) }))}
              placeholder="Selecione..."
              disabled={disabled}
              helperText={field.description}
              onValueChange={(v) => set(key, v)}
            />
          )
        }

        const isNumber = field.scalar === "number" || field.scalar === "integer"
        return (
          <Input
            key={key}
            id={id}
            label={label + (isRequired ? " *" : "")}
            type={isNumber ? "number" : "text"}
            disabled={disabled}
            required={isRequired}
            value={current != null ? String(current) : ""}
            helperText={field.description}
            min={field.minimum}
            max={field.maximum}
            onChange={(e) => {
              const raw = e.target.value
              if (isNumber) {
                set(key, raw === "" ? undefined : Number(raw))
              } else {
                set(key, raw === "" ? undefined : raw)
              }
            }}
          />
        )
      })}
    </div>
  )
}
