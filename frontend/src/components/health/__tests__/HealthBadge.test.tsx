import { render, screen } from "@testing-library/react"
import { HealthBadge } from "@/components/health/HealthBadge"

describe("HealthBadge", () => {
  it("renderiza label 'Saudável' para status healthy", () => {
    render(<HealthBadge status="healthy" />)
    expect(screen.getByText("Saudável")).toBeInTheDocument()
  })

  it("renderiza label 'Degradado' para status degraded", () => {
    render(<HealthBadge status="degraded" />)
    expect(screen.getByText("Degradado")).toBeInTheDocument()
  })

  it("renderiza label 'Indisponível' para status unhealthy", () => {
    render(<HealthBadge status="unhealthy" />)
    expect(screen.getByText("Indisponível")).toBeInTheDocument()
  })

  it("renderiza label 'Aguardando primeira coleta' para status unknown", () => {
    render(<HealthBadge status="unknown" />)
    expect(screen.getByText("Aguardando primeira coleta")).toBeInTheDocument()
  })
})
