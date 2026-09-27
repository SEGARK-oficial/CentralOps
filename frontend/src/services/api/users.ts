import { ADMIN_REDIRECT_PATH, apiRequest } from "./_core"
import type {
  AppUser,
  CreateUserRequest,
  UpdateUserRequest,
} from "@/types"

export async function listUsers() {
  return apiRequest<AppUser[]>("/auth/users", { forbiddenRedirectTo: ADMIN_REDIRECT_PATH })
}

export async function createUser(data: CreateUserRequest) {
  return apiRequest<AppUser>("/auth/users", {
    method: "POST",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function updateUser(id: string, data: UpdateUserRequest) {
  return apiRequest<AppUser>(`/auth/users/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function deleteUser(id: string) {
  return apiRequest<void>(`/auth/users/${encodeURIComponent(id)}`, {
    method: "DELETE",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getPermissionsMatrix() {
  return apiRequest<Record<string, string[]>>("/auth/permissions", {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

// Search API functions (now SQL via XDR Query API)

