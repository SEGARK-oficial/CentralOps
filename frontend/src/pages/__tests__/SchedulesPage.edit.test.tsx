/**
 * Editar um agendamento que já está ativo.
 *
 * Antes não havia como: a tela só oferecia criar e apagar, e o backend nem tinha
 * rota de update. Quem errasse o intervalo apagava e recriava, e o histórico ia
 * junto, porque os resultados guardam `schedule_id` e ele passava a apontar para
 * uma linha apagada.
 *
 * O mesmo formulário serve criar e editar de propósito: um segundo formulário
 * divergiria em validação e unidades na primeira mudança.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach } from "vitest"
import i18n from "@/i18n"
import { SchedulesPage } from "@/pages/SchedulesPage"
import type { Integration, Query, Schedule } from "@/types"

vi.mock("@/services/api", () => ({
  listSchedules: vi.fn(),
  getScheduleHistory: vi.fn().mockResolvedValue([]),
  listEmails: vi.fn().mockResolvedValue([]),
  listQueries: vi.fn(),
  listIntegrations: vi.fn(),
  createSchedule: vi.fn(),
  updateSchedule: vi.fn(),
  deleteSchedule: vi.fn(),
  downloadStoredCSV: vi.fn(),
}))

import * as api from "@/services/api"

const query: Query = {
  id: 10,
  title: "Logins suspeitos",
  statement: "SELECT *",
  table: "auth",
  client_ids: [1, 2],
}

const integrations = [
  { id: 1, name: "ACME Corp", is_authenticated: true, tenant_id: "t1", is_active: true, platform: "sophos" },
  { id: 2, name: "Globex", is_authenticated: true, tenant_id: "t2", is_active: true, platform: "sophos" },
] as unknown as Integration[]

const agendamento: Schedule = {
  id: 100,
  query_id: 10,
  query_title: "Logins suspeitos",
  client_ids: [1, 2],
  interval_value: 6,
  interval_unit: "hours",
  lookback_value: 1,
  lookback_unit: "days",
  notify_on_results: true,
  next_run: "2026-06-15T22:00:00Z",
}

const mocked = vi.mocked(api)

beforeEach(async () => {
  await i18n.changeLanguage("pt-BR")
  vi.clearAllMocks()
  mocked.listSchedules.mockResolvedValue([agendamento])
  mocked.listQueries.mockResolvedValue([query])
  mocked.listIntegrations.mockResolvedValue(integrations)
  mocked.getScheduleHistory.mockResolvedValue([])
  mocked.listEmails.mockResolvedValue([])
  mocked.updateSchedule.mockResolvedValue(agendamento)
})

describe("SchedulesPage — editar agendamento ativo", () => {
  it("oferece a ação de editar em cada agendamento", async () => {
    render(<SchedulesPage />)

    expect(await screen.findByRole("button", { name: /editar/i })).toBeInTheDocument()
  })

  it("carrega os valores do agendamento no formulário", async () => {
    render(<SchedulesPage />)

    fireEvent.click(await screen.findByRole("button", { name: /editar/i }))

    // O formulário passa a se anunciar como edição.
    expect(await screen.findByText("Editar agendamento")).toBeInTheDocument()
    // E vem preenchido com o intervalo atual, em vez do default.
    await waitFor(() => {
      expect(screen.getByDisplayValue("6")).toBeInTheDocument()
    })
  })

  it("salvar chama o update, e não cria um agendamento novo", async () => {
    render(<SchedulesPage />)

    fireEvent.click(await screen.findByRole("button", { name: /editar/i }))
    const salvar = await screen.findByRole("button", { name: /salvar alterações/i })
    fireEvent.click(salvar)

    await waitFor(() => {
      expect(mocked.updateSchedule).toHaveBeenCalledWith(100, expect.objectContaining({
        query_id: 10,
        interval_value: 6,
        interval_unit: "hours",
      }))
    })
    // Criar aqui produziria um agendamento duplicado rodando em paralelo.
    expect(mocked.createSchedule).not.toHaveBeenCalled()
  })

  it("cancelar sai do modo de edição sem gravar nada", async () => {
    render(<SchedulesPage />)

    fireEvent.click(await screen.findByRole("button", { name: /editar/i }))
    await screen.findByText("Editar agendamento")

    fireEvent.click(screen.getByRole("button", { name: /^cancelar$/i }))

    await waitFor(() => {
      expect(screen.queryByText("Editar agendamento")).not.toBeInTheDocument()
    })
    expect(mocked.updateSchedule).not.toHaveBeenCalled()
  })

  it("sem editar, o formulário continua criando", async () => {
    render(<SchedulesPage />)

    await screen.findByRole("button", { name: /editar/i })

    // O rótulo do submit é o de criação enquanto ninguém entra em edição.
    expect(screen.queryByRole("button", { name: /salvar alterações/i })).not.toBeInTheDocument()
  })
})
