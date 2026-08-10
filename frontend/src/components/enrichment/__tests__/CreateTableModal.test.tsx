/**
 * CreateTableModal tests.
 *
 * Cobre:
 * - Nome obrigatório antes de submeter.
 * - Organização obrigatória (não existe tabela de enriquecimento global).
 * - Submit bem-sucedido chama onCreated com a tabela criada.
 * - Erro do backend (ex.: nome duplicado) é exibido, modal permanece aberto.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest"
import { CreateTableModal } from "../CreateTableModal"
import * as api from "@/services/api"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

vi.mock("@/services/api")

const platformContextValue: {
  organizations: Array<{ id: number; name: string }>
  selectedOrgId: number | null
} = {
  organizations: [
    { id: 1, name: "Acme Corp" },
    { id: 2, name: "Beta Inc" },
  ],
  selectedOrgId: 1,
}

vi.mock("@/contexts/PlatformContext", () => ({
  usePlatform: () => platformContextValue,
}))

const mockedApi = vi.mocked(api)

beforeEach(() => {
  platformContextValue.selectedOrgId = 1
})

describe("CreateTableModal", () => {
  it("exige nome antes de submeter", async () => {
    const onCreated = vi.fn()
    render(<CreateTableModal open onClose={vi.fn()} onCreated={onCreated} />)

    fireEvent.click(screen.getByRole("button", { name: "Nova tabela" }))

    expect(await screen.findByText(/Informe um nome/i)).toBeInTheDocument()
    expect(mockedApi.createEnrichmentTable).not.toHaveBeenCalled()
  })

  it("exige organização selecionada quando não há filtro global ativo", async () => {
    platformContextValue.selectedOrgId = null
    const onCreated = vi.fn()
    render(<CreateTableModal open onClose={vi.fn()} onCreated={onCreated} />)

    fireEvent.change(screen.getByLabelText(/Nome/i), { target: { value: "rede-corp" } })
    fireEvent.click(screen.getByRole("button", { name: "Nova tabela" }))

    expect(await screen.findByText(/não existe tabela de enriquecimento global/i)).toBeInTheDocument()
    expect(mockedApi.createEnrichmentTable).not.toHaveBeenCalled()
  })

  it("submete com sucesso e chama onCreated", async () => {
    const created = {
      id: "t1",
      organization_id: 1,
      name: "rede-corp",
      description: null,
      match_mode: "cidr" as const,
      key_kind: "ip",
      current_version_id: null,
      entry_count: 0,
      approx_bytes: 0,
    }
    mockedApi.createEnrichmentTable.mockResolvedValue(created)
    const onCreated = vi.fn()

    render(<CreateTableModal open onClose={vi.fn()} onCreated={onCreated} />)

    fireEvent.change(screen.getByLabelText(/Nome/i), { target: { value: "rede-corp" } })
    fireEvent.click(screen.getByLabelText("Tipo de casamento"))
    fireEvent.click(screen.getByRole("option", { name: "CIDR" }))

    fireEvent.click(screen.getByRole("button", { name: "Nova tabela" }))

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created))
    expect(mockedApi.createEnrichmentTable).toHaveBeenCalledWith(
      expect.objectContaining({ name: "rede-corp", match_mode: "cidr", organization_id: 1 }),
    )
  })

  it("mostra erro do backend sem fechar o modal", async () => {
    mockedApi.createEnrichmentTable.mockRejectedValue(new Error("já existe uma tabela chamada 'x'"))
    render(<CreateTableModal open onClose={vi.fn()} onCreated={vi.fn()} />)

    fireEvent.change(screen.getByLabelText(/Nome/i), { target: { value: "x" } })
    fireEvent.click(screen.getByRole("button", { name: "Nova tabela" }))

    expect(await screen.findByText(/já existe uma tabela/i)).toBeInTheDocument()
  })
})
