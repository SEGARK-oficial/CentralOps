import { describe, it, expect } from "vitest"
import { fmtRate } from "@/lib/fmt"

describe("fmtRate", () => {
  it("retorna '0' para valores não-positivos ou não-finitos", () => {
    expect(fmtRate(0)).toBe("0")
    expect(fmtRate(-5)).toBe("0")
    expect(fmtRate(Number.NaN)).toBe("0")
    expect(fmtRate(Number.POSITIVE_INFINITY)).toBe("0")
  })

  it("formata valores < 10 com 1 casa decimal", () => {
    expect(fmtRate(3)).toBe("3.0")
    expect(fmtRate(7)).toBe("7.0")
    expect(fmtRate(9.4)).toBe("9.4")
  })

  it("formata valores >= 10 e < 100 como inteiro", () => {
    expect(fmtRate(10)).toBe("10")
    expect(fmtRate(42)).toBe("42")
    expect(fmtRate(99.6)).toBe("100") // toFixed(0) arredonda
  })

  it("formata valores >= 100 e < 1000 como inteiro arredondado", () => {
    expect(fmtRate(100)).toBe("100")
    expect(fmtRate(120)).toBe("120")
    expect(fmtRate(987.4)).toBe("987")
  })

  it("formata milhares com sufixo 'k' (1 casa decimal de 1k a <10k)", () => {
    expect(fmtRate(1000)).toBe("1.0k")
    expect(fmtRate(1500)).toBe("1.5k")
    expect(fmtRate(9990)).toBe("10.0k")
  })

  it("formata >= 10000 com 'k' sem casas decimais", () => {
    expect(fmtRate(10000)).toBe("10k")
    expect(fmtRate(12000)).toBe("12k")
    expect(fmtRate(150000)).toBe("150k")
  })

  it("produz o MESMO formato para a mesma métrica (consistência cross-UI)", () => {
    // Mesmo valor de EPS deve render igual na topologia e na lista de destinos.
    const eps = 120
    const topologyLabel = fmtRate(eps)
    const destinationsLabel = fmtRate(eps)
    expect(topologyLabel).toBe(destinationsLabel)
    expect(topologyLabel).toBe("120")
  })
})
