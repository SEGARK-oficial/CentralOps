import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiRequestError, createServiceAccount, formatValidationDetail } from "@/services/api"

// 422 real devolvido pelo backend quando o nome do Service Account tem espaço
// (ServiceAccountCreate.validate_name). Era este array que chegava cru na tela.
const PYDANTIC_422 = [
  {
    type: "value_error",
    loc: ["body", "name"],
    msg: "Value error, name must be alphanumeric (also allowed: '-', '_', '.')",
    input: "MCP Claude",
    ctx: { error: {} },
  },
]

function mockFetch(status: number, body: unknown) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: false,
    status,
    json: async () => body,
  } as unknown as Response)
  vi.stubGlobal("fetch", fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("formatValidationDetail", () => {
  it("turns a Pydantic 422 array into 'field: message'", () => {
    expect(formatValidationDetail(PYDANTIC_422)).toBe(
      "name: name must be alphanumeric (also allowed: '-', '_', '.')",
    )
  })

  it("joins multiple errors and keeps nested field paths", () => {
    expect(
      formatValidationDetail([
        { type: "missing", loc: ["body", "role"], msg: "Field required" },
        { type: "value_error", loc: ["body", "rules", 0, "field"], msg: "Value error, bad field" },
      ]),
    ).toBe("role: Field required; rules.0.field: bad field")
  })

  it("keeps a bare loc that is not a request-part prefix", () => {
    expect(formatValidationDetail([{ loc: ["name"], msg: "too short" }])).toBe("name: too short")
  })

  it("returns null for anything that is not a Pydantic error list", () => {
    expect(formatValidationDetail("boom")).toBeNull()
    expect(formatValidationDetail([])).toBeNull()
    expect(formatValidationDetail([{ nope: 1 }])).toBeNull()
  })
})

describe("apiRequest 422 handling", () => {
  it("surfaces a readable message instead of the raw Pydantic array", async () => {
    mockFetch(422, { detail: PYDANTIC_422 })

    const err = await createServiceAccount({ name: "MCP Claude", role: "viewer" }).then(
      () => null,
      (e: unknown) => e,
    )

    expect(err).toBeInstanceOf(ApiRequestError)
    const message = (err as ApiRequestError).message
    // Positivo: a mensagem que o usuário deve ler.
    expect(message).toBe("name: name must be alphanumeric (also allowed: '-', '_', '.')")
    // Negativo: nada do array cru sobrou (era isto que aparecia na tela).
    expect(message).not.toContain("value_error")
    expect(message).not.toContain("[{")
    expect(message).not.toContain('"loc"')
    expect((err as ApiRequestError).statusCode).toBe(422)
  })

  it("still stringifies a detail object that is not a validation list", async () => {
    mockFetch(400, { detail: { reason: "nope" } })

    const err = await createServiceAccount({ name: "ok", role: "viewer" }).then(
      () => null,
      (e: unknown) => e,
    )

    expect((err as ApiRequestError).message).toBe('{"reason":"nope"}')
  })
})
