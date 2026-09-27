import { apiRequest } from "./_core"
import type {
  CreateQueryRequest,
  Query,
  UpdateQueryRequest,
} from "@/types"

export async function listQueries() {
  return apiRequest<Query[]>("/queries/")
}

export async function createQuery(data: CreateQueryRequest) {
  return apiRequest<Query>("/queries/", {
    method: "POST",
    body: JSON.stringify(data),
  })
}

export async function updateQuery(id: number, data: UpdateQueryRequest) {
  return apiRequest<Query>(`/queries/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: JSON.stringify(data),
  })
}

export async function deleteQuery(id: number) {
  return apiRequest<void>(`/queries/${encodeURIComponent(id)}`, {
    method: "DELETE",
  })
}

export async function getQuery(id: number) {
  return apiRequest<Query>(`/queries/${encodeURIComponent(id)}`)
}

// Schedules API functions

