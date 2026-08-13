"use client"

import type React from "react"
import { useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { LockIcon, ShieldCheckIcon } from "lucide-react"

import * as api from "@/services/api"
import type { ScopeName } from "@/types"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Notice } from "@/components/ui/Notice/Notice"
import {
  PERMISSION_CATEGORY_ORDER,
  categoryLabelKeyOf,
  categoryOf,
  descriptionKeyOf,
} from "@/lib/permissions"

interface ScopeSelectorProps {
  /** Currently selected scopes. ``null`` means "full inherit" (no checkboxes
   *  selected, hint shown). Empty array same effect, but explicit. */
  value: ScopeName[] | null
  onChange: (scopes: ScopeName[] | null) => void
  /** When true, disables interaction (e.g. while submitting). */
  disabled?: boolean
  /** Hides the "full inherit" toggle — useful when caller already enforces
   *  least-privilege (e.g. Service Accounts em produção). */
  requireExplicit?: boolean
}

export const ScopeSelector: React.FC<ScopeSelectorProps> = ({
  value,
  onChange,
  disabled = false,
  requireExplicit = false,
}) => {
  const { t } = useTranslation("admin")
  const [available, setAvailable] = useState<ScopeName[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  /**
   * Qual radio o usuário ESCOLHEU, independente de `value`.
   *
   * `value` sozinho não basta: `null` (herdar) e `[]` (restringir, zero scopes
   * marcados ainda) são visualmente indistinguíveis a partir dele, e ambos
   * caem no mesmo "sem scopes selecionados". Clicar em "Restringir" chamava
   * `onChange([])`, o componente re-renderizava com `value=[]`, e o cálculo
   * antigo (`value === null || value.length === 0`) lia isso como "ainda é
   * herdar" — o clique não tinha efeito visível nenhum: nem o radio marcava,
   * nem o grid de checkboxes abria.
   *
   * O componente remonta a cada abertura do modal (o `Modal` pai desmonta os
   * filhos quando fechado), então este estado nasce correto a cada vez.
   */
  const [mode, setMode] = useState<"inherit" | "restrict">(
    value !== null && value.length > 0 ? "restrict" : "inherit",
  )
  const restricting = mode === "restrict" || requireExplicit

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api
      .listScopes()
      .then((scopes) => {
        if (cancelled) return
        setAvailable(scopes)
        setError(null)
      })
      .catch((e) => {
        if (cancelled) return
        setError(e instanceof Error ? e.message : "Falha ao carregar scopes")
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  // Agrupa scopes carregados pelas categorias.
  // Categoria e descrição vêm do catálogo compartilhado com a matriz de
  // /admin/users. Antes eram duas cópias, e elas divergiram: `query.run`,
  // `query.save` e `correlation.preview` existiam no backend e caíam em
  // "Outros" aqui, sem descrição nenhuma.
  const grouped = useMemo(() => {
    const m = new Map<string, ScopeName[]>()
    if (!available) return m
    for (const cat of PERMISSION_CATEGORY_ORDER) m.set(cat, [])
    for (const scope of available) {
      m.get(categoryOf(scope))?.push(scope)
    }
    return m
  }, [available])

  const toggleScope = (scope: ScopeName) => {
    if (disabled) return
    const current = value ?? []
    const next = current.includes(scope)
      ? current.filter((s) => s !== scope)
      : [...current, scope]
    // Fica em modo restrito mesmo em zero scopes: o usuário ESCOLHEU
    // restringir, e voltar sozinho para "herdar" trocaria o radio marcado sem
    // nenhum clique dele — pior, herdar é a role INTEIRA, o oposto de
    // restringir. Ver aviso abaixo: 0 marcado ainda assim equivale a herdar
    // no backend (`effective_scopes`: lista vazia é tratada como ausente).
    onChange(next)
  }

  const setFullInherit = () => {
    if (disabled || requireExplicit) return
    setMode("inherit")
    onChange(null)
  }

  const setRestrict = () => {
    if (disabled) return
    setMode("restrict")
    onChange(value ?? [])
  }

  if (loading) {
    return (
      <div className="rounded-md border border-border bg-bg-subtle p-4">
        <LoadingSpinner size="sm" text="Carregando lista de scopes…" />
      </div>
    )
  }

  if (error || !available) {
    return (
      <Notice variant="danger">
        Falha ao listar scopes: {error ?? "resposta vazia"}. Token será criado
        com herança completa de permissões.
      </Notice>
    )
  }

  return (
    <div className="space-y-3">
      {!requireExplicit && (
        <label className="flex items-start gap-2 rounded-md border border-border p-3 cursor-pointer hover:bg-bg-subtle">
          <input
            type="radio"
            checked={mode === "inherit"}
            onChange={setFullInherit}
            disabled={disabled}
            className="mt-1"
          />
          <div className="flex-1">
            <div className="flex items-center gap-2 text-sm font-semibold">
              <ShieldCheckIcon size={16} />
              Herdar permissões da conta (padrão)
            </div>
            <p className="mt-1 text-xs text-text-secondary">
              Token tem o mesmo nível de acesso da sua conta. Equivale a Fase 1
              (sem scopes). Se você for despromovido, o token perde acesso
              automaticamente.
            </p>
          </div>
        </label>
      )}

      <label className="flex items-start gap-2 rounded-md border border-border p-3 cursor-pointer hover:bg-bg-subtle">
        <input
          type="radio"
          checked={restricting}
          onChange={setRestrict}
          disabled={disabled}
          className="mt-1"
        />
        <div className="flex-1">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <LockIcon size={16} />
            Restringir a scopes específicos (least privilege)
          </div>
          <p className="mt-1 text-xs text-text-secondary">
            Marque apenas o que o cliente precisa. Permissão efetiva =
            interseção do role do dono com os scopes selecionados.
          </p>
        </div>
      </label>

      {restricting && (
        <div className="space-y-3 rounded-md border border-border-strong bg-bg p-3">
          {PERMISSION_CATEGORY_ORDER.map((cat) => {
            const scopes = grouped.get(cat)
            if (!scopes || scopes.length === 0) return null
            return (
              <div key={cat}>
                <div className="text-xs font-semibold uppercase tracking-wider text-text-secondary">
                  {t(categoryLabelKeyOf(cat))}
                </div>
                <div className="mt-1 grid gap-1">
                  {scopes.map((scope) => (
                    <label
                      key={scope}
                      className="flex items-start gap-2 rounded p-1.5 hover:bg-bg-subtle cursor-pointer"
                    >
                      <input
                        type="checkbox"
                        checked={value?.includes(scope) ?? false}
                        onChange={() => toggleScope(scope)}
                        disabled={disabled}
                        className="mt-0.5"
                      />
                      <div className="flex-1 text-sm">
                        <code className="font-mono text-xs">{scope}</code>
                        <span className="ml-2 text-xs text-text-secondary">
                          {t(descriptionKeyOf(scope))}
                        </span>
                      </div>
                    </label>
                  ))}
                </div>
              </div>
            )
          })}
          {value && value.length > 0 ? (
            <div className="border-t pt-2 text-xs text-text-secondary">
              <strong>{value.length}</strong>{" "}
              {value.length === 1 ? "scope" : "scopes"} selecionado(s)
            </div>
          ) : (
            // O backend trata lista vazia igual a ausente (`effective_scopes`:
            // `if not token_scopes`, e `not []` é `True` em Python) — token com
            // zero marcado sai idêntico a "herdar", não a "sem acesso nenhum".
            // Sem este aviso, restringir e não marcar nada parece least
            // privilege e na prática libera a role inteira.
            <div className="border-t pt-2 text-xs text-warning-600">
              {requireExplicit
                ? "Nenhum scope marcado ainda equivale a herdar tudo. Marque ao menos um."
                : "Nenhum scope marcado ainda equivale a herdar tudo. Marque ao menos um, ou escolha herdar as permissões da conta acima."}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
