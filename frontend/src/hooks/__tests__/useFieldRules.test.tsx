/**
 * Testes de useFieldRules
 * Cobre: fetch + build index, lookup por (vendor, event_type, path),
 * matching bidirecional (parent/child), cache hit, abort no unmount, erro.
 */

import { renderHook, waitFor } from "@testing-library/react"
import { useFieldRules, _resetFieldRulesCache, normalizeJmesPath } from "@/hooks/useFieldRules"

// ── Fixtures ──────────────────────────────────────────────────────────────────

const DEF_SOPHOS = {
  id: "def-sophos-detection",
  vendor: "sophos",
  event_type: "detection",
  current_version_id: "ver-s1",
}

const DEF_WAZUH = {
  id: "def-wazuh-auth",
  vendor: "wazuh",
  event_type: "authentication",
  current_version_id: "ver-w1",
}

const DEF_NO_VERSION = {
  id: "def-nover",
  vendor: "sophos",
  event_type: "event",
  current_version_id: null,
}

// Versão da Sophos: regra scalar, array_builder, fallback, preprocess
const VERSION_SOPHOS = {
  id: "ver-s1",
  definition_id: "def-sophos-detection",
  version_number: 1,
  author_user_id: null,
  commit_message: "init",
  diff_from_previous: null,
  dry_run_stats: null,
  created_at: "2026-01-01T00:00:00Z",
  rules: {
    preprocess: [
      { op: "json_parse", source: "details.rawData", target: "_parsed", tolerant: true },
      // source começa com "_" — NÃO deve ser indexado
      { op: "json_parse", source: "_parsed.extra", target: "_extraParsed", tolerant: true },
    ],
    rules: [
      // scalar primário
      {
        target: "normalized.severity",
        source: "threat.severity",
      },
      // scalar com fallback
      {
        target: "normalized.user",
        source: "threat.actor",
        fallback_source: ["user.name", "identity.login"],
      },
      // array_builder
      {
        target: "normalized.observables",
        kind: "array_builder",
        items: [
          { name: "src_ip", type: "IP Address", type_id: 2, source: "network.sourceIp" },
          { name: "dst_ip", type: "IP Address", type_id: 2, source: "network.destIp" },
        ],
      },
    ],
  },
}

// Versão do Wazuh: só scalar simples
const VERSION_WAZUH = {
  id: "ver-w1",
  definition_id: "def-wazuh-auth",
  version_number: 1,
  author_user_id: null,
  commit_message: "init",
  diff_from_previous: null,
  dry_run_stats: null,
  created_at: "2026-01-01T00:00:00Z",
  rules: {
    preprocess: [],
    rules: [
      { target: "normalized.username", source: "data.srcuser" },
      { target: "normalized.hostname", source: "agent.name" },
    ],
  },
}

// ── Mocks de fetch ────────────────────────────────────────────────────────────

function mockFetch(
  defsResponse: object[],
  versionMap: Record<string, object>,
) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string) => {
      if (url === "/api/mappings") {
        return Promise.resolve({
          ok: true,
          json: async () => defsResponse,
        })
      }

      // Matches /api/mappings/{defId}/versions/{verId}
      const match = url.match(/\/api\/mappings\/([^/]+)\/versions\/([^/]+)/)
      if (match) {
        const verId = match[2]
        const version = versionMap[verId]
        if (version) {
          return Promise.resolve({
            ok: true,
            json: async () => version,
          })
        }
        return Promise.resolve({ ok: false, status: 404 })
      }

      return Promise.resolve({ ok: false, status: 404 })
    }),
  )
}

// ── beforeEach / afterEach ────────────────────────────────────────────────────

