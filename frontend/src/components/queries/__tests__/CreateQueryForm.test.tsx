/**
 * CreateQueryForm — validação i18n (A11Y-23) e rede de segurança do
 * `submitError` do useForm (erro do submit agora aparece na UI).
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { CreateQueryForm } from "@/components/queries/CreateQueryForm"
import * as api from "@/services/api"
import type { Client } from "@/types"
import i18n from "@/i18n"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.spyOn(api, "listQueryCapabilities").mockResolvedValue([])

const CLIENTS: Client[] = []

function renderForm(onSubmit = vi.fn().mockResolvedValue(undefined)) {
  render(<CreateQueryForm clients={CLIENTS} onSubmit={onSubmit} onCancel={vi.fn()} />)
  return { onSubmit }
}

describe("CreateQueryForm", () => {
  it("mostra erros de validação traduzidos ao submeter vazio", async () => {
    renderForm()
    fireEvent.click(screen.getByRole("button", { name: /criar query|salvar/i }))
    expect(await screen.findByText("Título é obrigatório")).toBeInTheDocument()
    expect(screen.getByText("A consulta SQL é obrigatória")).toBeInTheDocument()
  })

  it("título com menos de 3 caracteres mostra a mensagem específica", async () => {
    renderForm()
    fireEvent.change(screen.getByLabelText(/t.tulo/i), { target: { value: "ab" } })
    fireEvent.click(screen.getByRole("button", { name: /criar query|salvar/i }))
    expect(await screen.findByText("Use pelo menos 3 caracteres")).toBeInTheDocument()
  })

  it("onSubmit rejeitado aparece na UI via submitError (rede de segurança do useForm)", async () => {
    const onSubmit = vi.fn().mockRejectedValue(new Error("500 no backend"))
    renderForm(onSubmit)
    fireEvent.change(screen.getByLabelText(/t.tulo/i), { target: { value: "Query válida" } })
    fireEvent.change(screen.getByLabelText(/query sql/i), { target: { value: "select 1" } })
    fireEvent.click(screen.getByRole("button", { name: /criar query|salvar/i }))

    expect(await screen.findByText("500 no backend")).toBeInTheDocument()
  })
})
