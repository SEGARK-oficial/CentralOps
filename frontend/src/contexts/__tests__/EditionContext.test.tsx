/**
 * EditionContext + useEdition/useFeature + EditionInfoCard.
 * Cobre: fetch no mount, exposição de edição/features, useFeature, fail-closed a
 * Community em erro, e os avisos de vencimento de licença do EditionInfoCard.
 */

import { render, screen, waitFor } from "@testing-library/react"
import { beforeAll, describe, it, expect, vi, beforeEach } from "vitest"
import { EditionProvider, useEdition, useFeature } from "@/contexts/EditionContext"
import { EditionInfoCard } from "@/components/config/EditionInfoCard"
import * as api from "@/services/api"
import type { EditionStatus } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

function Probe() {
  const { edition, isEnterprise, plan, maxOrganizations, loading, error } = useEdition()
  const fleet = useFeature("fleet_management")
  if (loading) return <div>loading</div>
  return (
    <div>
      <span data-testid="edition">{edition}</span>
      <span data-testid="enterprise">{String(isEnterprise)}</span>
      <span data-testid="plan">{plan ?? "—"}</span>
      <span data-testid="maxorgs">{maxOrganizations ?? "null"}</span>
      <span data-testid="fleet">{String(fleet)}</span>
      <span data-testid="error">{error ?? "—"}</span>
    </div>
  )
}

const ENTERPRISE: EditionStatus = {
  edition: "enterprise",
  features: ["fleet_management", "audit_compliance"],
  plan: "mssp",
  seats: 10,
  max_organizations: null,
  expires_at: null,
}

beforeEach(() => vi.clearAllMocks())

describe("EditionContext / useEdition", () => {
  it("expõe a edição Enterprise + features após o fetch", async () => {
    mockedApi.getEdition.mockResolvedValue(ENTERPRISE)
    render(
      <EditionProvider>
        <Probe />
      </EditionProvider>,
    )
    await waitFor(() => expect(screen.getByTestId("edition").textContent).toBe("enterprise"))
    expect(screen.getByTestId("enterprise").textContent).toBe("true")
    expect(screen.getByTestId("plan").textContent).toBe("mssp")
    expect(screen.getByTestId("fleet").textContent).toBe("true") // useFeature
  })

  it("fail-closed a Community quando o fetch falha", async () => {
    mockedApi.getEdition.mockRejectedValue(new Error("network down"))
    render(
      <EditionProvider>
        <Probe />
      </EditionProvider>,
    )
    await waitFor(() => expect(screen.getByTestId("edition").textContent).toBe("community"))
    expect(screen.getByTestId("enterprise").textContent).toBe("false")
    expect(screen.getByTestId("fleet").textContent).toBe("false")
    expect(screen.getByTestId("error").textContent).toMatch(/network down/)
  })

  it("Starter expõe max_organizations=1", async () => {
    mockedApi.getEdition.mockResolvedValue({
      edition: "enterprise",
      features: [],
      plan: "starter",
      max_organizations: 1,
    })
    render(
      <EditionProvider>
        <Probe />
      </EditionProvider>,
    )
    await waitFor(() => expect(screen.getByTestId("maxorgs").textContent).toBe("1"))
  })
})

describe("EditionInfoCard", () => {
  it("mostra badge Community quando não-licenciado", async () => {
    mockedApi.getEdition.mockResolvedValue({ edition: "community", features: [] })
    render(
      <EditionProvider>
        <EditionInfoCard />
      </EditionProvider>,
    )
    await waitFor(() => expect(screen.getByText("Community")).toBeInTheDocument())
  })

  it("avisa quando a licença está perto de vencer (< 14 dias)", async () => {
    const soon = new Date(Date.now() + 5 * 86_400_000).toISOString()
    mockedApi.getEdition.mockResolvedValue({
      edition: "enterprise",
      features: ["x"],
      plan: "enterprise",
      expires_at: soon,
    })
    render(
      <EditionProvider>
        <EditionInfoCard />
      </EditionProvider>,
    )
    await waitFor(() =>
      expect(screen.getByText(/Licença próxima do vencimento/)).toBeInTheDocument(),
    )
  })

  it("NÃO avisa quando a licença está longe de vencer", async () => {
    const far = new Date(Date.now() + 200 * 86_400_000).toISOString()
    mockedApi.getEdition.mockResolvedValue({
      edition: "enterprise",
      features: ["x"],
      expires_at: far,
    })
    render(
      <EditionProvider>
        <EditionInfoCard />
      </EditionProvider>,
    )
    await waitFor(() => expect(screen.getByText("Enterprise")).toBeInTheDocument())
    expect(screen.queryByText(/próxima do vencimento/)).not.toBeInTheDocument()
  })
})
