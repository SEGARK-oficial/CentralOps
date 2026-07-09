import type { ReactElement } from "react"

/**
 * Enterprise edition routes seam (open-core).
 *
 * This is the **Community stub**: an empty route set, so no Enterprise screen or
 * chunk ships in the Community bundle. It is the frontend counterpart of the backend
 * `activate()` hook.
 *
 * The Enterprise build overrides this module via Vite `resolve.alias`
 * (`@/ee/routes` -> `@centralops/web-ee/routes`, the private overlay), supplying the
 * real protected `<Route>` elements (lazy-loaded EE screens). Gating by build-time
 * module override — not a runtime flag — is what keeps EE code physically out of the
 * Community artifact.
 *
 * Contract: an array of react-router `<Route>` elements, rendered inside the
 * protected shell and before the catch-all (see App.tsx). Each element must carry a
 * stable `key`.
 */
export const eeRoutes: ReactElement[] = []
