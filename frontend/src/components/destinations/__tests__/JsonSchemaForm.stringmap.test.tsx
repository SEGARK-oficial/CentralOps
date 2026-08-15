/**
 * Mapa livre string→string precisa ser editável na tela.
 *
 * O formulário pulava qualquer propriedade de tipo objeto. Isso deixava o
 * `headers` do webhook e do OTLP inatingíveis: não havia como definir um header
 * de API key, nem um `X-Source-Type` que o destino do outro lado exigisse. A
 * única saída era PATCH manual na API, o que derrota o propósito da tela.
 *
 * Modelo aninhado de verdade (`$ref`, como `delivery.breaker`) continua fora de
 * propósito: ali o backend aplica os defaults por kind e valida.
 */

import { render, screen, fireEvent } from "@testing-library/react"
import { describe, it, expect, vi } from "vitest"
import { JsonSchemaForm } from "@/components/destinations/JsonSchemaForm"

const SCHEMA_COM_MAPA = {
  type: "object",
  properties: {
    url: { type: "string", title: "Url" },
    headers: {
      type: "object",
      title: "Headers",
      additionalProperties: true,
      description: "Headers extras (ex: X-Api-Key)",
    },
  },
  required: ["url"],
} as never

describe("JsonSchemaForm — mapa string para string", () => {
  it("desenha o campo em vez de pular a propriedade", () => {
    render(
      <JsonSchemaForm schema={SCHEMA_COM_MAPA} values={{}} onChange={vi.fn()} />,
    )

    expect(screen.getByText("Headers")).toBeInTheDocument()
  })

  it("mostra os pares já gravados", () => {
    render(
      <JsonSchemaForm
        schema={SCHEMA_COM_MAPA}
        values={{ headers: { "X-Source-Type": "centralops" } }}
        onChange={vi.fn()}
      />,
    )

    expect(screen.getByDisplayValue("X-Source-Type")).toBeInTheDocument()
    expect(screen.getByDisplayValue("centralops")).toBeInTheDocument()
  })

  it("acrescentar e preencher devolve o objeto ao chamador", () => {
    const onChange = vi.fn()
    const { rerender } = render(
      <JsonSchemaForm schema={SCHEMA_COM_MAPA} values={{}} onChange={onChange} />,
    )

    fireEvent.click(screen.getByRole("button", { name: "Acrescentar" }))
    // A linha nasce vazia; chave vazia não vira header, então nada é gravado.
    expect(onChange).toHaveBeenLastCalledWith({ headers: {} })

    rerender(
      <JsonSchemaForm
        schema={SCHEMA_COM_MAPA}
        values={{ headers: { "": "" } }}
        onChange={onChange}
      />,
    )
    fireEvent.change(screen.getByLabelText(/chave 1/i), {
      target: { value: "X-Source-Type" },
    })

    expect(onChange).toHaveBeenLastCalledWith({ headers: { "X-Source-Type": "" } })
  })

  it("remover tira o par do objeto", () => {
    const onChange = vi.fn()
    render(
      <JsonSchemaForm
        schema={SCHEMA_COM_MAPA}
        values={{ headers: { "X-Api-Key": "abc", "X-Outro": "def" } }}
        onChange={onChange}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: /Remover X-Api-Key/i }))

    expect(onChange).toHaveBeenLastCalledWith({ headers: { "X-Outro": "def" } })
  })

  it("modelo aninhado por $ref continua de fora", () => {
    const comRef = {
      type: "object",
      properties: {
        url: { type: "string", title: "Url" },
        breaker: { $ref: "#/$defs/Breaker", title: "Breaker" },
      },
    } as never

    render(<JsonSchemaForm schema={comRef} values={{}} onChange={vi.fn()} />)

    expect(screen.queryByText("Breaker")).not.toBeInTheDocument()
  })
})
