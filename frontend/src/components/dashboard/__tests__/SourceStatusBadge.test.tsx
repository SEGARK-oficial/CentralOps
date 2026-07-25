import { render, screen } from "@testing-library/react"
import { SourceStatusBadge } from "@/components/dashboard/SourceStatusBadge"
import i18n from "@/i18n"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

describe("SourceStatusBadge", () => {
  it.each([
    ["degraded", "Degradado"],
    ["error", "Erro"],
    ["unhealthy", "Indisponível"],
    ["unknown", "Aguardando coleta"],
  ])("traduz o enum %s do backend", (status, label) => {
    render(<SourceStatusBadge status={status} />)

    expect(screen.getByText(label)).toBeInTheDocument()
    expect(screen.queryByText(status)).toBeNull()
  })

  it("aceita o enum em caixa alta", () => {
    render(<SourceStatusBadge status="DEGRADED" />)

    expect(screen.getByText("Degradado")).toBeInTheDocument()
  })

  it("mostra o enum cru quando não conhece o status (drift fica visível)", () => {
    render(<SourceStatusBadge status="throttled" />)

    expect(screen.getByText("throttled")).toBeInTheDocument()
  })
})
