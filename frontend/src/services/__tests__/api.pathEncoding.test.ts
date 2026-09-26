/**
 * Sweep de segurança da Fase 2 aplicou `encodeURIComponent` em ~86 interpolações
 * de path/query no api.ts (services/api.ts). Este teste é o gate contra as duas
 * formas dele dar errado:
 *
 * 1. DUPLA codificação — se algum chamador já mandasse um valor pré-codificado,
 *    ou se um helper interno já tivesse escapado o pedaço, um "%" viraria "%25"
 *    de novo (ex.: "%2F" -> "%252F"). O backend receberia o valor ERRADO
 *    (decodifica uma vez só) e a rota não bateria.
 * 2. Path/query já montado tratado como valor único — ex.: um par
 *    `vendor/event_type` concatenado ANTES do encode faria a barra virar
 *    `%2F` num único segmento, quando o backend espera DOIS segmentos (ou,
 *    no caso do api.ts, os dois vão sempre por query string separada — nunca
 *    concatenados num path).
 *
 * Só há UM `{x:path}` no backend (o catch-all da SPA, ver backend/app/main.py) —
 * toda rota de API real usa segmentos simples, então UM encodeURIComponent por
 * valor é sempre o comportamento correto.
 */
import { afterEach, describe, expect, it, vi } from "vitest"

import { getMappingDiff, getOrganization, waitResults } from "@/services/api"

function mockFetch() {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({}),
  } as unknown as Response)
  vi.stubGlobal("fetch", fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("api.ts — encodeURIComponent nos segmentos de path (sem dupla codificação)", () => {
  it("um valor com caracteres reservados (/, #, espaço) vira UM segmento percent-encoded, não dois", async () => {
    const fetchMock = mockFetch()

    await getMappingDiff("map/1", "v a", "v#b")

    const requestedUrl = fetchMock.mock.calls[0][0] as string
    // A barra de "map/1" tem que estar ESCAPADA — se aparecesse crua, o
    // backend veria um segmento de path A MAIS e a rota não bateria com
    // `/mappings/{definition_id}/versions/{a}/diff/{b}`.
    expect(requestedUrl).toContain("/mappings/map%2F1/versions/v%20a/diff/v%23b")
    // Cada segmento foi codificado EXATAMENTE uma vez (não há "%25" — que
    // seria o "%" de um encode anterior escapado de novo).
    expect(requestedUrl).not.toMatch(/%25/)
  })

  it("um valor que já contém um '%' literal (dado de usuário, não pré-codificado) é escapado uma única vez", async () => {
    const fetchMock = mockFetch()

    // "%" é um caractere de usuário legítimo (ex.: id externo, nome de tenant)
    // — não é sinal de que o valor já veio percent-encoded. Dupla codificação
    // faria "%" (que codifica sozinho para "%25") virar "%2525".
    await waitResults(1, "100%-match")

    const requestedUrl = fetchMock.mock.calls[0][0] as string
    expect(requestedUrl).toContain("/search/1/100%25-match/wait")
    expect(requestedUrl).not.toContain("%2525")
  })

  it("um id numérico simples não é afetado (comportamento pré-sweep preservado)", async () => {
    const fetchMock = mockFetch()

    await getOrganization(42)

    const requestedUrl = fetchMock.mock.calls[0][0] as string
    expect(requestedUrl).toMatch(/\/organizations\/42$/)
  })
})
