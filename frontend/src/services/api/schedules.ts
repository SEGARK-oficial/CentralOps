import { ADMIN_REDIRECT_PATH, apiRequest } from "./_core"
import type {
  CreateScheduleRequest,
  Schedule,
  SearchHistoryItem,
  UpdateScheduleRequest,
} from "@/types"

export async function listSchedules() {
  return apiRequest<Schedule[]>("/schedules/", { forbiddenRedirectTo: ADMIN_REDIRECT_PATH })
}

export async function createSchedule(data: CreateScheduleRequest) {
  return apiRequest<Schedule>("/schedules/", {
    method: "POST",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

/**
 * Edita um agendamento existente, inclusive um que já está rodando.
 *
 * Só os campos enviados são aplicados. Editar NÃO dispara execução: o backend
 * reagenda quando a cadência muda e fica inerte quando não muda.
 */
export async function updateSchedule(id: number, data: UpdateScheduleRequest) {
  return apiRequest<Schedule>(`/schedules/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function deleteSchedule(id: number) {
  return apiRequest<void>(`/schedules/${encodeURIComponent(id)}`, {
    method: "DELETE",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getScheduleHistory(scheduleId: number) {
  return apiRequest<SearchHistoryItem[]>(`/schedules/${encodeURIComponent(scheduleId)}/history`, {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

// Email API functions

