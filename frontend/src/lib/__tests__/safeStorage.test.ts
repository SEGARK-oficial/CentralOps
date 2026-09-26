import { afterEach, describe, expect, it, vi } from "vitest"
import { safeStorage } from "@/lib/safeStorage"

/**
 * R3-6.2: storage bloqueado (modo privado estrito, política corporativa,
 * cota estourada) lança em QUALQUER acesso ao `localStorage` — inclusive
 * leitura. Código cru derruba o componente; `safeStorage` nunca deve lançar.
 */
function blockStorage() {
  const blocked: Storage = {
    getItem: () => {
      throw new DOMException("Access is denied for this document.", "SecurityError")
    },
    setItem: () => {
      throw new DOMException("Access is denied for this document.", "SecurityError")
    },
    removeItem: () => {
      throw new DOMException("Access is denied for this document.", "SecurityError")
    },
    key: () => {
      throw new DOMException("Access is denied for this document.", "SecurityError")
    },
    clear: () => {
      throw new DOMException("Access is denied for this document.", "SecurityError")
    },
    length: 0,
  }
  vi.stubGlobal("localStorage", blocked)
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("safeStorage — caminho feliz", () => {
  afterEach(() => {
    localStorage.clear()
  })

  it("getItem/setItem/removeItem funcionam como o localStorage nativo", () => {
    expect(safeStorage.getItem("k")).toBeNull()
    safeStorage.setItem("k", "v")
    expect(safeStorage.getItem("k")).toBe("v")
    safeStorage.removeItem("k")
    expect(safeStorage.getItem("k")).toBeNull()
  })

  it("key()/length refletem o storage real", () => {
    safeStorage.setItem("a", "1")
    expect(safeStorage.length).toBeGreaterThanOrEqual(1)
    expect(safeStorage.key(0)).not.toBeNull()
  })
})

describe("safeStorage — storage bloqueado (regressão R3-6.2)", () => {
  it("getItem retorna null em vez de lançar", () => {
    blockStorage()
    expect(() => safeStorage.getItem("centralops_org_id")).not.toThrow()
    expect(safeStorage.getItem("centralops_org_id")).toBeNull()
  })

  it("setItem é no-op em vez de lançar", () => {
    blockStorage()
    expect(() => safeStorage.setItem("centralops_org_id", "5")).not.toThrow()
  })

  it("removeItem é no-op em vez de lançar", () => {
    blockStorage()
    expect(() => safeStorage.removeItem("centralops_org_id")).not.toThrow()
  })

  it("key()/length não lançam e degradam para valores neutros", () => {
    blockStorage()
    expect(() => safeStorage.key(0)).not.toThrow()
    expect(safeStorage.key(0)).toBeNull()
    expect(() => safeStorage.length).not.toThrow()
    expect(safeStorage.length).toBe(0)
  })
})
