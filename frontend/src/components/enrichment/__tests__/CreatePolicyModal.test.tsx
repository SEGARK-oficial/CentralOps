/**
 * CreatePolicyModal tests.
 *
 * Cobre:
 * - Nome obrigatório antes de submeter.
 * - Organização obrigatória (não existe política de enriquecimento global).
 * - Submit bem-sucedido chama onCreated com a política criada (DESLIGADA, sem versão).
 * - Erro do backend é exibido, modal permanece aberto.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest"
import { CreatePolicyModal } from "../CreatePolicyModal"
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

describe("CreatePolicyModal", () => {
  it("exige nome antes de submeter", async () => {
    const onCreated = vi.fn()
    render(<CreatePolicyModal open onClose={vi.fn()} onCreated={onCreated} />)

    fireEvent.click(screen.getByRole("button", { name: "Nova política" }))

    expect(await screen.findByText(/Informe um nome/i)).toBeInTheDocument()
    expect(mockedApi.createEnrichmentPolicy).not.toHaveBeenCalled()
  })

  it("exige organização selecionada quando não há filtro global ativo", async () => {
    platformContextValue.selectedOrgId = null
    const onCreated = vi.fn()
    render(<CreatePolicyModal open onClose={vi.fn()} onCreated={onCreated} />)

    fireEvent.change(screen.getByLabelText(/Nome/i), { target: { value: "contexto-de-ativo" } })
    fireEvent.click(screen.getByRole("button", { name: "Nova política" }))

    expect(await screen.findByText(/não existe política de enriquecimento global/i)).toBeInTheDocument()
    expect(mockedApi.createEnrichmentPolicy).not.toHaveBeenCalled()
  })

  it("submete com sucesso e chama onCreated", async () => {
    const created = {
      id: "p1",
      organization_id: 1,
      name: "contexto-de-ativo",
      description: null,
      enabled: false,
      current_version_id: null,
      rule_count: 0,
    }
    mockedApi.createEnrichmentPolicy.mockResolvedValue(created)
    const onCreated = vi.fn()

    render(<CreatePolicyModal open onClose={vi.fn()} onCreated={onCreated} />)

    fireEvent.change(screen.getByLabelText(/Nome/i), { target: { value: "contexto-de-ativo" } })
    fireEvent.click(screen.getByRole("button", { name: "Nova política" }))

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created))
    expect(mockedApi.createEnrichmentPolicy).toHaveBeenCalledWith(
      expect.objectContaining({ name: "contexto-de-ativo", organization_id: 1 }),
    )
  })

  it("mostra erro do backend sem fechar o modal", async () => {
    mockedApi.createEnrichmentPolicy.mockRejectedValue(new Error("já existe uma política chamada 'x'"))
    render(<CreatePolicyModal open onClose={vi.fn()} onCreated={vi.fn()} />)

    fireEvent.change(screen.getByLabelText(/Nome/i), { target: { value: "x" } })
    fireEvent.click(screen.getByRole("button", { name: "Nova política" }))

    expect(await screen.findByText(/já existe uma política/i)).toBeInTheDocument()
  })
})
