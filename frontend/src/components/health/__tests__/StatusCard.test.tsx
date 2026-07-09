import { render, screen } from "@testing-library/react"
import { StatusCard } from "@/components/health/StatusCard"

describe("StatusCard", () => {
  it("renderiza data-testid correto", () => {
    render(<StatusCard status="healthy" />)
    expect(screen.getByTestId("health-status-card")).toBeInTheDocument()
  })

  it("status healthy: badge Saudável + descrição positiva", () => {
    render(<StatusCard status="healthy" />)
    expect(screen.getByText("Saudável")).toBeInTheDocument()
    expect(screen.getByText(/operando normalmente/i)).toBeInTheDocument()
  })

  it("status degraded: badge Degradado + descrição de degradação", () => {
    render(<StatusCard status="degraded" />)
    expect(screen.getByText("Degradado")).toBeInTheDocument()
    expect(screen.getByText(/degradação/i)).toBeInTheDocument()
  })

  it("status unhealthy: badge Indisponível + descrição de ação imediata", () => {
    render(<StatusCard status="unhealthy" />)
    expect(screen.getByText("Indisponível")).toBeInTheDocument()
    expect(screen.getByText(/ação imediata recomendada/i)).toBeInTheDocument()
  })

  it("status unknown: badge Aguardando + descrição de espera", () => {
    render(<StatusCard status="unknown" />)
    expect(screen.getByText("Aguardando primeira coleta")).toBeInTheDocument()
    expect(screen.getByText(/primeira execução/i)).toBeInTheDocument()
  })
})
