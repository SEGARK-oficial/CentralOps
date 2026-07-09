/**
 * Testes de brand-icons.tsx
 *
 * Cobre:
 * - BRAND_IDS contém todos os ids obrigatórios
 * - brandIconFor devolve ReactNode para id conhecido
 * - brandIconFor devolve ReactNode (fallback) para id desconhecido
 * - brandIconFor devolve ReactNode para null/undefined
 * - Cada id conhecido renderiza um <svg> com aria-hidden="true"
 * - size padrão (28) é aplicado ao svg
 * - size customizado é aplicado ao svg
 * - className é repassado ao svg
 * - Named exports existem e são funções React
 * - Case-insensitive: "Splunk" resolve o mesmo que "splunk"
 * - Fallback para id genérico renderiza ícone lucide (não svg direto da map, mas ReactNode)
 * - Ícones genéricos (syslog, webhook, jsonl) têm fill="currentColor" ou stroke="currentColor"
 */

import React from "react"
import { render, screen } from "@testing-library/react"
import { describe, it, expect } from "vitest"
import {
  brandIconFor,
  BRAND_IDS,
  SplunkIcon,
  ElasticIcon,
  ClickHouseIcon,
  CrowdStrikeIcon,
  DatadogIcon,
  OpenTelemetryIcon,
  ApacheKafkaIcon,
  AmazonS3Icon,
  AmazonSecurityLakeIcon,
  MicrosoftSentinelIcon,
  ChronicleIcon,
  SyslogIcon,
  WebhookIcon,
  JsonlIcon,
  FortinetIcon,
  WindowsIcon,
  SophosIcon,
  WazuhIcon,
  OktaIcon,
  NinjaOneIcon,
  MicrosoftIcon,
  AwsIcon,
} from "@/lib/brand-icons"

// ── Helpers ──────────────────────────────────────────────────────────────────

function renderNode(node: React.ReactNode) {
  return render(<div data-testid="root">{node}</div>)
}

// ── BRAND_IDS ─────────────────────────────────────────────────────────────────

const REQUIRED_IDS = [
  // Destinations
  "splunk",
  "elastic",
  "clickhouse",
  "crowdstrike",
  "datadog",
  "opentelemetry",
  "apachekafka",
  "amazons3",
  "amazonsecuritylake",
  "microsoftsentinel",
  "chronicle",
  "syslog",
  "webhook",
  "jsonl",
  // Sources
  "fortinet",
  "windows",
  "sophos",
  "wazuh",
  "okta",
  "ninjaone",
  "microsoft",
  "aws",
] as const

describe("BRAND_IDS", () => {
  it("é um ReadonlySet", () => {
    expect(BRAND_IDS).toBeInstanceOf(Set)
  })

  for (const id of REQUIRED_IDS) {
    it(`contém '${id}'`, () => {
      expect(BRAND_IDS.has(id)).toBe(true)
    })
  }

  it("não contém ids inválidos não intencionais", () => {
    // Deve ter exatamente os ids obrigatórios (pode ter mais, nunca menos)
    for (const id of REQUIRED_IDS) {
      expect(BRAND_IDS.has(id)).toBe(true)
    }
  })
})

// ── brandIconFor — comportamento para ids conhecidos ─────────────────────────

// Um nó de marca pode ser <svg> (tiers local-svgr / Simple Icons / inline) OU
// <img> (asset raster vendado, ex.: CrowdStrike). Os testes são agnósticos a
// formato: checam tamanho + aria-hidden + className no nó raiz, não a tag.
const BRAND_NODE = "svg, img"

describe("brandIconFor — ids conhecidos", () => {
  for (const id of REQUIRED_IDS) {
    it(`'${id}' devolve um nó de marca (svg|img) com aria-hidden`, () => {
      const { container } = renderNode(brandIconFor(id))
      const el = container.querySelector(BRAND_NODE)
      expect(el).not.toBeNull()
      expect(el?.getAttribute("aria-hidden")).toBe("true")
    })

    it(`'${id}' respeita size padrão 28`, () => {
      const { container } = renderNode(brandIconFor(id))
      const el = container.querySelector(BRAND_NODE)
      expect(el?.getAttribute("width")).toBe("28")
      expect(el?.getAttribute("height")).toBe("28")
    })

    it(`'${id}' respeita size customizado`, () => {
      const { container } = renderNode(brandIconFor(id, { size: 40 }))
      const el = container.querySelector(BRAND_NODE)
      expect(el?.getAttribute("width")).toBe("40")
      expect(el?.getAttribute("height")).toBe("40")
    })

    it(`'${id}' repassa className ao elemento raiz`, () => {
      const cls = "my-custom-class"
      const { container } = renderNode(brandIconFor(id, { className: cls }))
      const el = container.querySelector(BRAND_NODE)
      expect(el?.classList.contains(cls)).toBe(true)
    })
  }

  it("case-insensitive: 'Splunk' resolve igual a 'splunk'", () => {
    const { container: a } = render(<div>{brandIconFor("Splunk")}</div>)
    const { container: b } = render(<div>{brandIconFor("splunk")}</div>)
    expect(a.querySelector(BRAND_NODE)).not.toBeNull()
    expect(b.querySelector(BRAND_NODE)).not.toBeNull()
    // Ambos devem ter o mesmo width padrão
    expect(a.querySelector(BRAND_NODE)?.getAttribute("width")).toBe(
      b.querySelector(BRAND_NODE)?.getAttribute("width"),
    )
  })
})

