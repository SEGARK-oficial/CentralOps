/**
 * `Optional[Literal[...]]` precisa virar Select, não caixa de texto.
 *
 * O Pydantic emite `Optional[X]` como `anyOf: [{...}, {type:"null"}]` — sem
 * `enum` e sem `type` no topo. Um renderizador que decidisse o widget olhando
 * só o topo cairia no ramo final e desenharia um input livre.
 *
 * A consequência não é cosmética. O `source_type_from` do nano é o campo que
 * decide o rótulo de CADA linha entregue; digitado errado, o schema recusa no
 * save — mas o operador só descobre a lista de origens válidas depois de errar,
 * e a lista não está em lugar nenhum da tela. O Select É a documentação.
 */

import { render, screen, fireEvent, within } from "@testing-library/react"
import { describe, it, expect, vi } from "vitest"
import { JsonSchemaForm } from "@/components/destinations/JsonSchemaForm"

/** Recorte fiel do que `NanoConfig.model_json_schema()` emite hoje. */
const SCHEMA_NANO = {
  type: "object",
  properties: {
    url: { type: "string", title: "Url" },
    source_type: { type: "string", title: "Source Type", default: "" },
    source_type_from: {
      anyOf: [
        {
          type: "string",
          enum: ["organization", "organization_vendor", "vendor", "event_type"],
        },
        { type: "null" },
      ],
      default: null,
      title: "Source Type From",
      description: "Rótulo derivado de cada evento, em vez de fixo",
    },
  },
  required: ["url"],
} as never

describe("JsonSchemaForm — enum opcional (anyOf do Pydantic)", () => {
  it("desenha uma lista de opções, e não um campo de texto livre", () => {
    render(<JsonSchemaForm schema={SCHEMA_NANO} values={{}} onChange={vi.fn()} />)

    const campo = screen.getByLabelText(/source type from/i)
    // O `source_type` irmão é texto livre de propósito; o derivado NÃO pode ser.
    expect(screen.getByLabelText(/^source type$/i)).toHaveAttribute("type", "text")
    expect(campo).toHaveAttribute("aria-haspopup", "listbox")
  })

  it("oferece todas as origens do enum", () => {
    render(<JsonSchemaForm schema={SCHEMA_NANO} values={{}} onChange={vi.fn()} />)

    fireEvent.click(screen.getByLabelText(/source type from/i))
    const lista = screen.getByRole("listbox")
    const opcoes = within(lista)
      .getAllByRole("option")
      .map((o) => o.textContent)
    for (const origem of [
      "organization",
      "organization_vendor",
      "vendor",
      "event_type",
    ]) {
      expect(opcoes).toContain(origem)
    }
  })

  it("emite a origem escolhida para o formulário", () => {
    const onChange = vi.fn()
    render(<JsonSchemaForm schema={SCHEMA_NANO} values={{}} onChange={onChange} />)

    fireEvent.click(screen.getByLabelText(/source type from/i))
    fireEvent.click(screen.getByRole("option", { name: "organization_vendor" }))
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ source_type_from: "organization_vendor" }),
    )
  })

  it("mantém a descrição, que é onde está a recomendação de uso", () => {
    render(<JsonSchemaForm schema={SCHEMA_NANO} values={{}} onChange={vi.fn()} />)

    expect(
      screen.getByText(/Rótulo derivado de cada evento/i),
    ).toBeInTheDocument()
  })
})
