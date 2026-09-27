import { describe, it, expect } from "vitest"
import { safeExternalHref, safeInternalPath } from "@/lib/safeUrl"

describe("safeExternalHref", () => {
  it("aceita http:", () => {
    expect(safeExternalHref("http://example.com/a")).toBe("http://example.com/a")
  })

  it("aceita https:", () => {
    expect(safeExternalHref("https://example.com/a?b=1")).toBe("https://example.com/a?b=1")
  })

  it("rejeita javascript:", () => {
    // eslint-disable-next-line no-script-url -- payload de teste: prova que o esquema É rejeitado.
    expect(safeExternalHref("javascript:alert(1)")).toBeUndefined()
  })

  it("rejeita data:", () => {
    expect(safeExternalHref("data:text/html,<script>alert(1)</script>")).toBeUndefined()
  })

  it("rejeita vbscript:", () => {
    expect(safeExternalHref("vbscript:msgbox(1)")).toBeUndefined()
  })

  it("rejeita path relativo (sem esquema resolvível)", () => {
    expect(safeExternalHref("/relative/path")).toBeUndefined()
  })

  it("rejeita string vazia, null e undefined", () => {
    expect(safeExternalHref("")).toBeUndefined()
    expect(safeExternalHref(null)).toBeUndefined()
    expect(safeExternalHref(undefined)).toBeUndefined()
  })

  it("rejeita string inválida como URL", () => {
    expect(safeExternalHref("not a url")).toBeUndefined()
  })
})

describe("safeInternalPath", () => {
  it("aceita path interno simples", () => {
    expect(safeInternalPath("/settings/account")).toBe("/settings/account")
  })

  it("aceita path com query string", () => {
    expect(safeInternalPath("/search?q=abc")).toBe("/search?q=abc")
  })

  it("rejeita path sem barra inicial", () => {
    expect(safeInternalPath("settings/account")).toBeUndefined()
  })

  it("rejeita protocol-relative (//host)", () => {
    expect(safeInternalPath("//evil.com/phish")).toBeUndefined()
  })

  it("rejeita barra invertida", () => {
    expect(safeInternalPath("/\\evil.com")).toBeUndefined()
  })

  it("rejeita esquema absoluto disfarçado", () => {
    expect(safeInternalPath("https://evil.com")).toBeUndefined()
  })

  it("rejeita string vazia, null e undefined", () => {
    expect(safeInternalPath("")).toBeUndefined()
    expect(safeInternalPath(null)).toBeUndefined()
    expect(safeInternalPath(undefined)).toBeUndefined()
  })
})
