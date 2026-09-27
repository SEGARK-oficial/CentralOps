"use client"

import type React from "react"
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import * as api from "@/services/api"
import type { EditionStatus } from "@/types"

/**
 * Edition/license context.
 *
 * Fetches GET /api/edition once for the authenticated session and exposes the
 * running edition + licensed features to the UI. Fail-closed: any error resolves
 * to Community (no features, no caps) — the UI never grants paid behavior on doubt,
 * mirroring the backend's fail-closed-to-Community contract.
 */
interface EditionContextValue {
  edition: string
  features: string[]
  plan: string | null
  seats: number | null
  /** Teto de orgs do tier (null = ilimitado; Starter single-tenant = 1). */
  maxOrganizations: number | null
  /** ISO-8601 ou null. */
  expiresAt: string | null
  /** True = licença venceu mas está na janela de carência (renove!). */
  expiredInGrace: boolean
  isEnterprise: boolean
  loading: boolean
  error: string | null
  /** True quando a feature paga está liberada na edição corrente. */
  hasFeature: (name: string) => boolean
  refresh: () => Promise<void>
}

const EditionContext = createContext<EditionContextValue | undefined>(undefined)

interface EditionProviderProps {
  children: React.ReactNode
}

export const EditionProvider: React.FC<EditionProviderProps> = ({ children }) => {
  // `common` é namespace de SHELL (carregado eager no boot, ver
  // `SHELL_NAMESPACES` em `src/i18n/index.ts`) — o provider monta perto da
  // raiz do app, cedo demais pra arriscar suspender num namespace por tela.
  const { t } = useTranslation("common")
  const [status, setStatus] = useState<EditionStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const data = await api.getEdition()
      setStatus(data)
      setError(null)
    } catch (err) {
      // Fail-closed: na dúvida, Community (sem features, sem teto).
      // R2-6.11: fallback (sem mensagem do próprio erro) passa por `t()`.
      setStatus(null)
      setError(err instanceof Error ? err.message : t("editionContext.loadError"))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // useMemo: sem isto, `[]` nasce de novo a cada render quando `status?.features`
  // é undefined, e `hasFeature` (deps: [features]) fica instável mesmo com o
  // mesmo conteúdo lógico — qualquer consumidor que dependa de `hasFeature`
  // re-executaria sem necessidade.
  const features = useMemo(() => status?.features ?? [], [status?.features])
  // Normaliza p/ minúsculo (defensivo): se o backend mudar a caixa, não regredimos
  // a Community por engano.
  const edition = (status?.edition ?? "community").toLowerCase()
  const hasFeature = useCallback((name: string) => features.includes(name), [features])

  return (
    <EditionContext.Provider
      value={{
        edition,
        features,
        plan: status?.plan ?? null,
        seats: status?.seats ?? null,
        maxOrganizations: status?.max_organizations ?? null,
        expiresAt: status?.expires_at ?? null,
        expiredInGrace: status?.expired_in_grace ?? false,
        isEnterprise: edition === "enterprise",
        loading,
        error,
        hasFeature,
        refresh,
      }}
    >
      {children}
    </EditionContext.Provider>
  )
}

export function useEdition(): EditionContextValue {
  const context = useContext(EditionContext)
  if (!context) {
    throw new Error("useEdition must be used within an EditionProvider")
  }
  return context
}

/** Atalho para gatear UI por feature paga. Ex.: `const fleet = useFeature("fleet_management")`. */
export function useFeature(name: string): boolean {
  return useEdition().hasFeature(name)
}
