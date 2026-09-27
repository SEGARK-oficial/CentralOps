/**
 * EditQueryModal — título traduzido (A11Y-23) e rede de segurança do
 * `submitError` do useForm.
 */
import { render, screen, fireEvent } from "@testing-library/react"
import { EditQueryModal } from "@/components/queries/EditQueryModal"
import * as api from "@/services/api"
import type { Client, Query } from "@/types"
import i18n from "@/i18n"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.spyOn(api, "listQueryCapabilities").mockResolvedValue([])

const CLIENTS: Client[] = []
const QUERY: Query = {
  id: 1,
  title: "Query existente",
  statement: "select 1",
  table: "xdr_index",
}

function renderModal(onSubmit = vi.fn().mockResolvedValue(undefined)) {
  render(
    <EditQueryModal
      query={QUERY}
      clients={CLIENTS}
      open
      onClose={vi.fn()}
      onSubmit={onSubmit}
    />,
  )
  return { onSubmit }
}

describe("EditQueryModal", () => {
  it("título do Modal é 'Editar query' (i18n)", async () => {
    renderModal()
    expect(await screen.findByRole("dialog", { name: "Editar query" })).toBeInTheDocument()
  })

  it("onSubmit rejeitado aparece na UI via submitError", async () => {
    const onSubmit = vi.fn().mockRejectedValue(new Error("409 conflito"))
    renderModal(onSubmit)
    fireEvent.click(await screen.findByRole("button", { name: /salvar/i }))
    expect(await screen.findByText("409 conflito")).toBeInTheDocument()
  })

  // R2-8.3: `registerField` do useForm — o erro por campo já existia, mas o
  // foco nunca se movia. A query vem pré-preenchida (edição), então o teste
  // esvazia o campo para forçar a invalidação.
  it("apagar o Título e salvar foca de volta o campo Título", async () => {
    renderModal()
    const titleField = await screen.findByLabelText(/t.tulo/i)
    fireEvent.change(titleField, { target: { value: "" } })
    fireEvent.click(screen.getByRole("button", { name: /salvar/i }))

    await screen.findByText("Título é obrigatório")
    expect(document.activeElement).toBe(titleField)
  })

  it("apagar o Statement e salvar foca o campo Query SQL", async () => {
    renderModal()
    const statementField = await screen.findByLabelText(/query sql/i)
    fireEvent.change(statementField, { target: { value: "" } })
    fireEvent.click(screen.getByRole("button", { name: /salvar/i }))

    await screen.findByText("A consulta SQL é obrigatória")
    expect(document.activeElement).toBe(statementField)
  })
})
