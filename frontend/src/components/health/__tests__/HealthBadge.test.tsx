import { render, screen } from "@testing-library/react"
import { beforeAll, describe, it, expect } from "vitest"
import { HealthBadge } from "@/components/health/HealthBadge"
import i18n from "@/i18n"

// Sem o bootstrap do i18n o i18next nunca é inicializado neste processo e toda
// asserção bate contra a chave crua ("health.badge.healthy").
beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

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

  it("renderiza label 'Aguardando coleta' para status unknown", () => {
    render(<HealthBadge status="unknown" />)
    expect(screen.getByText("Aguardando coleta")).toBeInTheDocument()
  })

  // Saudável NÃO ganha matiz: num grid de dezenas de integrações em ordem, o
  // verde em todas apagaria a única que precisa de atenção.
  it("status healthy usa o selo neutro, não o de sucesso", () => {
    const badge = render(<HealthBadge status="healthy" />).getByText("Saudável")
    expect(badge.className).toContain("bg-surface-tertiary")
    expect(badge.className).not.toContain("success")
  })

  it("status degraded mantém o âmbar de atenção", () => {
    const badge = render(<HealthBadge status="degraded" />).getByText("Degradado")
    expect(badge.className).toContain("warning")
  })
})