beforeEach(() => {
  vi.clearAllMocks()
  _resetFieldRulesCache()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

// ── normalizeJmesPath ─────────────────────────────────────────────────────────

describe("normalizeJmesPath", () => {
  it("remove [*]", () => {
    expect(normalizeJmesPath("items[*].value")).toBe("items.value")
  })

  it("remove [N]", () => {
    expect(normalizeJmesPath("endpoint[0].address")).toBe("endpoint.address")
  })

  it("dotted path sem operadores fica igual", () => {
    expect(normalizeJmesPath("data.nested.field")).toBe("data.nested.field")
  })

  it("path simples sem ponto fica igual", () => {
    expect(normalizeJmesPath("severity")).toBe("severity")
  })
})

// ── useFieldRules — fetch e build ─────────────────────────────────────────────

describe("useFieldRules — fetch e build do índice", () => {
  it("inicia com loading=true e data=null", () => {
    mockFetch([DEF_SOPHOS], { "ver-s1": VERSION_SOPHOS })
    const { result } = renderHook(() => useFieldRules())
    expect(result.current.loading).toBe(true)
    expect(result.current.data).toBeNull()
    expect(result.current.error).toBeNull()
  })

  it("depois de fetch, loading=false e data não é null", async () => {
    mockFetch([DEF_SOPHOS], { "ver-s1": VERSION_SOPHOS })
    const { result } = renderHook(() => useFieldRules())
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.data).not.toBeNull()
    expect(result.current.error).toBeNull()
  })

  it("definitions sem current_version_id são ignoradas silenciosamente", async () => {
    mockFetch([DEF_NO_VERSION], {})
    const { result } = renderHook(() => useFieldRules())
    await waitFor(() => expect(result.current.loading).toBe(false))
    // Índice construído, mas vazio — count deve ser 0 para qualquer path
    expect(result.current.data?.count("sophos", "event", "any.path")).toBe(0)
  })
})

// ── useFieldRules — lookup scalar primário ────────────────────────────────────

describe("useFieldRules — lookup scalar primário", () => {
  it("encontra regra pelo source exato", async () => {
    mockFetch([DEF_SOPHOS], { "ver-s1": VERSION_SOPHOS })
    const { result } = renderHook(() => useFieldRules())
    await waitFor(() => expect(result.current.loading).toBe(false))

    const matches = result.current.data!.lookup("sophos", "detection", "threat.severity")
    expect(matches).toHaveLength(1)
    expect(matches[0].match_kind).toBe("primary")
    expect(matches[0].rule_target).toBe("normalized.severity")
    expect(matches[0].source).toBe("threat.severity")
  })

  it("count retorna 1 para source exato", async () => {
    mockFetch([DEF_SOPHOS], { "ver-s1": VERSION_SOPHOS })
    const { result } = renderHook(() => useFieldRules())
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.data!.count("sophos", "detection", "threat.severity")).toBe(1)
  })

  it("não encontra source de outro vendor", async () => {
    mockFetch([DEF_SOPHOS, DEF_WAZUH], { "ver-s1": VERSION_SOPHOS, "ver-w1": VERSION_WAZUH })
    const { result } = renderHook(() => useFieldRules())
    await waitFor(() => expect(result.current.loading).toBe(false))

    // "data.srcuser" pertence ao wazuh — não deve aparecer em sophos
    expect(result.current.data!.count("sophos", "detection", "data.srcuser")).toBe(0)
  })
})

// ── useFieldRules — lookup fallback ──────────────────────────────────────────

describe("useFieldRules — lookup fallback", () => {
  it("encontra regra via fallback_source", async () => {
    mockFetch([DEF_SOPHOS], { "ver-s1": VERSION_SOPHOS })
    const { result } = renderHook(() => useFieldRules())
    await waitFor(() => expect(result.current.loading).toBe(false))

    const matches = result.current.data!.lookup("sophos", "detection", "user.name")
    expect(matches).toHaveLength(1)
    expect(matches[0].match_kind).toBe("fallback")
    expect(matches[0].source).toBe("user.name")
  })

  it("encontra segundo fallback (identity.login)", async () => {
    mockFetch([DEF_SOPHOS], { "ver-s1": VERSION_SOPHOS })
    const { result } = renderHook(() => useFieldRules())
    await waitFor(() => expect(result.current.loading).toBe(false))

    const matches = result.current.data!.lookup("sophos", "detection", "identity.login")
    expect(matches[0].match_kind).toBe("fallback")
    expect(matches[0].source).toBe("identity.login")
  })
})

// ── useFieldRules — lookup array_builder_item ─────────────────────────────────

describe("useFieldRules — lookup array_builder_item", () => {
  it("encontra source de item de array_builder", async () => {
    mockFetch([DEF_SOPHOS], { "ver-s1": VERSION_SOPHOS })
    const { result } = renderHook(() => useFieldRules())
    await waitFor(() => expect(result.current.loading).toBe(false))

    const matches = result.current.data!.lookup("sophos", "detection", "network.sourceIp")
    expect(matches).toHaveLength(1)
    expect(matches[0].match_kind).toBe("array_builder_item")
    expect(matches[0].rule_target).toBe("normalized.observables")
  })
})

// ── useFieldRules — preprocess ────────────────────────────────────────────────

describe("useFieldRules — preprocess", () => {
  it("source de preprocess que é path raw real é indexado", async () => {
    mockFetch([DEF_SOPHOS], { "ver-s1": VERSION_SOPHOS })
    const { result } = renderHook(() => useFieldRules())
    await waitFor(() => expect(result.current.loading).toBe(false))

    // details.rawData é um path real — deve aparecer
    const matches = result.current.data!.lookup("sophos", "detection", "details.rawData")
    expect(matches).toHaveLength(1)
    expect(matches[0].match_kind).toBe("preprocess")
    expect(matches[0].source).toBe("details.rawData")
  })

  it("source de preprocess que começa com _ é excluído do índice", async () => {
    mockFetch([DEF_SOPHOS], { "ver-s1": VERSION_SOPHOS })
    const { result } = renderHook(() => useFieldRules())
    await waitFor(() => expect(result.current.loading).toBe(false))

    // _parsed.extra começa com "_" — é referência virtual, não indexada
    expect(result.current.data!.count("sophos", "detection", "_parsed.extra")).toBe(0)
  })
})

