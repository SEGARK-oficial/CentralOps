/**
 * Catálogo de permissões — lógica pura.
 *
 * A guarda de DIVERGÊNCIA com o backend (toda permissão do `Permission`
 * StrEnum precisa ter categoria e descrição) vive no pytest, em
 * `backend/tests/test_permission_catalog_parity.py`: ela precisa ler o fonte
 * Python E os arquivos do frontend, e o Vite recusa importar de fora da raiz
 * do projeto. Aqui ficam só as garantias que não cruzam essa fronteira.
 */

import { describe, it, expect } from "vitest"
import { PERMISSION_CATEGORY_ORDER, categoryOf, groupByCategory } from "@/lib/permissions"

describe("catálogo de permissões", () => {
  it("permissão desconhecida cai numa categoria em vez de sumir", () => {
    // Degradação, não crash: se o backend ganhar uma permissão antes do
    // frontend, ela ainda aparece na tela (e o teste de paridade acusa a dívida).
    expect(categoryOf("algo.que.nao.existe")).toBe("internal")
  })

  it("agrupa preservando a ordem de exibição e omite categoria vazia", () => {
    const grupos = groupByCategory(["user.manage", "integration.read", "mapping.read"])

    expect(grupos.map((g) => g.category)).toEqual(["integrations", "mappings", "administration"])
    // A ordem entre categorias é a de `PERMISSION_CATEGORY_ORDER`, não a de entrada.
    const posicoes = grupos.map((g) => PERMISSION_CATEGORY_ORDER.indexOf(g.category))
    expect(posicoes).toEqual([...posicoes].sort((a, b) => a - b))
  })

  it("ordena as permissões dentro de cada categoria", () => {
    const [grupo] = groupByCategory(["integration.write", "integration.pause", "integration.read"])
    expect(grupo.permissions).toEqual([
      "integration.pause",
      "integration.read",
      "integration.write",
    ])
  })
})