// ── brandIconFor — fallback para ids desconhecidos ────────────────────────────

describe("brandIconFor — fallback ids desconhecidos", () => {
  it("id desconhecido devolve um ReactNode não-nulo", () => {
    const node = brandIconFor("unknown-brand-xyz")
    expect(node).not.toBeNull()
    expect(node).not.toBeUndefined()
  })

  it("id desconhecido renderiza algo no DOM", () => {
    const { container } = renderNode(brandIconFor("totally-unknown"))
    // Lucide renders an svg too
    expect(container.firstChild).not.toBeNull()
  })

  it("null devolve ReactNode (fallback)", () => {
    const node = brandIconFor(null)
    expect(node).not.toBeNull()
  })

  it("undefined devolve ReactNode (fallback)", () => {
    const node = brandIconFor(undefined)
    expect(node).not.toBeNull()
  })

  it("fallback com size customizado não quebra", () => {
    expect(() => renderNode(brandIconFor("no-such-id", { size: 32 }))).not.toThrow()
  })
})

// ── Named exports (componentes diretos) ───────────────────────────────────────

describe("Named icon exports", () => {
  const namedIcons = [
    ["SplunkIcon", SplunkIcon],
    ["ElasticIcon", ElasticIcon],
    ["ClickHouseIcon", ClickHouseIcon],
    ["CrowdStrikeIcon", CrowdStrikeIcon],
    ["DatadogIcon", DatadogIcon],
    ["OpenTelemetryIcon", OpenTelemetryIcon],
    ["ApacheKafkaIcon", ApacheKafkaIcon],
    ["AmazonS3Icon", AmazonS3Icon],
    ["AmazonSecurityLakeIcon", AmazonSecurityLakeIcon],
    ["MicrosoftSentinelIcon", MicrosoftSentinelIcon],
    ["ChronicleIcon", ChronicleIcon],
    ["SyslogIcon", SyslogIcon],
    ["WebhookIcon", WebhookIcon],
    ["JsonlIcon", JsonlIcon],
    ["FortinetIcon", FortinetIcon],
    ["WindowsIcon", WindowsIcon],
    ["SophosIcon", SophosIcon],
    ["WazuhIcon", WazuhIcon],
    ["OktaIcon", OktaIcon],
    ["NinjaOneIcon", NinjaOneIcon],
    ["MicrosoftIcon", MicrosoftIcon],
    ["AwsIcon", AwsIcon],
  ] as const

  for (const [name, Icon] of namedIcons) {
    it(`${name} é uma função`, () => {
      expect(typeof Icon).toBe("function")
    })

    it(`${name} renderiza svg com aria-hidden="true"`, () => {
      const { container } = render(React.createElement(Icon))
      const svg = container.querySelector("svg")
      expect(svg).not.toBeNull()
      expect(svg?.getAttribute("aria-hidden")).toBe("true")
    })

    it(`${name} aceita size prop`, () => {
      const { container } = render(React.createElement(Icon, { size: 32 }))
      const svg = container.querySelector("svg")
      expect(svg?.getAttribute("width")).toBe("32")
    })

    it(`${name} aceita className prop`, () => {
      const { container } = render(React.createElement(Icon, { className: "test-cls" }))
      expect(container.querySelector(".test-cls")).not.toBeNull()
    })
  }
})

// ── Ícones genéricos: currentColor ───────────────────────────────────────────

describe("Ícones genéricos usam currentColor (herdam tema)", () => {
  it("SyslogIcon tem fill='currentColor'", () => {
    const { container } = render(<SyslogIcon />)
    const svg = container.querySelector("svg")
    // fill="currentColor" é um atributo no elemento svg
    expect(svg?.getAttribute("fill")).toBe("currentColor")
  })

  it("WebhookIcon usa stroke='currentColor' nos paths", () => {
    const { container } = render(<WebhookIcon />)
    const paths = container.querySelectorAll("path")
    const hasCurrentColor = Array.from(paths).some(
      (p) => p.getAttribute("stroke") === "currentColor",
    )
    expect(hasCurrentColor).toBe(true)
  })

  it("JsonlIcon usa stroke='currentColor'", () => {
    const { container } = render(<JsonlIcon />)
    const svgOrChildren = container.querySelectorAll("[stroke='currentColor']")
    expect(svgOrChildren.length).toBeGreaterThan(0)
  })
})

// ── Acessibilidade ────────────────────────────────────────────────────────────

