import type { ReactNode } from "react"

/**
 * Enterprise sidebar entries seam (open-core) — **Community stub**.
 *
 * Returns NO entries, so the Community sidebar shows only Community routes. The
 * Enterprise build overrides this module via Vite `resolve.alias`
 * (`@/ee/navItems` -> `@centralops/web-ee/navItems`) to inject the federated-search
 * links (federated search, correlation) — which were carved out of the Community core —
 * into the matching nav groups. Frontend counterpart of the backend `activate()` hook.
 *
 * Contract: keyed by `NavGroupKey` → the items appended to that group in
 * `Navigation.tsx`. The key is stable and NOT translated; the previous contract
 * keyed on the group's translated label, which silently dropped every EE entry
 * whenever the user switched language. `Navigation.tsx` still falls back to a
 * label lookup so an overlay built against the old contract keeps working.
 *
 * The groups follow the pipeline: `collect` → `normalize` → `reduce` → `route` →
 * `detect`, then the neutral block `overview` / `admin` / `account`. Federated
 * search and correlation belong to `detect`.
 */
export interface EeNavItem {
  key: string
  label: string
  path: string
  icon: ReactNode
}

/** Grupos da sidebar: SEIS estágios do pipeline + três neutros. */
export type NavGroupKey =
  | "collect"
  | "normalize"
  | "enrich"
  | "reduce"
  | "route"
  | "detect"
  | "overview"
  | "admin"
  | "account"

export interface EeNavContext {
  canRunQuery: boolean
  canSaveQuery: boolean
  isAdmin: boolean
}

export function eeNavItems(_ctx: EeNavContext): Partial<Record<NavGroupKey, EeNavItem[]>> {
  return {}
}
