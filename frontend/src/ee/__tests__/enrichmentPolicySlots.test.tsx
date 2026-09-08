/**
 * Stub Community do seam de modelo da matriz.
 *
 * O que se protege aqui é o SINALIZADOR, não a feature: o artefato Community
 * não carrega o painel Enterprise, e renderizar `null` no lugar seria um muro
 * invisível — o operador de um MSP não descobriria que existe uma forma de
 * aplicar a mesma política a N clientes, nem a forma manual que ele já tem.
 */

import { render, screen } from "@testing-library/react"
import { describe, it, expect, beforeAll } from "vitest"
import { EnrichmentPolicyTemplatePanel } from "@/ee/enrichmentPolicySlots"
import type { EnrichmentPolicy } from "@/services/api"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

const policy: EnrichmentPolicy = {
  id: "p1",
  organization_id: 1,
  name: "padrao-soc",
  description: null,
  enabled: true,
  current_version_id: "v1",
  rule_count: 2,
  is_active: true,
}

const orgs = [
  { id: 1, name: "Matriz" },
  { id: 2, name: "Filial" },
]

describe("EnrichmentPolicyTemplatePanel (stub Community)", () => {
  it("aponta o caminho que a Community de fato tem", () => {
    render(
      <EnrichmentPolicyTemplatePanel
        policy={policy}
        organizations={orgs}
        onChanged={() => {}}
      />,
    )
    const aviso = screen.getByTestId("enterprise-template-signpost")
    // Nomeia a alternativa manual, não só a ausência da feature.
    expect(aviso).toHaveTextContent(/Copiar para outra organização/i)
  })

  it("cala numa instalação de organização única", () => {
    // Ali o aviso seria propaganda sem função: não há a quem aplicar.
    const { container } = render(
      <EnrichmentPolicyTemplatePanel
        policy={policy}
        organizations={[{ id: 1, name: "Única" }]}
        onChanged={() => {}}
      />,
    )
    expect(container).toBeEmptyDOMElement()
  })
})
