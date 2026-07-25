/**
 * Testes de severity.ts (Fase 4 / C5)
 * Cobre: healthEncoding, StatusBadge.
 *
 * ALERT_MAP e PIPELINE_MAP sairam do modulo: nao tinham call site e o
 * PIPELINE_MAP ainda guardava o encoding antigo (route=violeta, drop=vermelhao),
 * contradizendo o FlowCanvas. Os testes deles foram junto.
 */

import { describe, it, expect } from "vitest"
import { healthEncoding, HEALTH_MAP, StatusBadge } from "@/lib/severity"

// ── healthEncoding ──────────────────────────────────────────────────────────

describe("healthEncoding", () => {
  it("healthy → badgeVariant neutro (mesma leitura de HealthBadge/FlowCanvas)", () => {
    const enc = healthEncoding("healthy")
    expect(enc.badgeVariant).toBe("default")
    // Nenhum token de matiz: saudável não pode sair teal em /destinations e
    // cinza em /pipeline-health.
    expect(enc.colorToken).not.toMatch(/success|warning|danger|primary/)
    expect(enc.bgToken).not.toMatch(/success|warning|danger|primary/)
  })

  it("healthy reusa a chave do HealthBadge (as duas telas leem o mesmo texto)", () => {
    expect(healthEncoding("healthy").labelKey).toBe("health.badge.healthy")
    expect(healthEncoding("down").labelKey).toBe("health.badge.unhealthy")
  })

  it("nenhum nível carrega texto fixo no lugar da chave i18n", () => {
    for (const enc of Object.values(HEALTH_MAP)) {
      expect(enc.labelKey).toMatch(/^[a-z][\w.]*\.[\w]+$/)
    }
  })

  it("degraded → badgeVariant warning", () => {
    expect(healthEncoding("degraded").badgeVariant).toBe("warning")
  })

  it("down → badgeVariant danger", () => {
    expect(healthEncoding("down").badgeVariant).toBe("danger")
  })

  it("unknown → badgeVariant outline", () => {
    expect(healthEncoding("unknown").badgeVariant).toBe("outline")
  })

  it("case-insensitive: 'Healthy' resolve igual a 'healthy'", () => {
    expect(healthEncoding("Healthy").labelKey).toBe(healthEncoding("healthy").labelKey)
  })

  it("'unhealthy' do contrato de destino resolve igual a 'down'", () => {
    // Sem o alias caía no fallback: destino fora do ar saía "Aguardando coleta"
    // em /destinations e "Indisponível" em /pipeline-health.
    expect(healthEncoding("unhealthy")).toEqual(healthEncoding("down"))
    expect(healthEncoding("unhealthy").badgeVariant).toBe("danger")
  })

  it("'disabled' é estado próprio e neutro, não 'down'", () => {
    // Colapsar disabled em down acendia vermelhão e dizia "Indisponível" para um
    // destino que o operador tinha acabado de desligar.
    const enc = healthEncoding("disabled")
    expect(enc.badgeVariant).toBe("outline")
    expect(enc.labelKey).toBe("health.badge.disabled")
    expect(enc.labelKey).not.toBe(healthEncoding("down").labelKey)
    expect(enc.colorToken).not.toMatch(/success|warning|danger|primary/)
    expect(enc.bgToken).not.toMatch(/success|warning|danger|primary/)
  })

  it("valor desconhecido → fallback unknown", () => {
    expect(healthEncoding("foobar").badgeVariant).toBe("outline")
  })

  it("null → fallback unknown", () => {
    expect(healthEncoding(null).badgeVariant).toBe("outline")
  })

  it("undefined → fallback unknown", () => {
    expect(healthEncoding(undefined).badgeVariant).toBe("outline")
  })

  it("cada nível tem Icon definido (nunca undefined)", () => {
    for (const enc of Object.values(HEALTH_MAP)) {
      expect(enc.Icon).toBeDefined()
    }
  })

  it("cada nível tem iconName string não-vazio", () => {
    for (const enc of Object.values(HEALTH_MAP)) {
      expect(typeof enc.iconName).toBe("string")
      expect(enc.iconName.length).toBeGreaterThan(0)
    }
  })
})

// ── StatusBadge (componente) ─────────────────────────────────────────────────
// Testamos apenas a forma (não renderizamos DOM aqui para manter como test puro .ts)

describe("StatusBadge export", () => {
  it("é uma função", () => {
    expect(typeof StatusBadge).toBe("function")
  })

  it("aceita encoding de healthEncoding sem erro de tipo", () => {
    const enc = healthEncoding("healthy")
    // Só verifica que o objeto de encoding é estruturalmente completo
    expect(enc).toHaveProperty("Icon")
    expect(enc).toHaveProperty("colorToken")
    expect(enc).toHaveProperty("bgToken")
    expect(enc).toHaveProperty("labelKey")
    expect(enc).toHaveProperty("badgeVariant")
    expect(enc).toHaveProperty("iconName")
  })
})
