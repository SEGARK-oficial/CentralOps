/**
 * R2-6.2: fontes self-host (SOC air-gapped não pode chamar fonts.googleapis
 * .com/fonts.gstatic.com) + CSP endurecida (script-src sem 'unsafe-inline',
 * img-src sem `https:` coringa).
 *
 * Guard de regressão por CONTEÚDO dos arquivos de config (não há runtime pra
 * testar aqui — nginx.conf e index.html não passam pelo bundler de teste).
 */
import { readFileSync } from "node:fs"
import { join } from "node:path"

const ROOT = process.cwd()
const GLOBALS_CSS = readFileSync(join(ROOT, "src", "styles", "globals.css"), "utf8")
const INDEX_HTML = readFileSync(join(ROOT, "index.html"), "utf8")
const NGINX_HTTP = readFileSync(join(ROOT, "nginx.single.conf"), "utf8")
const NGINX_HTTPS = readFileSync(join(ROOT, "nginx.single.https.conf"), "utf8")

describe("R2-6.2 — fontes self-host via @fontsource", () => {
  it("globals.css importa as 3 famílias/pesos usados via @fontsource, não Google Fonts", () => {
    expect(GLOBALS_CSS).toMatch(/@import\s+"@fontsource\/archivo\/500\.css"/)
    expect(GLOBALS_CSS).toMatch(/@import\s+"@fontsource\/archivo\/600\.css"/)
    expect(GLOBALS_CSS).toMatch(/@import\s+"@fontsource\/archivo\/700\.css"/)
    expect(GLOBALS_CSS).toMatch(/@import\s+"@fontsource\/ibm-plex-sans\/400\.css"/)
    expect(GLOBALS_CSS).toMatch(/@import\s+"@fontsource\/ibm-plex-sans\/500\.css"/)
    expect(GLOBALS_CSS).toMatch(/@import\s+"@fontsource\/ibm-plex-sans\/600\.css"/)
    expect(GLOBALS_CSS).toMatch(/@import\s+"@fontsource\/ibm-plex-mono\/400\.css"/)
    expect(GLOBALS_CSS).toMatch(/@import\s+"@fontsource\/ibm-plex-mono\/500\.css"/)
    expect(GLOBALS_CSS).toMatch(/@import\s+"@fontsource\/ibm-plex-mono\/600\.css"/)
  })

  it("nenhum arquivo de config referencia fonts.googleapis.com/fonts.gstatic.com como URL real", () => {
    for (const [name, content] of [
      ["globals.css", GLOBALS_CSS],
      ["index.html", INDEX_HTML],
    ] as const) {
      // Só rejeita se aparecer como parte de uma URL (href=/@import url(...)),
      // não como texto de comentário explicando a decisão.
      expect(content, `${name} não deve referenciar fonts.googleapis.com como URL`).not.toMatch(
        /(href|url)\s*[:=(]\s*["']?https?:\/\/fonts\.googleapis\.com/,
      )
      expect(content, `${name} não deve referenciar fonts.gstatic.com como URL`).not.toMatch(
        /(href|url)\s*[:=(]\s*["']?https?:\/\/fonts\.gstatic\.com/,
      )
    }
  })

  it("package.json declara as 3 dependências @fontsource (só essas 3)", () => {
    const pkg = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf8")) as {
      dependencies: Record<string, string>
    }
    const fontsourceDeps = Object.keys(pkg.dependencies).filter((d) => d.startsWith("@fontsource/"))
    expect(fontsourceDeps.sort()).toEqual([
      "@fontsource/archivo",
      "@fontsource/ibm-plex-mono",
      "@fontsource/ibm-plex-sans",
    ])
  })
})

describe("R2-6.2 — CSP endurecida (script-src/img-src)", () => {
  const CSP_RE = /Content-Security-Policy\s+"([^"]+)"/g

  function allCspDirectives(content: string): string[] {
    const out: string[] = []
    let m: RegExpExecArray | null
    const re = new RegExp(CSP_RE)
    while ((m = re.exec(content))) out.push(m[1])
    return out
  }

  it("todo Content-Security-Policy usa script-src 'self' (sem unsafe-inline/unsafe-eval)", () => {
    for (const [name, content] of [
      ["nginx.single.conf", NGINX_HTTP],
      ["nginx.single.https.conf", NGINX_HTTPS],
    ] as const) {
      const csps = allCspDirectives(content)
      expect(csps.length, `${name} deveria ter pelo menos 1 CSP`).toBeGreaterThan(0)
      for (const csp of csps) {
        expect(csp, name).toMatch(/script-src 'self';/)
        expect(csp, name).not.toMatch(/script-src[^;]*unsafe-inline/)
        expect(csp, name).not.toMatch(/script-src[^;]*unsafe-eval/)
      }
    }
  })

  it("todo Content-Security-Policy usa img-src 'self' data: (sem coringa https:)", () => {
    for (const [name, content] of [
      ["nginx.single.conf", NGINX_HTTP],
      ["nginx.single.https.conf", NGINX_HTTPS],
    ] as const) {
      const csps = allCspDirectives(content)
      for (const csp of csps) {
        expect(csp, name).toMatch(/img-src 'self' data:;/)
        expect(csp, name).not.toMatch(/img-src[^;]*https:/)
      }
    }
  })

  it("style-src/font-src não citam mais fonts.googleapis.com/fonts.gstatic.com", () => {
    for (const [name, content] of [
      ["nginx.single.conf", NGINX_HTTP],
      ["nginx.single.https.conf", NGINX_HTTPS],
    ] as const) {
      const csps = allCspDirectives(content)
      for (const csp of csps) {
        expect(csp, name).not.toMatch(/fonts\.googleapis\.com/)
        expect(csp, name).not.toMatch(/fonts\.gstatic\.com/)
      }
    }
  })
})
