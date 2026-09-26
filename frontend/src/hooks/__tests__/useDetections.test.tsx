/**
 * Testes de useDetections — R2-5.4 (fallback de erro traduzido) e PERF-14
 * (sinal de truncamento).
 */
import { renderHook, waitFor } from "@testing-library/react"
import { useDetections } from "@/hooks/useDetections"
import * as api from "@/services/api"
import i18n from "@/i18n"
import type { DetectionRead } from "@/types"

vi.mock("@/services/api", async () => {
  const actual = await vi.importActual<typeof import("@/services/api")>("@/services/api")
  return {
    ...actual,
    listDetections: vi.fn(),
    updateDetectionStatus: vi.fn(),
  }
})

const mockedApi = vi.mocked(api)

beforeEach(() => {
  vi.clearAllMocks()
})

describe("useDetections — fallback de erro traduzido (R2-5.4)", () => {
  it("rejeição sem Error usa a mensagem traduzida (pt)", async () => {
    await i18n.changeLanguage("pt")
    mockedApi.listDetections.mockRejectedValue("boom")

    const { result } = renderHook(() => useDetections())
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.error).toBe("Falha ao carregar detecções")
  })

  it("a mesma rejeição usa a mensagem em espanhol quando o idioma é es", async () => {
    await i18n.changeLanguage("es")
    mockedApi.listDetections.mockRejectedValue("boom")

    const { result } = renderHook(() => useDetections())
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.error).toBe("Error al cargar las detecciones")
    await i18n.changeLanguage("pt")
  })

  it("um Error de verdade preserva a própria mensagem (não usa o fallback)", async () => {
    await i18n.changeLanguage("pt")
    mockedApi.listDetections.mockRejectedValue(new Error("Falha de rede específica"))

    const { result } = renderHook(() => useDetections())
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.error).toBe("Falha de rede específica")
  })
})

describe("useDetections — truncated (PERF-14)", () => {
  it("truncated=true quando a resposta bate no teto de 200", async () => {
    const many: DetectionRead[] = Array.from({ length: 200 }, (_, i) => ({
      id: i + 1,
      organization_id: 1,
      source: "scheduled_query",
      severity_id: 3,
      status: "open",
      dedup_key: `d-${i}`,
    }))
    mockedApi.listDetections.mockResolvedValue(many)

    const { result } = renderHook(() => useDetections())
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.truncated).toBe(true)
  })

  it("truncated=false abaixo do teto", async () => {
    mockedApi.listDetections.mockResolvedValue([])
    const { result } = renderHook(() => useDetections())
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.truncated).toBe(false)
  })
})
