import { ADMIN_REDIRECT_PATH, apiRequest } from "./_core"
import type {
  CreateEmailRequest,
  EmailConfig,
  EmailRecipient,
  UpdateEmailConfigRequest,
} from "@/types"

export async function listEmails() {
  return apiRequest<EmailRecipient[]>("/emails/", { forbiddenRedirectTo: ADMIN_REDIRECT_PATH })
}

export async function createEmail(data: CreateEmailRequest) {
  return apiRequest<EmailRecipient>("/emails/", {
    method: "POST",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function deleteEmail(id: number) {
  return apiRequest<void>(`/emails/${encodeURIComponent(id)}`, {
    method: "DELETE",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getEmailConfig() {
  return apiRequest<EmailConfig>("/emails/config", { forbiddenRedirectTo: ADMIN_REDIRECT_PATH })
}

export async function updateEmailConfig(data: UpdateEmailConfigRequest) {
  return apiRequest<EmailConfig>("/emails/config", {
    method: "PUT",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function sendTestEmail() {
  return apiRequest<{ detail: string }>("/emails/test", {
    method: "POST",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

