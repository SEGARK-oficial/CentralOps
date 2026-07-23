/**
 * FinOps (cost_per_gb) na DestinationForm.
 *
 * O JsonSchemaForm só renderiza escalares de 1º nível e pula o objeto aninhado
 * `cost`, então cost_per_gb ficava inatingível pela UI — sem preço, o pricer EE
 * devolve US$ 0 e "Economia estimada" fica em zero sem explicação. Este teste
 * trava que o campo aparece, lê o valor salvo e escreve em delivery.cost.
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach } from "vitest"
import { DestinationForm } from "../DestinationForm"
import * as api from "@/services/api"
import type { DestinationType } from "@/types"

vi.mock("@/services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof api>()
  return { ...actual, listDestinationTypes: vi.fn() }
})
const mockedApi = vi.mocked(api)

const SPLUNK: DestinationType = {
  kind: "splunk_hec",
  label: "Splunk HEC",
  required_secrets: [],
  config_schema: { type: "object", properties: {} },
  delivery_schema: { type: "object", properties: {} },
} as unknown as DestinationType

beforeEach(() => {
  vi.clearAllMocks()
  mockedApi.listDestinationTypes.mockResolvedValue([SPLUNK])
})

it("mostra o campo Preço por GB e pré-carrega o valor salvo", async () => {
  render(
    <DestinationForm
      mode="edit"
      destination={
        {
          id: "d1",
          name: "Splunk prod",
          kind: "splunk_hec",
          enabled: true,
          config: {},
          delivery: { cost: { cost_per_gb: 2.5, currency: "USD" } },
        } as never
      }
      onCancel={vi.fn()}
      onSubmit={vi.fn()}
    />,
  )
  const input = (await screen.findByLabelText("Preço por GB")) as HTMLInputElement
  expect(input.value).toBe("2.5")
})

it("escreve o preço digitado em delivery.cost e submete", async () => {
  const onSubmit = vi.fn().mockResolvedValue(undefined)
  render(
    <DestinationForm
      mode="edit"
      destination={
        {
          id: "d1",
          name: "Splunk prod",
          kind: "splunk_hec",
          enabled: true,
          config: {},
          delivery: {},
        } as never
      }
      onCancel={vi.fn()}
      onSubmit={onSubmit}
    />,
  )
  const input = await screen.findByLabelText("Preço por GB")
  fireEvent.change(input, { target: { value: "3.75" } })

  const submit = screen.getByRole("button", { name: /salvar|criar|atualizar|guardar/i })
  fireEvent.click(submit)

  await waitFor(() => expect(onSubmit).toHaveBeenCalled())
  const payload = onSubmit.mock.calls[0][0]
  expect((payload.delivery as { cost: { cost_per_gb: number } }).cost.cost_per_gb).toBe(3.75)
})
