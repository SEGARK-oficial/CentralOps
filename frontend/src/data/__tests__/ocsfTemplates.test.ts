/**
 * Testes de ocsfTemplates
 * Valida a integridade estrutural dos templates OCSF estáticos.
 */

import { OCSF_TEMPLATES } from "@/data/ocsfTemplates"
import type { ScalarMappingRule } from "@/types"

describe("ocsfTemplates — integridade estrutural", () => {
  it("exporta exatamente 3 templates", () => {
    expect(OCSF_TEMPLATES).toHaveLength(3)
  })

  it("todos os ids são únicos", () => {
    const ids = OCSF_TEMPLATES.map((t) => t.id)
    const unique = new Set(ids)
    expect(unique.size).toBe(ids.length)
  })

  it("cada template tem pelo menos 8 regras", () => {
    for (const template of OCSF_TEMPLATES) {
      expect(template.rules.length).toBeGreaterThanOrEqual(8)
    }
  })

  it("todos os targets começam com 'normalized.'", () => {
    for (const template of OCSF_TEMPLATES) {
      for (const rule of template.rules) {
        expect(rule.target).toMatch(/^normalized\./)
      }
    }
  })

  it("regra class_uid usa 'const', não 'source'", () => {
    for (const template of OCSF_TEMPLATES) {
      const rule = template.rules.find(
        (r) => r.target === "normalized.class_uid",
      ) as ScalarMappingRule | undefined

      expect(rule).toBeDefined()
      expect((rule as ScalarMappingRule).const).toBeDefined()
      expect((rule as ScalarMappingRule).source).toBeUndefined()
    }
  })

  it("regra category_uid usa 'const', não 'source'", () => {
    for (const template of OCSF_TEMPLATES) {
      const rule = template.rules.find(
        (r) => r.target === "normalized.category_uid",
      ) as ScalarMappingRule | undefined

      expect(rule).toBeDefined()
      expect((rule as ScalarMappingRule).const).toBeDefined()
      expect((rule as ScalarMappingRule).source).toBeUndefined()
    }
  })

  it("regra severity_id tem value_map não vazio", () => {
    for (const template of OCSF_TEMPLATES) {
      const rule = template.rules.find(
        (r) => r.target === "normalized.severity_id",
      ) as ScalarMappingRule | undefined

      expect(rule).toBeDefined()
      const vm = (rule as ScalarMappingRule).value_map
      expect(vm).toBeDefined()
      expect(Object.keys(vm!).length).toBeGreaterThan(0)
    }
  })

  it("severity_id value_map corresponde ao SEVERITY_ID de classes.py", () => {
    const expectedMap = {
      unknown: 0,
      informational: 1,
      low: 2,
      medium: 3,
      high: 4,
      critical: 5,
      fatal: 6,
      other: 99,
    }

    for (const template of OCSF_TEMPLATES) {
      const rule = template.rules.find(
        (r) => r.target === "normalized.severity_id",
      ) as ScalarMappingRule | undefined

      expect((rule as ScalarMappingRule).value_map).toEqual(expectedMap)
    }
  })

  it("Detection Finding tem class_uid=2004 e category_uid=2", () => {
    const template = OCSF_TEMPLATES.find((t) => t.id === "detection_finding_2004")!
    expect(template).toBeDefined()
    expect(template.class_uid).toBe(2004)
    expect(template.category_uid).toBe(2)

    const classRule = template.rules.find((r) => r.target === "normalized.class_uid") as ScalarMappingRule
    expect(classRule.const).toBe(2004)

    const catRule = template.rules.find((r) => r.target === "normalized.category_uid") as ScalarMappingRule
    expect(catRule.const).toBe(2)
  })

  it("Incident Finding tem class_uid=2005 e category_uid=2", () => {
    const template = OCSF_TEMPLATES.find((t) => t.id === "incident_finding_2005")!
    expect(template).toBeDefined()
    expect(template.class_uid).toBe(2005)
    expect(template.category_uid).toBe(2)

    const classRule = template.rules.find((r) => r.target === "normalized.class_uid") as ScalarMappingRule
    expect(classRule.const).toBe(2005)
  })

  it("Email Activity tem class_uid=4009 e category_uid=4", () => {
    const template = OCSF_TEMPLATES.find((t) => t.id === "email_activity_4009")!
    expect(template).toBeDefined()
    expect(template.class_uid).toBe(4009)
    expect(template.category_uid).toBe(4)

    const classRule = template.rules.find((r) => r.target === "normalized.class_uid") as ScalarMappingRule
    expect(classRule.const).toBe(4009)
  })

  it("type_uid é calculado corretamente (class_uid * 100 + activity_id)", () => {
    const expectations = [
      { id: "detection_finding_2004", class_uid: 2004, expectedTypeUid: 200401 },
      { id: "incident_finding_2005", class_uid: 2005, expectedTypeUid: 200501 },
      { id: "email_activity_4009", class_uid: 4009, expectedTypeUid: 400901 },
    ]

    for (const { id, expectedTypeUid } of expectations) {
      const template = OCSF_TEMPLATES.find((t) => t.id === id)!
      const typeUidRule = template.rules.find(
        (r) => r.target === "normalized.type_uid",
      ) as ScalarMappingRule

      expect(typeUidRule).toBeDefined()
      expect(typeUidRule.const).toBe(expectedTypeUid)
    }
  })

  it("Detection Finding tem regra finding_info.uid", () => {
    const template = OCSF_TEMPLATES.find((t) => t.id === "detection_finding_2004")!
    const rule = template.rules.find((r) => r.target === "normalized.finding_info.uid")
    expect(rule).toBeDefined()
  })

  it("Incident Finding tem regra incident.uid", () => {
    const template = OCSF_TEMPLATES.find((t) => t.id === "incident_finding_2005")!
    const rule = template.rules.find((r) => r.target === "normalized.incident.uid")
    expect(rule).toBeDefined()
  })

  it("Email Activity tem regras email.from, email.to, email.subject", () => {
    const template = OCSF_TEMPLATES.find((t) => t.id === "email_activity_4009")!
    const targets = template.rules.map((r) => r.target)
    expect(targets).toContain("normalized.email.from")
    expect(targets).toContain("normalized.email.to")
    expect(targets).toContain("normalized.email.subject")
  })

  it("metadata.version tem const='1.5.0' em todos os templates", () => {
    for (const template of OCSF_TEMPLATES) {
      const rule = template.rules.find(
        (r) => r.target === "normalized.metadata.version",
      ) as ScalarMappingRule | undefined
      expect(rule).toBeDefined()
      expect((rule as ScalarMappingRule).const).toBe("1.5.0")
    }
  })
})
