/**
 * Testes de utils.ts, foco em formatBytes.
 *
 * O caso que motivou o arquivo: `bytes_per_min` é `bytes_window/300`, então um
 * destino de baixo volume manda um valor sub-1 e o formatador cuspia
 * "512 undefined" no card de saúde.
 */

import { describe, it, expect } from "vitest"
import { formatBytes } from "@/lib/utils"

describe("formatBytes", () => {
  it("valor sub-1 não sai como 'undefined'", () => {
    expect(formatBytes(0.5)).toBe("0.5 Bytes")
    expect(formatBytes(0.5)).not.toContain("undefined")
  })

  it("qualquer fração de byte fica em Bytes", () => {
    for (const v of [0.001, 0.25, 0.99]) {
      expect(formatBytes(v)).toMatch(/Bytes$/)
      expect(formatBytes(v)).not.toContain("undefined")
    }
  })

  it("zero e não-finito degradam para '0 Bytes'", () => {
    expect(formatBytes(0)).toBe("0 Bytes")
    expect(formatBytes(Number.NaN)).toBe("0 Bytes")
    expect(formatBytes(Number.POSITIVE_INFINITY)).toBe("0 Bytes")
  })

  it("escala normal continua igual", () => {
    expect(formatBytes(1024)).toBe("1 KB")
    expect(formatBytes(1536)).toBe("1.5 KB")
    expect(formatBytes(1024 ** 3)).toBe("1 GB")
  })

  it("acima da maior unidade satura em vez de estourar o array", () => {
    const huge = formatBytes(1024 ** 9)
    expect(huge).toContain("PB")
    expect(huge).not.toContain("undefined")
  })

  it("negativo não vira NaN nem undefined", () => {
    expect(formatBytes(-2048)).toBe("-2 KB")
    expect(formatBytes(-0.5)).not.toContain("undefined")
  })

  it("decimals negativo é tratado como zero casas", () => {
    expect(formatBytes(1536, -1)).toBe("2 KB")
  })
})
