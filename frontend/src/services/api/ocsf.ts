import { ADMIN_REDIRECT_PATH, apiRequest } from "./_core"
import type {
  OcsfCompliance,
  OcsfEnforcementMode,
  OcsfPolicy,
} from "@/types"

// ── OCSF governance — admin ────────────────────────────────────
export async function listOcsfPolicies() {
  return apiRequest<OcsfPolicy[]>("/ocsf/policies", {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function setOcsfPolicy(orgId: number, enforcementMode: OcsfEnforcementMode) {
  return apiRequest<OcsfPolicy>(`/ocsf/policies/${encodeURIComponent(orgId)}`, {
    method: "PUT",
    body: JSON.stringify({ enforcement_mode: enforcementMode }),
  })
}

export async function getOcsfCompliance() {
  return apiRequest<OcsfCompliance>("/ocsf/compliance", {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

