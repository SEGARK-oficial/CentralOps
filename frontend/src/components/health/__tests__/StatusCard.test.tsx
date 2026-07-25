import { render, screen } from "@testing-library/react"
import { beforeAll, describe, it, expect } from "vitest"
import { StatusCard } from "@/components/health/StatusCard"
import i18n from "@/i18n"

// Sem o bootstrap do i18n as asserções batem contra a chave crua.
beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

describe("StatusCard", () => {
  it("renderiza data-testid correto", () => {
    render(<StatusCard status="healthy" />)
    expect(screen.getByTestId("health-status-card")).toBeInTheDocument()
  })

  it("status healthy: badge Saudável + descrição positiva", () => {
    render(<StatusCard status="healthy" />)
    expect(screen.getByText("Saudável")).toBeInTheDocument()
    expect(screen.getByText(/operando sem erros/i)).toBeInTheDocument()
  })

  it("status degraded: badge Degradado + descrição de degradação", () => {
    render(<StatusCard status="degraded" />)
    expect(screen.getByText("Degradado")).toBeInTheDocument()
    expect(screen.getByText(/degradação/i)).toBeInTheDocument()
  })

  // A descrição diz o que fazer, não só que está ruim.
  it("status unhealthy: badge Indisponível + o que verificar", () => {
    render(<StatusCard status="unhealthy" />)
    expect(screen.getByText("Indisponível")).toBeInTheDocument()
    expect(screen.getByText(/verifique o coletor/i)).toBeInTheDocument()
  })

  it("status unknown: badge Aguardando + descrição de espera", () => {
    render(<StatusCard status="unknown" />)
    expect(screen.getByText("Aguardando coleta")).toBeInTheDocument()
    expect(screen.getByText(/primeira execução/i)).toBeInTheDocument()
  })
})
