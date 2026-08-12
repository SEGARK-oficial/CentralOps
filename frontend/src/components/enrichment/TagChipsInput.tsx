import type React from "react"
import { useId, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { XIcon } from "lucide-react"
import { cn } from "@/lib/utils"

interface TagChipsInputProps {
  label?: string
  value: string[]
  /** Tags já usadas em algum lugar. Viram sugestão, não restrição. */
  suggestions?: string[]
  helperText?: string
  placeholder?: string
  onChange: (next: string[]) => void
  "data-testid"?: string
}

/**
 * Entrada de tags em chips, com sugestão do que já existe.
 *
 * Substitui o campo de texto separado por vírgula. O problema não era o
 * formato: era que `asset_conhecido` e `asset_known` viram tags DIFERENTES sem
 * nenhum aviso, e a regra que consome `has_tag` para de casar. Erro de
 * digitação em tag é silencioso por natureza, porque a tag "errada" é válida.
 *
 * A lista de sugestões vem das outras regras da política e das tags que o
 * runtime gera sozinho. Ela sugere, mas não impede tag nova: a primeira regra
 * de uma política precisa poder inventar a primeira tag.
 *
 * Normaliza para minúsculas com underscore, que é a convenção do repo
 * (`asset_known`, `enrich_degraded`). Normalizar mata a classe inteira de
 * divergência por maiúscula e espaço.
 */
export const TagChipsInput: React.FC<TagChipsInputProps> = ({
  label,
  value,
  suggestions = [],
  helperText,
  placeholder,
  onChange,
  "data-testid": testId,
}) => {
  const { t } = useTranslation("enrichment")
  const inputId = useId().replace(/:/g, "")
  const listId = `${inputId}-sugestoes`
  const [draft, setDraft] = useState("")

  const available = useMemo(
    () => Array.from(new Set(suggestions)).filter((s) => s && !value.includes(s)).sort(),
    [suggestions, value],
  )

  function normalize(raw: string): string {
    return raw.trim().toLowerCase().replace(/\s+/g, "_")
  }

  function commit(raw: string) {
    const tag = normalize(raw)
    if (!tag || value.includes(tag)) {
      setDraft("")
      return
    }
    onChange([...value, tag])
    setDraft("")
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" || e.key === "," || e.key === "Tab") {
      if (!draft.trim()) return
      e.preventDefault()
      commit(draft)
      return
    }
    // Backspace no campo vazio remove o último chip: sem isto, corrigir a
    // última tag exige mirar num botão de 16px.
    if (e.key === "Backspace" && !draft && value.length > 0) {
      onChange(value.slice(0, -1))
    }
  }

  return (
    <div className="flex flex-col gap-1.5" data-testid={testId}>
      {label && (
        <label htmlFor={inputId} className="flex items-center gap-1.5 text-sm font-medium text-text">
          {label}
        </label>
      )}

      <div
        className={cn(
          "flex min-h-9 flex-wrap items-center gap-1.5 rounded-md border border-border-field",
          "bg-surface-tertiary px-2 py-1.5 transition-colors focus-within:border-border-field-hover",
        )}
      >
        {value.map((tag) => (
          <span
            key={tag}
            className="inline-flex items-center gap-1 rounded bg-surface px-1.5 py-0.5 font-mono text-xs text-text"
          >
            {tag}
            <button
              type="button"
              onClick={() => onChange(value.filter((v) => v !== tag))}
              aria-label={t("policies.versions.removeTag", { tag })}
              className="text-text-tertiary transition-colors hover:text-danger-500"
            >
              <XIcon size={11} aria-hidden />
            </button>
          </span>
        ))}
        <input
          id={inputId}
          list={listId}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={handleKeyDown}
          // Sair do campo confirma o que estava digitado. Sem isto, escrever a
          // tag e clicar direto em Publicar descartava o texto em silêncio.
          onBlur={() => draft.trim() && commit(draft)}
          placeholder={value.length === 0 ? placeholder : undefined}
          className="min-w-24 flex-1 bg-transparent text-sm text-text outline-none placeholder:text-text-tertiary"
        />
        <datalist id={listId}>
          {available.map((s) => (
            <option key={s} value={s} />
          ))}
        </datalist>
      </div>

      {helperText && <p className="text-xs text-muted">{helperText}</p>}
    </div>
  )
}

export default TagChipsInput
