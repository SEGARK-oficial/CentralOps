/**
 * Testes de DetectionDetailsDrawer — migrado para o primitivo ui/Drawer
 * (A11Y-16/17/ARQ-06/LAY-20): cobre render, i18n (A11Y-23), ações de triagem e
 * o fechamento por Escape/overlay herdado do Drawer.
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { DetectionDetailsDrawer } from "@/components/detections/DetectionDetailsDrawer"
import * as permHooks from "@/hooks/usePermission"
import type { DetectionRead } from "@/types"
import i18n from "@/i18n"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/hooks/usePermission")
const mockedUsePermission = vi.mocked(permHooks.usePermission)

const DETECTION: DetectionRead = {
  id: 42,
  organization_id: 1,
  source: "correlation",
  severity_id: 5,
  status: "open",
  dedup_key: "dedup-abc",
  rule_name: "Login suspeito",
  rule_id: "rule-1",
  count: 3,
  created_at: "2026-09-01T00:00:00Z",
}

function renderDrawer(overrides: Partial<Parameters<typeof DetectionDetailsDrawer>[0]> = {}) {
  const onClose = vi.fn()
  const onTriage = vi.fn().mockResolvedValue(undefined)
  render(
    <DetectionDetailsDrawer
      open
      detection={DETECTION}
      onClose={onClose}
      onTriage={onTriage}
      {...overrides}
    />,
  )
  return { onClose, onTriage }
}

describe("DetectionDetailsDrawer", () => {
  beforeEach(() => {
    mockedUsePermission.mockReturnValue(true)
  })

  it("renderiza título, severidade e status traduzidos (A11Y-23)", () => {
    renderDrawer()
    expect(screen.getByRole("dialog", { name: "Detalhes da detecção" })).toBeInTheDocument()
    expect(screen.getByText("Login suspeito")).toBeInTheDocument()
    expect(screen.getByText("Crítica")).toBeInTheDocument()
    expect(screen.getByText("Aberta")).toBeInTheDocument()
    expect(screen.getByText("Correlação")).toBeInTheDocument()
  })

  it("não renderiza nada quando fechado", () => {
    renderDrawer({ open: false })
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  })

  it("esconde ações de triagem sem a permissão query.run", () => {
    mockedUsePermission.mockReturnValue(false)
    renderDrawer()
    expect(screen.queryByRole("button", { name: /reconhecer/i })).not.toBeInTheDocument()
  })

  it("chama onTriage ao clicar em Reconhecer (Ack)", async () => {
    const { onTriage } = renderDrawer()
    fireEvent.click(screen.getByRole("button", { name: /reconhecer/i }))
    await waitFor(() => expect(onTriage).toHaveBeenCalledWith(42, "ack"))
  })

  it("mostra o erro de triagem via Notice", () => {
    renderDrawer({ triageError: "falha ao mudar status" })
    expect(screen.getByText("Falha na triagem")).toBeInTheDocument()
    expect(screen.getByText("falha ao mudar status")).toBeInTheDocument()
  })

  it("fecha no Escape (herdado do primitivo ui/Drawer)", () => {
    const { onClose } = renderDrawer()
    fireEvent.keyDown(document, { key: "Escape" })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it("chama onClose ao clicar no botão fechar", () => {
    const { onClose } = renderDrawer()
    fireEvent.click(screen.getByRole("button", { name: "Fechar detalhes" }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
