/**
 * URLs centralizadas para a documentação pública do projeto.
 *
 * Aponta para o portal Docusaurus publicado em GitHub Pages. Override possível
 * via env `VITE_DOCS_BASE_URL` para apontar a um portal interno/staging.
 */

const DEFAULT_DOCS_BASE = "https://docs.segark.com"
const DOCS_BASE_URL =
  ((import.meta as unknown as { env?: Record<string, string | undefined> }).env?.VITE_DOCS_BASE_URL ??
    DEFAULT_DOCS_BASE).replace(/\/+$/, "")

/**
 * Converte um path tipo `normalization/dsl-spec.md` num link do portal:
 * `https://.../docs/normalization/dsl-spec`. O Docusaurus serve sem extensão.
 */
export function docsUrl(path: string, anchor?: string): string {
  const cleanPath = path.replace(/^\/+/, "").replace(/\.mdx?$/, "")
  const base = `${DOCS_BASE_URL}/docs/${cleanPath}`
  return anchor ? `${base}#${anchor}` : base
}

/** Atalhos pras docs mais referenciadas. */
export const DOCS = {
  mappingEditorGuide: docsUrl("normalization/dsl-spec"),
  ruleAnatomy: docsUrl("normalization/dsl-spec", "anatomia-de-uma-regra"),
  howMappingWorks: docsUrl("normalization/overview"),
} as const
