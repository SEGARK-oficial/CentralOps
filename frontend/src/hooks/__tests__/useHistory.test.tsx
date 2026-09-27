/**
 * Testes de useHistory — R2-5.4: fallback de erro traduzido (antes fixo em
 * PT), usado só quando a rejeição não é um `Error` de verdade.
 */
import { renderHook, act } from "@testing-library/react"
import { useHistory } from "@/hooks/useHistory"
import { useAuth } from "@/contexts/AuthContext"
import * as api from "@/services/api"
import i18n from "@/i18n"

vi.mock("@/services/api", async () => {
  const actual = await vi.importActual<typeof import("@/services/api")>("@/services/api")
  return {
    ...actual,
    listHistory: vi.fn(),
    listSearchHistory: vi.fn(),
    listAuditHistoryFiltered: vi.fn(),
    downloadStoredCSV: vi.fn(),
    downloadAuditHistoryCSV: vi.fn(),
  }
})
vi.mock("@/contexts/AuthContext")

const mockedApi = vi.mocked(api)
const mockedUseAuth = vi.mocked(useAuth)

beforeEach(() => {
  vi.clearAllMocks()
  mockedUseAuth.mockReturnValue({ user: { role: "admin" } } as never)
})

describe("useHistory — fallback de erro traduzido (R2-5.4)", () => {
  it("fetchHistory: rejeição sem Error usa a mensagem traduzida (pt)", async () => {
    await i18n.changeLanguage("pt")
    mockedApi.listHistory.mockRejectedValue("boom") // rejeição crua, não um Error
    mockedApi.listSearchHistory.mockResolvedValue([])

    const { result } = renderHook(() => useHistory())
    await act(async () => { await result.current.fetchHistory() })

    expect(result.current.error).toBe("Falha ao carregar histórico")
  })

  it("fetchHistory: a mesma rejeição usa a mensagem em inglês quando o idioma é en", async () => {
    await i18n.changeLanguage("en")
    mockedApi.listHistory.mockRejectedValue("boom")
    mockedApi.listSearchHistory.mockResolvedValue([])

    const { result } = renderHook(() => useHistory())
    await act(async () => { await result.current.fetchHistory() })

    expect(result.current.error).toBe("Failed to load history")
    await i18n.changeLanguage("pt")
  })

  it("fetchAuditHistory: rejeição sem Error usa a mensagem traduzida", async () => {
    await i18n.changeLanguage("pt")
    mockedApi.listAuditHistoryFiltered.mockRejectedValue("boom")

    const { result } = renderHook(() => useHistory())
    await act(async () => { await result.current.fetchAuditHistory() })

    expect(result.current.error).toBe("Falha ao carregar auditoria")
  })

  it("downloadCSV: rejeição sem Error relança com a mensagem traduzida", async () => {
    await i18n.changeLanguage("pt")
    mockedApi.downloadStoredCSV.mockRejectedValue("boom")

    const { result } = renderHook(() => useHistory())
    await expect(result.current.downloadCSV("srch-1")).rejects.toThrow("Falha ao baixar CSV")
  })

  it("downloadAuditCSV: usuário não-admin recebe a mensagem traduzida de acesso restrito", async () => {
    await i18n.changeLanguage("pt")
    mockedUseAuth.mockReturnValue({ user: { role: "operator" } } as never)

    const { result } = renderHook(() => useHistory())
    await expect(result.current.downloadAuditCSV()).rejects.toThrow("Acesso restrito a administradores")
  })

  it("downloadAuditCSV: rejeição sem Error (admin) usa a mensagem traduzida de exportação", async () => {
    await i18n.changeLanguage("pt")
    mockedApi.downloadAuditHistoryCSV.mockRejectedValue("boom")

    const { result } = renderHook(() => useHistory())
    await expect(result.current.downloadAuditCSV()).rejects.toThrow("Falha ao exportar auditoria")
  })
})
