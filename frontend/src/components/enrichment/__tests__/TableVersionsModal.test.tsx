/**
 * TableVersionsModal tests.
 *
 * Cobre:
 * - Carrega e mostra o histórico de versões ao abrir.
 * - Estado vazio quando não há versão publicada.
 * - JSON inválido nas linhas é rejeitado sem chamar a API.
 * - Mensagem de commit obrigatória.
 * - Publicação bem-sucedida chama onChanged e mostra Notice de sucesso.
 * - Rollback chama a API e onChanged; versão vigente não mostra botão de reverter.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll } from "vitest"
import { TableVersionsModal } from "../TableVersionsModal"
import * as api from "@/services/api"
import type { EnrichmentTable, EnrichmentTableVersion } from "@/services/api"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

const table: EnrichmentTable = {
  id: "t1",
  organization_id: 1,
  name: "rede-corp",
  description: null,
  match_mode: "exact",
  key_kind: "ip",
  current_version_id: "v2",
  entry_count: 10,
  approx_bytes: 100,
}

const versions: EnrichmentTableVersion[] = [
  {
    id: "v2",
    version_number: 2,
    entry_count: 10,
    approx_bytes: 100,
    commit_message: "atualização de inventário",
    author_user_id: 1,
    created_at: "2026-08-01T00:00:00Z",
    is_current: true,
    invalid_rows: 0,
  },
  {
    id: "v1",
    version_number: 1,
    entry_count: 8,
    approx_bytes: 80,
    commit_message: "inventário inicial",
    author_user_id: 1,
    created_at: "2026-07-01T00:00:00Z",
    is_current: false,
    invalid_rows: 1,
  },
]

describe("TableVersionsModal", () => {
  it("carrega e mostra o histórico de versões ao abrir", async () => {
    mockedApi.listEnrichmentTableVersions.mockResolvedValue(versions)
    render(<TableVersionsModal open table={table} onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText("inventário inicial")).toBeInTheDocument()
    expect(screen.getByText("atualização de inventário")).toBeInTheDocument()
    expect(screen.getByText("vigente")).toBeInTheDocument()
  })

  it("mostra estado vazio quando não há versão publicada", async () => {
    mockedApi.listEnrichmentTableVersions.mockResolvedValue([])
    render(<TableVersionsModal open table={table} onClose={vi.fn()} onChanged={vi.fn()} />)

    expect(await screen.findByText("Nenhuma versão publicada ainda.")).toBeInTheDocument()
  })

  it("rejeita JSON inválido sem chamar a API", async () => {
    mockedApi.listEnrichmentTableVersions.mockResolvedValue([])
    render(<TableVersionsModal open table={table} onClose={vi.fn()} onChanged={vi.fn()} />)
    await screen.findByText("Nenhuma versão publicada ainda.")

    fireEvent.change(screen.getByLabelText("Linhas (JSON)"), { target: { value: "{ not valid json" } })
    fireEvent.change(screen.getByLabelText("Mensagem do commit"), { target: { value: "teste" } })
    fireEvent.click(screen.getByRole("button", { name: "Publicar versão" }))

    expect(await screen.findByText(/JSON inválido/i)).toBeInTheDocument()
    expect(mockedApi.commitEnrichmentTableVersion).not.toHaveBeenCalled()
  })

  it("exige mensagem de commit antes de publicar", async () => {
    mockedApi.listEnrichmentTableVersions.mockResolvedValue([])
    render(<TableVersionsModal open table={table} onClose={vi.fn()} onChanged={vi.fn()} />)
    await screen.findByText("Nenhuma versão publicada ainda.")

    fireEvent.change(screen.getByLabelText("Linhas (JSON)"), {
      target: { value: '{"10.0.5.7": {"site": "filial-sp"}}' },
    })
    fireEvent.click(screen.getByRole("button", { name: "Publicar versão" }))

    expect(await screen.findByText(/Descreva a mudança antes de publicar/i)).toBeInTheDocument()
    expect(mockedApi.commitEnrichmentTableVersion).not.toHaveBeenCalled()
  })

  it("publica com sucesso e chama onChanged", async () => {
    mockedApi.listEnrichmentTableVersions.mockResolvedValue([])
    mockedApi.commitEnrichmentTableVersion.mockResolvedValue({
      id: "v3",
      version_number: 3,
      entry_count: 1,
      approx_bytes: 10,
      commit_message: "teste",
      author_user_id: 1,
      created_at: "2026-08-09T00:00:00Z",
      is_current: true,
      invalid_rows: 0,
    })
    const onChanged = vi.fn()
    render(<TableVersionsModal open table={table} onClose={vi.fn()} onChanged={onChanged} />)
    await screen.findByText("Nenhuma versão publicada ainda.")

    fireEvent.change(screen.getByLabelText("Linhas (JSON)"), {
      target: { value: '{"10.0.5.7": {"site": "filial-sp"}}' },
    })
    fireEvent.change(screen.getByLabelText("Mensagem do commit"), { target: { value: "teste" } })
    fireEvent.click(screen.getByRole("button", { name: "Publicar versão" }))

    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(mockedApi.commitEnrichmentTableVersion).toHaveBeenCalledWith("t1", {
      rows: { "10.0.5.7": { site: "filial-sp" } },
      commit_message: "teste",
    })
    expect(await screen.findByText("Versão publicada")).toBeInTheDocument()
  })

  it("reverte para uma versão antiga e não mostra reverter na vigente", async () => {
    mockedApi.listEnrichmentTableVersions.mockResolvedValue(versions)
    mockedApi.rollbackEnrichmentTable.mockResolvedValue(table)
    const onChanged = vi.fn()
    render(<TableVersionsModal open table={table} onClose={vi.fn()} onChanged={onChanged} />)
    await screen.findByText("inventário inicial")

    const rollbackButtons = screen.getAllByRole("button", { name: "Reverter para esta" })
    expect(rollbackButtons).toHaveLength(1)
    fireEvent.click(rollbackButtons[0])

    await waitFor(() => expect(mockedApi.rollbackEnrichmentTable).toHaveBeenCalledWith("t1", "v1"))
    expect(onChanged).toHaveBeenCalled()
  })
})
