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

type Scalar = "string" | "number" | "integer" | "boolean" | "enum" | "stringmap"

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

  // Modelo aninhado ($ref / allOf-ref) → o backend aplica os defaults por kind
  // e valida, então omitir aqui é seguro (ex.: delivery.breaker).
  if (effective.$ref || Array.isArray(effective.allOf)) {
    return { scalar: null }
  }

  // Mapa livre string→string (`dict` no Pydantic). Sem este ramo o campo era
  // pulado, e isso tinha consequência real: `headers` do webhook e do OTLP
  // ficavam inatingíveis pela tela, então não havia como definir um header de
  // API key nem um `X-Source-Type` que o destino exigisse. A única saída era
  // PATCH manual na API.
  if (effective.type === "object") {
    const extras = effective.additionalProperties
    const ehMapaLivre =
      extras === true ||
      extras === undefined ||
      (typeof extras === "object" && extras !== null && (extras as JsonSchemaProperty).type === "string")
    if (ehMapaLivre) {
      return { scalar: "stringmap", description: prop.description ?? effective.description, default: effective.default }
    }
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

        if (field.scalar === "stringmap") {
          const mapa = (current && typeof current === "object" ? current : {}) as Record<string, string>
          const linhas = Object.entries(mapa)

          const gravar = (proximas: [string, string][]) => {
            const obj: Record<string, string> = {}
            for (const [k, v] of proximas) {
              // Chave vazia é descartada: ela não vira header nenhum e deixá-la
              // no objeto faria o operador achar que gravou algo.
              if (k.trim()) obj[k.trim()] = v
            }
            set(key, obj)
          }

          return (
            <fieldset key={key} className="space-y-2 rounded-lg border border-border p-3">
              <legend className="px-1 text-sm font-medium text-text">
                {label}
                {isRequired ? " *" : ""}
              </legend>
              {field.description && (
                <p className="text-xs text-text-tertiary">{field.description}</p>
              )}

              {linhas.length === 0 && (
                <p className="text-xs text-text-tertiary">Nenhum item. Use o botão abaixo para acrescentar.</p>
              )}

              {linhas.map(([k, v], i) => (
                <div key={`${id}-${i}`} className="flex items-start gap-2">
                  <Input
                    aria-label={`${label} — chave ${i + 1}`}
                    placeholder="Nome"
                    value={k}
                    disabled={disabled}
                    onChange={(e) => {
                      const proximas = [...linhas] as [string, string][]
                      proximas[i] = [e.target.value, v]
                      gravar(proximas)
                    }}
                  />
                  <Input
                    aria-label={`${label} — valor ${i + 1}`}
                    placeholder="Valor"
                    value={v}
                    disabled={disabled}
                    onChange={(e) => {
                      const proximas = [...linhas] as [string, string][]
                      proximas[i] = [k, e.target.value]
                      gravar(proximas)
                    }}
                  />
                  <button
                    type="button"
                    disabled={disabled}
                    aria-label={`Remover ${k || `item ${i + 1}`}`}
                    className="mt-1 rounded px-2 py-1 text-sm text-text-secondary hover:bg-surface-tertiary"
                    onClick={() => gravar(linhas.filter((_, j) => j !== i) as [string, string][])}
                  >
                    Remover
                  </button>
                </div>
              ))}

              <button
                type="button"
                disabled={disabled}
                className="rounded border border-border px-2 py-1 text-sm text-text hover:bg-surface-tertiary"
                onClick={() => gravar([...(linhas as [string, string][]), ["", ""]])}
              >
                Acrescentar
              </button>
            </fieldset>
          )
        }

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
            // Sem o " *" manual: o Input já desenha o marcador de obrigatório a
            // partir de `required`. Concatenar os dois rendia "Url * *" em todo
            // campo obrigatório, aqui e no formulário de destinos. O Select
            // acima mantém o manual porque não tem a prop `required`.
            label={label}
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
