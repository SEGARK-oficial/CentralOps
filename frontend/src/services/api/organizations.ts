import { ADMIN_REDIRECT_PATH, apiRequest } from "./_core"
import type {
  CreateOrganizationRequest,
  Organization,
  OrganizationPartnerLink,
  PartnerAttachRequest,
  PartnerDetachRequest,
  PartnerLinkResult,
  UpdateOrganizationRequest,
} from "@/types"

// ── Organization API ──────────────────────────────────────────────────

export interface ListOrganizationsParams {
  includeInactive?: boolean
  /** Substring case-insensitive em name/slug. */
  name?: string
  /** "active" | "inactive" | "all". Sobrepõe includeInactive. */
  status?: "active" | "inactive" | "all"
  /** "true" | "false" | "all". Default backend: 'all'. */
  autoManaged?: "true" | "false" | "all"
  externalProvider?: string
  /** 1-indexed. Default backend: 1. */
  page?: number
  /** Itens por página. Cap 200. Default backend: 50. */
  size?: number
}

export async function listOrganizations(params: ListOrganizationsParams | boolean = {}) {
  // Compat: chamada antiga `listOrganizations(true)` continua funcionando.
  const opts: ListOrganizationsParams =
    typeof params === "boolean" ? { includeInactive: params } : params

  const search = new URLSearchParams()
  if (opts.includeInactive) search.set("include_inactive", "true")
  if (opts.name && opts.name.trim()) search.set("name", opts.name.trim())
  if (opts.status) search.set("status", opts.status)
  if (opts.autoManaged) search.set("auto_managed", opts.autoManaged)
  if (opts.externalProvider) search.set("external_provider", opts.externalProvider)
  if (opts.page) search.set("page", String(opts.page))
  if (opts.size) search.set("size", String(opts.size))
  const qs = search.toString()
  return apiRequest<Organization[]>(`/organizations/${qs ? `?${qs}` : ""}`)
}

export interface BulkDeactivateOrganizationsResult {
  processed: number
  deactivated: number
  errors: { id: number; reason: string }[]
}

export async function bulkDeactivateOrganizations(ids: number[]) {
  return apiRequest<BulkDeactivateOrganizationsResult>(
    "/organizations/bulk/deactivate",
    {
      method: "POST",
      body: JSON.stringify({ ids }),
      forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
    },
  )
}

export async function createOrganization(data: CreateOrganizationRequest) {
  return apiRequest<Organization>("/organizations/", {
    method: "POST",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getOrganization(id: number) {
  return apiRequest<Organization>(`/organizations/${encodeURIComponent(id)}`)
}

export async function updateOrganization(id: number, data: UpdateOrganizationRequest) {
  return apiRequest<Organization>(`/organizations/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function deleteOrganization(id: number) {
  return apiRequest<void>(`/organizations/${encodeURIComponent(id)}`, {
    method: "DELETE",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

// ── Vínculo com o parceiro Sophos (ADR-0017, Enterprise) ───────────────
// Rotas do módulo EE (montadas em /api, admin global + licença multi_tenant). O
// stub Community não as chama; ficam aqui, como `listSophosTenants`/`selectTenants`,
// para o overlay usar o mesmo cliente HTTP (cookie, timeout, Accept-Language).
// Sem `forbiddenRedirectTo`: um 403 `license_required` precisa chegar à tela, não
// redirecioná-la.

export async function getOrganizationPartnerLink(organizationId: number) {
  return apiRequest<OrganizationPartnerLink>(
    `/organizations/${encodeURIComponent(organizationId)}/partner`,
  )
}

export async function detachOrganizationFromPartner(
  organizationId: number,
  body: PartnerDetachRequest,
) {
  return apiRequest<PartnerLinkResult>(
    `/organizations/${encodeURIComponent(organizationId)}/partner/detach`,
    { method: "POST", body: JSON.stringify(body) },
  )
}

export async function attachOrganizationToPartner(
  organizationId: number,
  body: PartnerAttachRequest,
) {
  return apiRequest<PartnerLinkResult>(
    `/organizations/${encodeURIComponent(organizationId)}/partner/attach`,
    { method: "POST", body: JSON.stringify(body) },
  )
}
