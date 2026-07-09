"use client"

import { useState, useEffect, useCallback } from "react"
import * as api from "@/services/api"
import type { EmailConfig, EmailRecipient, UpdateEmailConfigRequest } from "@/types"

type EmailFeedback =
  | {
      type: "success" | "error"
      message: string
    }
  | null

interface UseEmailConfigReturn {
  config: EmailConfig | null
  recipients: EmailRecipient[]
  loading: boolean
  saving: boolean
  testing: boolean
  addingRecipient: boolean
  removingRecipientId: number | null
  error: string | null
  feedback: EmailFeedback
  saveConfig: (data: UpdateEmailConfigRequest) => Promise<boolean>
  addRecipient: (email: string) => Promise<boolean>
  removeRecipient: (id: number) => Promise<boolean>
  sendTest: () => Promise<boolean>
  clearFeedback: () => void
  refetch: () => Promise<void>
}

export function useEmailConfig(): UseEmailConfigReturn {
  const [config, setConfig] = useState<EmailConfig | null>(null)
  const [recipients, setRecipients] = useState<EmailRecipient[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [addingRecipient, setAddingRecipient] = useState(false)
  const [removingRecipientId, setRemovingRecipientId] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<EmailFeedback>(null)

  const fetchData = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const [cfg, emails] = await Promise.all([api.getEmailConfig(), api.listEmails()])
      setConfig(cfg)
      setRecipients(emails)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Falha ao carregar configuração"
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [])

  const saveConfig = useCallback(async (data: UpdateEmailConfigRequest) => {
    try {
      setSaving(true)
      setFeedback(null)
      const cfg = await api.updateEmailConfig(data)
      setConfig(cfg)
      setFeedback({ type: "success", message: "Configurações de email salvas com sucesso." })
      return true
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Falha ao salvar configuração de email"
      setFeedback({ type: "error", message: msg })
      return false
    } finally {
      setSaving(false)
    }
  }, [])

  const addRecipient = useCallback(async (email: string) => {
    try {
      setAddingRecipient(true)
      setFeedback(null)
      const rec = await api.createEmail({ email })
      setRecipients((prev) => [...prev, rec])
      setFeedback({ type: "success", message: `Destinatario ${email} adicionado.` })
      return true
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Falha ao adicionar destinatario"
      setFeedback({ type: "error", message: msg })
      return false
    } finally {
      setAddingRecipient(false)
    }
  }, [])

  const removeRecipient = useCallback(async (id: number) => {
    try {
      setRemovingRecipientId(id)
      setFeedback(null)
      await api.deleteEmail(id)
      setRecipients((prev) => prev.filter((r) => r.id !== id))
      setFeedback({ type: "success", message: "Destinatario removido com sucesso." })
      return true
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Falha ao remover destinatario"
      setFeedback({ type: "error", message: msg })
      return false
    } finally {
      setRemovingRecipientId(null)
    }
  }, [])

  const sendTest = useCallback(async () => {
    try {
      setTesting(true)
      setFeedback(null)
      const response = await api.sendTestEmail()
      setFeedback({ type: "success", message: response.detail })
      return true
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Falha ao enviar email de teste"
      setFeedback({ type: "error", message: msg })
      return false
    } finally {
      setTesting(false)
    }
  }, [])

  const clearFeedback = useCallback(() => {
    setFeedback(null)
  }, [])

  const refetch = useCallback(async () => {
    await fetchData()
  }, [fetchData])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  return {
    config,
    recipients,
    loading,
    saving,
    testing,
    addingRecipient,
    removingRecipientId,
    error,
    feedback,
    saveConfig,
    addRecipient,
    removeRecipient,
    sendTest,
    clearFeedback,
    refetch,
  }
}
