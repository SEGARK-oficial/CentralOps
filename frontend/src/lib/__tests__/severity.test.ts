/**
 * Testes de severity.ts (Fase 4 / C5)
 * Cobre: healthEncoding, alertEncoding, pipelineEncoding, StatusBadge.
 */

import { describe, it, expect } from "vitest"
import {
  healthEncoding,
  alertEncoding,
  pipelineEncoding,
  HEALTH_MAP,
  ALERT_MAP,
  PIPELINE_MAP,
  StatusBadge,
} from "@/lib/severity"

// ── healthEncoding ──────────────────────────────────────────────────────────

describe("healthEncoding", () => {
  it("healthy → badgeVariant success e label 'Saudável'", () => {
    const enc = healthEncoding("healthy")
    expect(enc.badgeVariant).toBe("success")
    expect(enc.label).toBe("Saudável") // acento correto
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
    expect(healthEncoding("Healthy").label).toBe(healthEncoding("healthy").label)
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

// ── alertEncoding ────────────────────────────────────────────────────────────

describe("alertEncoding", () => {
  it("ok → success", () => {
    expect(alertEncoding("ok").badgeVariant).toBe("success")
  })

  it("warn → warning", () => {
    expect(alertEncoding("warn").badgeVariant).toBe("warning")
  })

  it("error → danger", () => {
    expect(alertEncoding("error").badgeVariant).toBe("danger")
  })

  it("critical → danger + label 'Crítico'", () => {
    const enc = alertEncoding("critical")
    expect(enc.badgeVariant).toBe("danger")
    expect(enc.label).toBe("Crítico")
  })

  it("valor desconhecido → fallback error (danger)", () => {
    expect(alertEncoding("unknown_level").badgeVariant).toBe("danger")
  })

  it("cada nível tem colorToken que começa com 'text-'", () => {
    for (const enc of Object.values(ALERT_MAP)) {
      expect(enc.colorToken).toMatch(/^text-/)
    }
  })

  it("cada nível tem bgToken que começa com 'bg-'", () => {
    for (const enc of Object.values(ALERT_MAP)) {
      expect(enc.bgToken).toMatch(/^bg-/)
    }
  })
})

// ── pipelineEncoding ─────────────────────────────────────────────────────────

describe("pipelineEncoding", () => {
  it("route → primary", () => {
    expect(pipelineEncoding("route").badgeVariant).toBe("primary")
  })

  it("drop → danger (não primary/azul — colorblind-safe)", () => {
    const enc = pipelineEncoding("drop")
    expect(enc.badgeVariant).toBe("danger")
    // label nunca é igual ao de 'route' — diferenciação semântica
    expect(enc.label).not.toBe(pipelineEncoding("route").label)
  })

  it("quarantine → warning", () => {
    expect(pipelineEncoding("quarantine").badgeVariant).toBe("warning")
  })

  it("unknown → outline", () => {
    expect(pipelineEncoding("unknown").badgeVariant).toBe("outline")
  })

  it("null → fallback unknown", () => {
    expect(pipelineEncoding(null).badgeVariant).toBe("outline")
  })

  it("todos os statuses têm Icon ≠ undefined", () => {
    for (const enc of Object.values(PIPELINE_MAP)) {
      expect(enc.Icon).toBeDefined()
    }
  })

  it("drop e route têm Icons diferentes (canal visual independente)", () => {
    expect(pipelineEncoding("drop").iconName).not.toBe(pipelineEncoding("route").iconName)
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
    expect(enc).toHaveProperty("label")
    expect(enc).toHaveProperty("badgeVariant")
    expect(enc).toHaveProperty("iconName")
  })
})
