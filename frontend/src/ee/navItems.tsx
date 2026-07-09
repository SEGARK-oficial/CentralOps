import type { ReactNode } from "react"

/**
 * Enterprise sidebar entries seam (open-core) — **Community stub**.
 *
 * Returns NO entries, so the Community sidebar shows only Community routes. The
 * Enterprise build overrides this module via Vite `resolve.alias`
 * (`@/ee/navItems` -> `@centralops/web-ee/navItems`) to inject the federated-search
 * links (Busca federada, Correlação) — which were carved out of the Community core —
 * into the matching nav groups. Frontend counterpart of the backend `activate()` hook.
 *
 * Contract: keyed by GROUP LABEL → the items appended to that group in `Navigation.tsx`.
 */
export interface EeNavItem {
  key: string
  label: string
  path: string
  icon: ReactNode
}

export interface EeNavContext {
  canRunQuery: boolean
  canSaveQuery: boolean
  isAdmin: boolean
}

export function eeNavItems(_ctx: EeNavContext): Record<string, EeNavItem[]> {
  return {}
}
