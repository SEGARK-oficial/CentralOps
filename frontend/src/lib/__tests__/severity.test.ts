/**
 * Testes de severity.ts (Fase 4 / C5)
 * Cobre: healthEncoding, StatusBadge.
 *
 * ALERT_MAP e PIPELINE_MAP sairam do modulo: nao tinham call site e o
 * PIPELINE_MAP ainda guardava o encoding antigo (route=violeta, drop=vermelhao),
 * contradizendo o FlowCanvas. Os testes deles foram junto.
 */

import { createElement } from "react"
import { describe, it, expect } from "vitest"
import { render, screen } from "@testing-library/react"
import { healthEncoding, HEALTH_MAP, StatusBadge, detectionSeverityEncoding } from "@/lib/severity"

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

// ── detectionSeverityEncoding (LAY-18) ───────────────────────────────────────

describe("detectionSeverityEncoding", () => {
  it("severidade 3 (Média/OCSF) é neutra, NÃO primary (violeta = normalize/OCSF/marca)", () => {
    // DetectionsTable usava variant="primary" para a severidade 3 — a cor
    // errada para um nível de risco médio, e reservada para outro conceito.
    expect(detectionSeverityEncoding(3).badgeVariant).toBe("default")
    expect(detectionSeverityEncoding(3).badgeVariant).not.toBe("primary")
  })

  it("1 e 2 (Informational/Low) são neutras", () => {
    expect(detectionSeverityEncoding(1).badgeVariant).toBe("default")
    expect(detectionSeverityEncoding(2).badgeVariant).toBe("default")
  })

  it("4 (High) é warning", () => {
    expect(detectionSeverityEncoding(4).badgeVariant).toBe("warning")
  })

  it("5 e 6 (Critical/Fatal) são danger", () => {
    expect(detectionSeverityEncoding(5).badgeVariant).toBe("danger")
    expect(detectionSeverityEncoding(6).badgeVariant).toBe("danger")
  })

  it("cada nível 1-6 tem uma labelKey própria (namespace schedules)", () => {
    const keys = new Set<string>()
    for (let id = 1; id <= 6; id++) {
      const { labelKey } = detectionSeverityEncoding(id)
      expect(labelKey.startsWith("schedules:detections.severity.")).toBe(true)
      keys.add(labelKey)
    }
    expect(keys.size).toBe(6)
  })

  it("severidade fora de 1-6 cai no fallback com o id para interpolação", () => {
    const enc = detectionSeverityEncoding(9)
    expect(enc.labelKey).toBe("schedules:detections.severity.unknown")
    expect(enc.labelParams).toEqual({ id: 9 })
    expect(enc.badgeVariant).toBe("danger")
  })
})

// ── StatusBadge — A11Y-34: nome acessível por texto real, não por aria-label
//    num <span> sem role (que a maioria dos leitores de tela ignora) ─────────

describe("StatusBadge — A11Y-34", () => {
  it("com showLabel (padrão), o texto do rótulo é visível — não há aria-label redundante no wrapper", () => {
    render(createElement(StatusBadge, { encoding: healthEncoding("degraded") }))
    const label = screen.getByText("Degradado")
    expect(label.parentElement?.hasAttribute("aria-label")).toBe(false)
  })

  it("com showLabel=false (ícone-apenas), o rótulo continua na árvore de acessibilidade via sr-only", () => {
    render(createElement(StatusBadge, { encoding: healthEncoding("degraded"), showLabel: false }))
    // sr-only: presente no DOM (acessível), mesmo sem aparecer visualmente.
    expect(screen.getByText("Degradado")).toBeInTheDocument()
    expect(screen.getByText("Degradado")).toHaveClass("sr-only")
  })
})