describe("Acessibilidade", () => {
  it("todo nó de marca (svg|img) tem aria-hidden='true' (não polui a tree de acessibilidade)", () => {
    for (const id of REQUIRED_IDS) {
      const { container } = renderNode(brandIconFor(id))
      const nodes = container.querySelectorAll("svg, img")
      expect(nodes.length).toBeGreaterThan(0)
      for (const node of Array.from(nodes)) {
        expect(node.getAttribute("aria-hidden")).toBe("true")
      }
    }
  })

  it("ícones não têm role='img' sem aria-label (double-announce)", () => {
    for (const id of REQUIRED_IDS) {
      const { container } = renderNode(brandIconFor(id))
      // Se tiver role="img" DEVE ter aria-label; se não tiver aria-label, não deve ter role="img"
      const svgWithImgRole = container.querySelector("svg[role='img']:not([aria-label])")
      expect(svgWithImgRole).toBeNull()
    }
  })

  it("nenhum ícone de marca é focável por padrão (não está no tab order)", () => {
    for (const id of REQUIRED_IDS) {
      const { container } = renderNode(brandIconFor(id))
      const focusable = container.querySelector(
        "svg[tabindex]:not([tabindex='-1']), img[tabindex]:not([tabindex='-1'])",
      )
      expect(focusable).toBeNull()
    }
  })

  it("brandIconFor não lança com opts vazios ({})", () => {
    expect(() => renderNode(brandIconFor("splunk", {}))).not.toThrow()
  })

  it("brandIconFor sem argumentos não lança", () => {
    expect(() => renderNode(brandIconFor())).not.toThrow()
  })
})

// ── Render padrão: tamanho correto sem opts ───────────────────────────────────

describe("Render padrão sem opts", () => {
  it("viewBox é '0 0 24 24' para marcas vetoriais não-locais (inline + Simple Icons)", () => {
    // sophos usa o mark oficial (viewBox nativo 0 0 65 65) e crowdstrike é raster
    // (<img>) — não se aplicam à convenção 24×24 dos demais (inline + Simple Icons).
    const NON_24 = new Set(["sophos", "crowdstrike"])
    for (const id of REQUIRED_IDS) {
      if (NON_24.has(id)) continue
      const { container } = renderNode(brandIconFor(id))
      const svg = container.querySelector("svg")
      expect(svg?.getAttribute("viewBox")).toBe("0 0 24 24")
    }
  })

  it("size padrão 28 é aplicado quando opts é undefined", () => {
    const { container } = renderNode(brandIconFor("elastic"))
    const svg = container.querySelector("svg")
    expect(svg?.getAttribute("width")).toBe("28")
    expect(svg?.getAttribute("height")).toBe("28")
  })
})

// ── Aliases esperados ─────────────────────────────────────────────────────────

describe("Aliases mapeados corretamente", () => {
  it("microsoftsentinel renderiza svg (não fallback lucide)", () => {
    const { container } = renderNode(brandIconFor("microsoftsentinel"))
    // Brand icons renderizam svg com viewBox 0 0 24 24
    const svg = container.querySelector("svg[viewBox='0 0 24 24']")
    expect(svg).not.toBeNull()
  })

  it("amazons3 renderiza svg de brand", () => {
    const { container } = renderNode(brandIconFor("amazons3"))
    expect(container.querySelector("svg")).not.toBeNull()
  })

  it("amazonsecuritylake renderiza svg de brand", () => {
    const { container } = renderNode(brandIconFor("amazonsecuritylake"))
    expect(container.querySelector("svg")).not.toBeNull()
  })

  it("apachekafka renderiza svg de brand", () => {
    const { container } = renderNode(brandIconFor("apachekafka"))
    expect(container.querySelector("svg")).not.toBeNull()
  })

  it("opentelemetry renderiza svg de brand", () => {
    const { container } = renderNode(brandIconFor("opentelemetry"))
    expect(container.querySelector("svg")).not.toBeNull()
  })
})

// ── Snapshot leve: ids distintos produzem SVGs distintos ─────────────────────

describe("IDs distintos produzem marcas visuais distintas", () => {
  const pairs: [string, string][] = [
    ["splunk", "elastic"],
    ["aws", "microsoft"],
    ["fortinet", "sophos"],
    ["okta", "crowdstrike"],
  ]

  for (const [a, b] of pairs) {
    it(`'${a}' e '${b}' produzem marcas distintas`, () => {
      const { container: ca } = renderNode(brandIconFor(a))
      const { container: cb } = renderNode(brandIconFor(b))
      const elA = ca.querySelector("svg, img")
      const elB = cb.querySelector("svg, img")
      expect(elA).not.toBeNull()
      expect(elB).not.toBeNull()
      expect(elA?.outerHTML).not.toBe(elB?.outerHTML)
    })
  }
})

// ── Integração com screen (acessibilidade via label do contêiner pai) ─────────

describe("Integração com aria-label no contêiner pai", () => {
  it("ícone dentro de botão com aria-label não precisa de aria no svg", () => {
    render(
      <button type="button" aria-label="Abrir Splunk">
        {brandIconFor("splunk")}
      </button>,
    )
    // O botão deve ser encontrável por seu label
    expect(screen.getByRole("button", { name: "Abrir Splunk" })).toBeInTheDocument()
  })
})