// ── useFieldRules — matching bidirecional (parent/child) ─────────────────────

describe("useFieldRules — matching bidirecional", () => {
  it("drift com path 'threat' (parent) encontra regra com source 'threat.severity' (child)", async () => {
    mockFetch([DEF_SOPHOS], { "ver-s1": VERSION_SOPHOS })
    const { result } = renderHook(() => useFieldRules())
    await waitFor(() => expect(result.current.loading).toBe(false))

    // "threat" é ancestral de "threat.severity" — deve dar match
    const matches = result.current.data!.lookup("sophos", "detection", "threat")
    expect(matches.some((m) => m.source === "threat.severity")).toBe(true)
  })

  it("drift com path 'network.sourceIp.extra' (child) encontra regra com source 'network.sourceIp' (parent)", async () => {
    mockFetch([DEF_SOPHOS], { "ver-s1": VERSION_SOPHOS })
    const { result } = renderHook(() => useFieldRules())
    await waitFor(() => expect(result.current.loading).toBe(false))

    const matches = result.current.data!.lookup("sophos", "detection", "network.sourceIp.extra")
    expect(matches.some((m) => m.source === "network.sourceIp")).toBe(true)
  })

  it("sem match quando paths não têm relação", async () => {
    mockFetch([DEF_SOPHOS], { "ver-s1": VERSION_SOPHOS })
    const { result } = renderHook(() => useFieldRules())
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.data!.count("sophos", "detection", "completely.unrelated.path")).toBe(0)
  })
})

// ── useFieldRules — múltiplos vendors ────────────────────────────────────────

describe("useFieldRules — múltiplos mappings", () => {
  it("lookup isolado por (vendor, event_type)", async () => {
    mockFetch([DEF_SOPHOS, DEF_WAZUH], { "ver-s1": VERSION_SOPHOS, "ver-w1": VERSION_WAZUH })
    const { result } = renderHook(() => useFieldRules())
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.data!.count("wazuh", "authentication", "agent.name")).toBe(1)
    expect(result.current.data!.count("sophos", "detection", "agent.name")).toBe(0)
  })
})

// ── useFieldRules — cache ─────────────────────────────────────────────────────

describe("useFieldRules — cache em módulo", () => {
  it("segunda montagem não dispara novo fetch", async () => {
    mockFetch([DEF_SOPHOS], { "ver-s1": VERSION_SOPHOS })

    const { result: r1, unmount: u1 } = renderHook(() => useFieldRules())
    await waitFor(() => expect(r1.current.loading).toBe(false))
    const callCount = (vi.mocked(fetch) as ReturnType<typeof vi.fn>).mock.calls.length
    u1()

    const { result: r2 } = renderHook(() => useFieldRules())
    // Cache hit é síncrono: loading=false imediatamente
    expect(r2.current.loading).toBe(false)
    expect(r2.current.data).not.toBeNull()
    // Não fez chamadas adicionais
    expect((vi.mocked(fetch) as ReturnType<typeof vi.fn>).mock.calls.length).toBe(callCount)
  })
})

// ── useFieldRules — unmount durante fetch ─────────────────────────────────────

describe("useFieldRules — unmount durante fetch", () => {
  it("abort no unmount: sem warning de setState", async () => {
    let abortFired = false

    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string, opts?: RequestInit) => {
        if (url === "/api/mappings") {
          return new Promise((_, rej) => {
            opts?.signal?.addEventListener("abort", () => {
              abortFired = true
              rej(new DOMException("Aborted", "AbortError"))
            })
          })
        }
        return Promise.resolve({ ok: false, status: 404 })
      }),
    )

    const { result, unmount } = renderHook(() => useFieldRules())
    expect(result.current.loading).toBe(true)
    unmount()

    await new Promise((r) => setTimeout(r, 30))
    expect(abortFired).toBe(true)
    expect(result.current.error).toBeNull()
  })
})

// ── useFieldRules — erro de rede ──────────────────────────────────────────────

describe("useFieldRules — erro de rede", () => {
  it("erro HTTP 500: error populado, loading=false, data=null", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 500 }),
    )

    const { result } = renderHook(() => useFieldRules())
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.data).toBeNull()
    expect(result.current.error).toBeInstanceOf(Error)
    expect(result.current.error!.message).toMatch(/500/)
  })
})
